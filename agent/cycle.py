"""The daily run: scan sites -> rank new postings -> apply within limits -> dashboard -> digest."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import traceback
from datetime import datetime, timedelta, timezone

from . import apply, claude, config, dashboard, db, discover, mail, notify, secrets, triage

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


def _eligible(con, rows, out, per, caps, prio_of, blocked):
    """Yield rows that pass the transcript block, duplicate-role and per-company checks."""
    for r in rows:
        if r["company"] in blocked:
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


def pick_jobs(con, budget: int) -> list:
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
    approved = con.execute("SELECT * FROM jobs WHERE status='approved' ORDER BY score DESC").fetchall()
    queued = sorted(con.execute("SELECT * FROM jobs WHERE status='queued'").fetchall(), key=lambda r: -(r["score"] or 0))
    first = [r for r in queued if prio_of(r)] if prio else queued
    rest = [r for r in queued if not prio_of(r)] if prio else []
    out, per = [], {}
    for group in (approved, first):
        for r in _eligible(con, group, out, per, caps, prio_of, blocked):
            if len(out) >= budget:
                return out
            out.append(r)
    if rest and (not strict or not any(prio_of(r) for r in out if r["status"] == "queued")):
        for r in _eligible(con, rest, out, per, caps, prio_of, blocked):
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
    label = f"{job['company']}: {job['title'][:60]}"
    msg = {"submitted": "Application submitted", "ready_for_review": "Ready for your review",
           "needs_answer": "Needs your answer", "needs_human": "Needs your attention"}.get(status)
    if msg:
        notify.notify(msg, label)
    log.info("RESULT %-16s %s — %s", status, label, res["reason"][:160])
    return res


def send_digest(con) -> None:
    if not config.settings()["notifications"].get("daily_digest_email"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if db.kv_get(con, "last_digest") == today:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = con.execute("SELECT * FROM jobs WHERE updated_at >= ? AND status IN "
                       "('submitted','ready_for_review','needs_answer','needs_human') ORDER BY status", (since,)).fetchall()
    qs = con.execute("SELECT question FROM questions WHERE status='pending'").fetchall()
    queued = con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    items = "".join(f"<li><b>{r['status'].replace('_', ' ')}</b> — {r['company']}: "
                    f"<a href='{r['url']}'>{r['title']}</a></li>" for r in rows)
    body = (f"<h3>Internship Agent — today's run</h3><ul>{items or '<li>No new activity.</li>'}</ul>"
            + (f"<p><b>{len(qs)} question(s) need your answer</b> — run <code>./agent.sh answer</code>.</p>" if qs else "")
            + f"<p>Still queued: {queued}. Full details: Dashboard.html in the InternshipAgent folder.</p>")
    try:
        mail.send("Internship Agent daily summary", body)
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


def run_cycle(headed=None, force: bool = False) -> None:
    lock = _lock()
    if lock is None:
        log.info("another run is still in progress; skipping")
        return
    hb = config.DATA / "heartbeat.json"
    check = selfcheck()
    hb.write_text(json.dumps(check, indent=1))
    con = db.connect()
    s = config.settings()
    today = datetime.now().strftime("%Y-%m-%d")
    if db.kv_get(con, "paused"):
        log.info("paused (./agent.sh resume to continue)")
        return
    if not force and db.kv_get(con, "last_run_date") == today:
        log.info("today's run already happened; nothing to do")
        return
    db.kv_set(con, "last_run_date", today)
    keep_awake()
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
        log.info("discovery: %s", discover.run_discovery(con))
        if not claude.cooldown_active(con):
            log.info("triage: %s", triage.run_triage(con, max_jobs=400))
        budget = s["limits"]["max_applications_per_day"] - applied_today(con)
        if budget > 0 and not claude.cooldown_active(con):
            for job in pick_jobs(con, budget):
                apply_one(con, job, headed=headed)
    except claude.UsageLimited as e:
        log.warning("Claude usage limit reached; the rest waits for the next run (limit resets %s)", e.reset_at)
    finally:
        dashboard.render(con)
        send_digest(con)
        check["finished"] = db.now()
        hb.write_text(json.dumps(check, indent=1))
