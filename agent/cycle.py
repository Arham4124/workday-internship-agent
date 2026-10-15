"""The daily run: scan sites -> rank new postings -> apply within limits -> dashboard -> digest."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import time
import traceback
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from . import apply, claude, config, dashboard, db, discover, mail, notify, secrets, tracker, triage

log = logging.getLogger("agent.cycle")
ACTIVE = ("submitted", "ready_for_review", "approved", "applying", "already_applied")


def _lock():
    f = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return f


def _midnight_utc() -> str:
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def applied_today(con) -> int:
    return con.execute("SELECT COUNT(*) FROM jobs WHERE applied_at >= ? AND status IN ('submitted','ready_for_review')",
                       (_midnight_utc(),)).fetchone()[0]


def _eligible(con, rows, out, per, caps, prio_of, blocked, applied):
    """Yield rows that pass the transcript block, applications.csv, duplicate-role and per-company checks."""
    for r in rows:
        if r["company"] in blocked:
            continue
        why = tracker.duplicate_of(r, applied)
        if why:  # already applied to this role (by the agent or by you): never again
            db.set_job(con, r["id"], status="duplicate", result={"status": "duplicate", "reason": why})
            continue
        key = (r["company"], " ".join((r["title"] or "").lower().split()))
        dup = any((o["company"], " ".join((o["title"] or "").lower().split())) == key for o in out) or con.execute(
            f"SELECT 1 FROM jobs WHERE company=? AND lower(title)=lower(?) AND id<>? AND status IN ({','.join('?' * len(ACTIVE))})",
            (r["company"], r["title"], r["id"], *ACTIVE)).fetchone()
        if dup:  # same role posted in several locations: apply once
            continue
        used = per.setdefault(r["company"], con.execute(
            f"SELECT COUNT(*) FROM jobs WHERE company=? AND status IN ({','.join('?' * len(ACTIVE))})",
            (r["company"], *ACTIVE)).fetchone()[0])
        cap = caps["priority"] if prio_of(r) else caps["other"]
        if r["status"] == "queued" and used >= cap:
            continue
        per[r["company"]] = used + (r["status"] == "queued")
        yield r


def pick_jobs(con, budget: int, exclude: set | None = None) -> list:
    """Today's applications: already-approved ones first, then by industry priority and fit score.
    targeting.priority_sectors lists industries (companies.yaml `sector:`) that come first;
    priority_mode strict = other industries only when no priority opening is left, prefer = fill with them."""
    s = config.settings()
    lim, tgt = s["limits"], s.get("targeting", {}) or {}
    prio = set(tgt.get("priority_sectors") or [])
    strict = (tgt.get("priority_mode") or "strict") == "strict"
    caps = {"priority": lim.get("max_per_priority_company_per_season",
                                lim.get("max_per_pharma_company_per_season", lim["max_per_company_per_season"])),
            "other": lim["max_per_company_per_season"]}
    sector = {c["name"]: c.get("sector", "other") for c in config.companies()}
    prio_of = lambda r: sector.get(r["company"]) in prio  # noqa: E731
    blocked = set()
    if not config.doc_path("transcript"):  # companies that demand a transcript: wait until one is added
        blocked = {r[0] for r in con.execute(
            "SELECT DISTINCT j.company FROM questions q JOIN jobs j ON j.id = q.job_id "
            "WHERE q.status='pending' AND q.question LIKE '%transcript%'")}
    applied = tracker.load()
    exclude = exclude or set()
    approved = [r for r in con.execute("SELECT * FROM jobs WHERE status='approved' ORDER BY score DESC")
                if r["id"] not in exclude]
    queued = []
    for r in con.execute("SELECT * FROM jobs WHERE status='queued'").fetchall():
        if r["id"] in exclude:
            continue
        if why := discover.title_filter(r["title"], sector.get(r["company"], "other")):  # settings changed since
            db.set_job(con, r["id"], status="skipped")
            db.event(con, r["id"], "skip", f"no longer wanted: {why}")
            log.info("skip (settings changed) %s: %s (%s)", r["company"], r["title"][:60], why)
            continue
        queued.append(r)
    queued.sort(key=lambda r: -(r["score"] or 0))
    first = [r for r in queued if prio_of(r)] if prio else queued
    rest = [r for r in queued if not prio_of(r)] if prio else []
    out, per = [], {}
    for group in (approved, first):
        for r in _eligible(con, group, out, per, caps, prio_of, blocked, applied):
            if len(out) >= budget:
                return out
            out.append(r)
    if rest and (not strict or not any(prio_of(r) for r in out if r["status"] == "queued")):
        for r in _eligible(con, rest, out, per, caps, prio_of, blocked, applied):
            if len(out) >= budget:
                break
            out.append(r)
    return out


_awake = False


def keep_awake() -> None:
    """Stop idle sleep until this process exits (closing the lid still sleeps)."""
    global _awake
    if not _awake and config.settings()["schedule"].get("keep_awake_during_run"):
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _awake = True


OFFLINE_RE = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|ERR_NETWORK_CHANGED|ERR_NETWORK_IO_SUSPENDED|"
                        r"ERR_ADDRESS_UNREACHABLE|NameResolutionError|nodename nor servname|getaddrinfo|ENOTFOUND|"
                        r"Network is unreachable|Temporary failure in name resolution", re.I)


class Offline(Exception):
    """The connection dropped (e.g. the Mac went back to sleep): stop, and let the next check resume the run."""


def _asleep() -> bool:
    """Lid closed with nothing keeping the Mac awake. launchd can start jobs during the Mac's brief background
    wakes while it sleeps; those have no reliable internet and end within a minute, so a run must not start."""
    try:
        out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return '"AppleClamshellState" = Yes' in out and '"AppleClamshellCausesSleep" = No' not in out


def _online(wait_s: int = 120) -> bool:
    """Can we reach Workday? Waits up to wait_s (Wi-Fi takes a moment to reconnect after waking)."""
    hosts = [urlparse(c["url"]).hostname for c in config.companies()[:2]] + ["www.myworkday.com"]
    deadline = time.monotonic() + wait_s
    while True:
        for h in filter(None, hosts):
            try:
                socket.create_connection((h, 443), timeout=5).close()
                return True
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)


def _due(s) -> bool:
    hh, mm = (int(x) for x in str(s["schedule"].get("daily_run_time") or "12:00").split(":"))
    now = datetime.now()
    return (now.hour, now.minute) >= (hh, mm)


def apply_one(con, job, mode: str | None = None, headed=None) -> dict:
    keep_awake()
    s = config.settings()
    mode = mode or ("auto" if job["status"] == "approved" else s["mode"])
    prev, attempts = job["status"], (job["attempts"] or 0) + 1
    db.set_job(con, job["id"], status="applying", attempts=attempts)
    db.event(con, job["id"], "apply_start", {"mode": mode})
    try:
        prior = json.loads(job["result"]) if job["result"] else {}
        res = apply.apply_job(con, job, mode, headed=headed, fast=(prev == "approved"),
                              notes=prior.get("review_problems") or None)
    except claude.UsageLimited:
        db.set_job(con, job["id"], status=prev, attempts=attempts - 1)
        raise
    except Exception as e:
        log.error("apply crashed for %s:\n%s", job["id"], traceback.format_exc())
        res = {"status": "failed", "reason": f"{type(e).__name__}: {e}"[:400], "run_dir": ""}
    if res["status"] == "failed" and OFFLINE_RE.search(res.get("reason") or "") and not _online(wait_s=20):
        db.set_job(con, job["id"], status=prev, attempts=attempts - 1)  # not this posting's fault: retry it
        db.event(con, job["id"], "apply_end", {"status": "offline", "reason": res["reason"]})
        raise Offline(res["reason"])
    status = res["status"]
    fields = {"status": status, "result": res, "run_dir": res.get("run_dir", "")}
    if status in ("submitted", "ready_for_review"):
        fields["applied_at"] = db.now()
    if status == "failed" and attempts < s["limits"]["max_attempts_per_job"]:
        fields["status"] = prev if prev in ("queued", "approved") else "queued"
    if status == "needs_human" and res.get("review_problems") and attempts < s["limits"]["max_attempts_per_job"]:
        fields["status"] = "queued"  # retry next run with the final check's notes; nothing was submitted
    if status == "needs_answer":
        for q in res.get("questions") or [res["reason"]]:
            con.execute("INSERT INTO questions(job_id,question,context,created_at) VALUES(?,?,?,?)",
                        (job["id"], q, res.get("context", ""), db.now()))
    db.set_job(con, job["id"], **fields)
    db.event(con, job["id"], "apply_end", res)
    if fields["status"] in ("submitted", "ready_for_review", "already_applied"):
        tracker.record(job, fields["status"], "workday" if status == "already_applied" else "agent",
                       "" if status == "submitted" else res.get("reason", ""))
    label = f"{job['company']}: {job['title'][:60]}"
    msg = {"submitted": "Application submitted", "ready_for_review": "Ready for your review",
           "needs_answer": "Needs your answer", "needs_human": "Needs your attention"}.get(status)
    if msg:
        notify.notify(msg, label)
    log.info("RESULT %-16s %s — %s", status, label, res["reason"][:160])
    return res


def send_digest(con, run_started: str | None = None) -> None:
    """A summary of THIS run (not a rolling 24 hours), plus all-time totals."""
    if not config.settings()["notifications"].get("daily_digest_email"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if db.kv_get(con, "last_digest") == today:
        return
    since = run_started or _midnight_utc()
    rows = con.execute("SELECT j.*, e.detail FROM events e JOIN jobs j ON j.id = e.job_id "
                       "WHERE e.kind='apply_end' AND e.ts >= ? ORDER BY e.ts", (since,)).fetchall()
    groups = {"Submitted": [], "Waiting for your approval": [], "Needs your answer": [], "Needs your attention": [],
              "Held, will retry automatically": [], "Already applied (by you)": [], "Other": []}
    for r in rows:
        res = json.loads(r["detail"] or "{}")
        st, now_status = res.get("status"), r["status"]
        key = {"submitted": "Submitted", "ready_for_review": "Waiting for your approval",
               "needs_answer": "Needs your answer", "already_applied": "Already applied (by you)"}.get(st)
        if st == "needs_human":
            key = "Held, will retry automatically" if now_status == "queued" else "Needs your attention"
        groups[key or "Other"].append((r, res.get("reason", "")))
    parts = ["<h3>Internship Agent — this run</h3>"]
    for title, items in groups.items():
        if items:
            lis = "".join(f"<li>{r['company']}: <a href='{r['url']}'>{r['title']}</a>"
                          + (f" <i>({reason[:140]})</i>" if title not in ("Submitted", "Already applied (by you)") else "")
                          + "</li>" for r, reason in items)
            parts.append(f"<p><b>{title} ({len(items)})</b></p><ul>{lis}</ul>")
    if not rows:
        parts.append("<p>No applications today.</p>")
    total = con.execute("SELECT COUNT(*) FROM jobs WHERE status='submitted'").fetchone()[0]
    queued = con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    qs = con.execute("SELECT COUNT(*) FROM questions WHERE status='pending'").fetchone()[0]
    day = applied_today(con)
    parts.append(f"<p>Today so far (all runs): <b>{day}</b> · all-time submitted: <b>{total}</b> · still queued: {queued}"
                 + (f" · <b>{qs} question(s)</b> for you: run <code>./agent.sh answer</code>" if qs else "")
                 + ". Full record: applications.csv and Dashboard.html.</p>")
    try:
        run_n = len(groups["Submitted"])
        mail.send(f"Internship Agent: {run_n} submitted this run ({day} today)" if run_n != day
                  else f"Internship Agent: {day} submitted today", "".join(parts))
        db.kv_set(con, "last_digest", today)
    except Exception as e:
        log.warning("digest e-mail failed: %s", e)


def selfcheck() -> dict:
    """Cheap checks that the background environment works (no Claude tokens used)."""
    out = {"time": db.now(), "desktop_files": True}
    try:
        out["keychain"] = {"gmail": bool(secrets.gmail_app_password()), "workday_common": bool(secrets.common_workday_password())}
    except Exception as e:
        out["keychain"] = f"error: {e}"
    try:
        r = subprocess.run([config.CLAUDE_BIN, "auth", "status"], capture_output=True, text=True, timeout=60)
        out["claude_logged_in"] = '"loggedIn": true' in r.stdout
    except Exception as e:
        out["claude_logged_in"] = f"error: {e}"
    return out


def daily_limit() -> float:
    """limits.max_applications_per_day: a number, 0 (scan and rank only), or unlimited/null (no daily cap)."""
    v = config.settings()["limits"].get("max_applications_per_day")
    if v is None or str(v).strip().lower() in ("", "none", "null", "unlimited", "no limit"):
        return float("inf")
    return max(0, int(v))


def run_cycle(headed=None, force: bool = False, count: int | None = None) -> None:
    lock = _lock()
    if lock is None:
        log.info("another run is still in progress; skipping")
        return
    hb = config.DATA / "heartbeat.json"
    # launchd checks in at login, at schedule.daily_run_time and every 30 minutes. The day's run starts at the
    # first check after the run time when the Mac is really awake and online; the other checks end right here.
    hb.write_text(json.dumps(selfcheck() if not hb.exists() else {"time": db.now(), "desktop_files": True}, indent=1))
    con = db.connect()
    s = config.settings()
    today = datetime.now().strftime("%Y-%m-%d")
    last_run = db.kv_get(con, "last_run_date")
    if not force and (last_run == today or not _due(s)):
        return
    if db.kv_get(con, "paused"):
        if force or db.kv_get(con, "paused_noted") != today:
            db.kv_set(con, "paused_noted", today)
            log.info("paused (./agent.sh resume to continue)")
        return
    if not force and _asleep():
        log.info("the Mac is asleep (lid closed); today's run starts at the next check after it wakes")
        return
    if not _online():
        log.warning("no internet connection; today's run starts at the next check (every 30 min)")
        return
    run_started = db.now()
    check = selfcheck()
    hb.write_text(json.dumps(check, indent=1))
    db.kv_set(con, "last_run_date", today)
    offline = False
    keep_awake()
    tracker.sync_from_db(con)
    con.execute("UPDATE jobs SET status='queued' WHERE status='applying' AND updated_at < ?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds"),))
    if config.doc_path("transcript"):  # a transcript was added: retry jobs that were waiting for one
        waiting = [r[0] for r in con.execute(
            "SELECT job_id FROM questions WHERE status='pending' AND question LIKE '%transcript%'")]
        for jid in waiting:
            con.execute("UPDATE jobs SET status='queued' WHERE id=? AND status='needs_answer'", (jid,))
        con.execute("UPDATE questions SET status='answered', answer='transcript added' "
                    "WHERE status='pending' AND question LIKE '%transcript%'")
    con.commit()
    try:
        found = discover.run_discovery(con)
        log.info("discovery: %s", found)
        if found.get("sites_failed") and not found.get("sites_ok") and not _online(wait_s=20):
            raise Offline("no company site could be reached")
        if not claude.cooldown_active(con):
            log.info("triage: %s", triage.run_triage(con, max_jobs=400))
        # Keep going until today's quota is actually SUBMITTED: postings you already applied to, held ones and
        # errors don't use up a slot. A ceiling on attempts keeps Claude usage bounded.
        # The quota is the user's: limits.max_applications_per_day, or `--count N` for a one-off run.
        cap = (applied_today(con) + count) if count is not None else daily_limit()
        remaining = cap - applied_today(con)
        default_attempts = 200 if remaining == float("inf") else int(2 * max(remaining, 0))
        max_attempts = s["limits"].get("max_attempts_per_run") or default_attempts
        attempts, tried = 0, set()
        while attempts < max_attempts and applied_today(con) < cap and not claude.cooldown_active(con):
            left = cap - applied_today(con)
            batch = pick_jobs(con, 10_000 if left == float("inf") else int(left), exclude=tried)
            if not batch:
                break
            for job in batch:
                if attempts >= max_attempts or applied_today(con) >= cap:
                    break
                tried.add(job["id"])
                attempts += 1
                apply_one(con, job, headed=headed)
        log.info("today: %d submitted/ready (limit %s), %d attempts", applied_today(con),
                 "unlimited" if cap == float("inf") else int(cap), attempts)
    except claude.UsageLimited as e:
        log.warning("Claude usage limit reached; the rest waits for the next run (limit resets %s)", e.reset_at)
    except Offline as e:
        offline = True
        db.kv_set(con, "last_run_date", last_run)  # not done: the next check (within 30 min) picks it up again
        log.warning("lost the internet connection (%s); the run resumes at the next check", str(e)[:120])
    finally:
        dashboard.render(con)
        if not offline:
            send_digest(con, run_started)
        check["finished"] = db.now()
        hb.write_text(json.dumps(check, indent=1))
