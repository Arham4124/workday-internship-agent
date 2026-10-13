"""Command line: ./agent.sh <command>"""
from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys

from . import claude, config, cycle, dashboard, db, discover, mail, scheduler, secrets, tracker, triage


def _logging(verbose: bool) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers = [logging.FileHandler(config.LOGS / "agent.log"), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_setup(_):
    print("Secrets go into your macOS Keychain (service 'InternshipAgent'). Leave blank to keep the current value.\n")
    pw = getpass.getpass(f"Gmail app password for {config.profile()['contact']['email']} (16 letters, spaces OK): ").replace(" ", "")
    if pw:
        secrets.put("gmail-app-password", pw)
        try:
            print("  Gmail login:", mail.test_login())
        except Exception as e:
            print("  Gmail login FAILED:", e)
    pw = getpass.getpass("Your usual Workday password (for companies where you already have an account): ")
    if pw:
        secrets.put("workday-common", pw)
        print("  saved.")
    (config.CONFIG / "profile.yaml").chmod(0o600)
    print("\nDone.")


def cmd_import_secrets(_):
    """Move passwords from ~/.jobagent_secrets into the Keychain, then wipe and delete the file."""
    import os
    path = os.path.expanduser("~/.jobagent_secrets")
    if not os.path.exists(path):
        sys.exit("~/.jobagent_secrets not found")
    values = {}
    with open(path) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    gmail = values.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    workday = values.get("WORKDAY_PASSWORD", "")
    if gmail:
        secrets.put("gmail-app-password", gmail)
        print(f"Gmail app password: saved to Keychain ({len(gmail)} characters)")
        try:
            print("Gmail login test:", mail.test_login())
        except Exception as e:
            print("Gmail login test FAILED:", type(e).__name__, str(e)[:120])
    else:
        print("Gmail app password: not provided")
    if workday:
        secrets.put("workday-common", workday)
        print(f"Workday password: saved to Keychain ({len(workday)} characters)")
    else:
        print("Workday password: not provided")
    size = os.path.getsize(path)
    with open(path, "r+b") as f:  # overwrite before deleting
        f.write(os.urandom(size))
        f.flush()
        os.fsync(f.fileno())
    os.remove(path)
    print("~/.jobagent_secrets wiped and deleted.")


SECRETS_FILE = "~/.jobagent_secrets"
SECRETS_TEMPLATE = """# Type each password right after the = sign (no quotes, no spaces). Save, then run:
#     ./agent.sh import-secrets
# which moves them into your macOS Keychain, tests the Gmail login, and wipes + deletes this file.
GMAIL_APP_PASSWORD=
WORKDAY_PASSWORD=
"""


def cmd_init(_):
    """First-time setup: create your private config files from the blank templates."""
    import shutil
    for name in ("settings", "profile", "answers"):
        dst, src = config.CONFIG / f"{name}.yaml", config.CONFIG / f"{name}.example.yaml"
        if dst.exists():
            print(f"  keep    {dst.relative_to(config.ROOT)} (already exists)")
        else:
            shutil.copy(src, dst)
            print(f"  created {dst.relative_to(config.ROOT)}  <- fill this in (see QUESTIONNAIRE.md)")
        if name != "settings":
            dst.chmod(0o600)
    (config.ROOT / "documents").mkdir(exist_ok=True)
    print("Put your resume at documents/resume.pdf (and optionally documents/transcript.pdf).")


def cmd_secrets_template(_):
    """Create the private file you type your passwords into (readable only by you)."""
    import os
    path = os.path.expanduser(SECRETS_FILE)
    if os.path.exists(path):
        print(f"{SECRETS_FILE} already exists.")
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(SECRETS_TEMPLATE)
        print(f"Created {SECRETS_FILE} (permissions 600: only you can read it).")
    print(f"Now, in Terminal (not in a chat with Claude):  nano {SECRETS_FILE}")
    print("Type the passwords after the = signs, save (Control+O, Return, Control+X), then run:")
    print("  ./agent.sh import-secrets")


def cmd_discover(_):
    con = db.connect()
    print(discover.run_discovery(con))
    dashboard.render(con)


def cmd_triage(_):
    con = db.connect()
    if until := claude.cooldown_active(con):
        print(f"Claude usage limit reached; paused until {until:%H:%M} UTC.")
        return
    print(triage.run_triage(con, max_jobs=400))
    dashboard.render(con)


def cmd_cycle(args):
    cycle.run_cycle(headed=args.headed or None, force=args.force or args.count is not None, count=args.count)


def cmd_limit(args):
    """Show or set how many applications the agent sends (per day, per company)."""
    changes = []
    if args.per_day is not None:
        v = args.per_day.strip().lower()
        if v in ("unlimited", "none", "no-limit", "max"):
            config.set_setting("max_applications_per_day", "unlimited")
        elif v.isdigit():
            config.set_setting("max_applications_per_day", int(v))
        else:
            sys.exit("per-day limit must be a number, 0 (scan and rank only), or 'unlimited'")
        changes.append("per day")
    if args.per_company is not None:
        config.set_setting("max_per_company_per_season", args.per_company)
        changes.append("per company")
    if args.per_priority_company is not None:
        config.set_setting("max_per_priority_company_per_season", args.per_priority_company)
        changes.append("per priority-industry company")
    lim = config.settings()["limits"]
    day = cycle.daily_limit()
    print(f"Applications per day:            {'unlimited' if day == float('inf') else int(day)}"
          f"{'  (0 = scan and rank only, no applying)' if day == 0 else ''}")
    print(f"Per company per season:          {lim['max_per_company_per_season']}")
    print(f"Per priority-industry company:   {lim.get('max_per_priority_company_per_season', lim['max_per_company_per_season'])}")
    print(f"Submitted or ready today:        {cycle.applied_today(db.connect())}")
    if changes:
        print(f"Updated {', '.join(changes)}; takes effect on the next run.")


def cmd_apply(args):
    con = db.connect()
    job = db.job(con, args.job) or con.execute(
        "SELECT * FROM jobs WHERE id LIKE ? ORDER BY score DESC LIMIT 1", (f"%{args.job}%",)).fetchone()
    if not job:
        sys.exit(f"no job matching {args.job!r}")
    print(f"Applying ({args.mode or config.settings()['mode']} mode): {job['company']} — {job['title']}")
    print(cycle.apply_one(con, job, mode=args.mode, headed=args.headed or None))
    dashboard.render(con)


def cmd_approve(args):
    con = db.connect()
    where = "status='ready_for_review'" + ("" if args.job == "all" else " AND id LIKE ?")
    params = () if args.job == "all" else (f"%{args.job}%",)
    ids = [r[0] for r in con.execute(f"SELECT id FROM jobs WHERE {where}", params)]
    n = con.execute(f"UPDATE jobs SET status='approved' WHERE {where}", params).rowcount
    con.commit()
    for jid in ids:
        tracker.record(db.job(con, jid), "approved", "you")
    print(f"{n} application(s) approved; they will be submitted on the next run.")
    dashboard.render(con)


def cmd_mode(args):
    p = config.CONFIG / "settings.yaml"
    p.write_text(re.sub(r"(?m)^mode: \w+", f"mode: {args.mode}", p.read_text()))
    print(f"mode set to {args.mode}")


def cmd_pause(_):
    db.kv_set(db.connect(), "paused", True)
    print("Paused. ./agent.sh resume to continue.")


def cmd_resume(_):
    db.kv_set(db.connect(), "paused", False)
    print("Resumed.")


def cmd_status(_):
    con = db.connect()
    for status, n in con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print(f"{status:>18}: {n}")
    print("\nTop queued:")
    for r in con.execute("SELECT score, company, title FROM jobs WHERE status='queued' ORDER BY score DESC LIMIT 15"):
        print(f"  {r[0]:>3}  {r[1][:24]:<24}  {r[2][:70]}")
    print(f"\nApplied today: {cycle.applied_today(con)}   paused: {bool(db.kv_get(con, 'paused'))}   "
          f"last run: {db.kv_get(con, 'last_run_date', 'never')}   Claude cooldown: {claude.cooldown_active(con) or 'none'}")
    dashboard.render(con)
    print(f"Dashboard: {config.DASHBOARD}")


def cmd_tracker(_):
    """Bring applications.csv up to date and summarise it."""
    con = db.connect()
    added = tracker.sync_from_db(con)
    rows = tracker.load()
    from collections import Counter
    print(f"{tracker.PATH}  ({len(rows)} rows{f', {added} added from history' if added else ''})")
    for status, n in Counter(r.get("status", "") for r in rows).most_common():
        print(f"  {status:<18} {n}")
    print("Add applications you send yourself as rows with status 'applied_manually'; the agent will skip them.")


def cmd_answer(_):
    con = db.connect()
    rows = con.execute("SELECT * FROM questions WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        print("No pending questions.")
        return
    for q in rows:
        print(f"\n[{q['job_id'].split('|')[0]}] {q['question']}\n  ({q['context'] or ''})")
        a = input("Your answer (blank = skip): ").strip()
        if a:
            config.append_answer(q["question"], a)
            con.execute("UPDATE questions SET status='answered', answer=? WHERE id=?", (a, q["id"]))
            con.execute("UPDATE jobs SET status='queued' WHERE id=? AND status='needs_answer'", (q["job_id"],))
            con.commit()
    dashboard.render(con)


def cmd_login(args):
    """Show the Workday login the agent uses for a company, so you can sign in yourself."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("For your security this only works when you type it yourself in a Terminal window.")
    con = db.connect()
    rows = con.execute("SELECT tenant, password_source FROM accounts WHERE tenant LIKE ?", (f"%{args.company.lower()}%",)).fetchall()
    for r in rows:
        print(f"{r['tenant']}: {config.profile()['contact']['email']} / {secrets.tenant_password(r['tenant'])}")
    if not rows:
        print("No Workday account recorded for that company yet.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent.sh", description="Workday internship agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    add("init", cmd_init, "first-time setup: create your config files from the blank templates")
    add("secrets-template", cmd_secrets_template, "create ~/.jobagent_secrets to type passwords into privately")
    add("setup", cmd_setup, "save Gmail app password + usual Workday password in Keychain (hidden prompts)")
    add("import-secrets", cmd_import_secrets, "move passwords from ~/.jobagent_secrets into Keychain, then delete the file")
    add("discover", cmd_discover, "scan all Workday career sites for internships")
    add("triage", cmd_triage, "have Claude rank newly found internships")
    p = add("cycle", cmd_cycle, "the daily run: scan, rank, apply within limits")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--force", action="store_true", help="run even if today's run already happened")
    p.add_argument("--count", type=int, help="apply to this many now (one-off; ignores the daily limit)")
    p = add("limit", cmd_limit, "show or set how many applications per day / per company")
    p.add_argument("per_day", nargs="?", help="a number, 0 (scan and rank only), or 'unlimited'")
    p.add_argument("--per-company", type=int, help="max applications per company per season")
    p.add_argument("--per-priority-company", type=int, help="same, for companies in your priority industries")
    p = add("apply", cmd_apply, "apply to one job now (id or part of it)")
    p.add_argument("job")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--mode", choices=["review", "auto"])
    add("approve", cmd_approve, "approve reviewed applications for submission ('all' or a job id)").add_argument("job")
    add("mode", cmd_mode, "review = stop before Submit, auto = submit").add_argument("mode", choices=["review", "auto"])
    add("pause", cmd_pause, "pause everything")
    add("resume", cmd_resume, "resume")
    add("status", cmd_status, "summary + refresh Dashboard.html")
    add("answer", cmd_answer, "answer questions the agent could not")
    add("tracker", cmd_tracker, "update and summarise applications.csv (your application record)")
    add("login", cmd_login, "show the Workday login the agent made for a company").add_argument("company")
    add("install", lambda a: scheduler.install(), "run once a day in the background")
    add("uninstall", lambda a: scheduler.uninstall(), "stop the background runs")
    args = ap.parse_args(argv)
    _logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
