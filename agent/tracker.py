"""applications.csv — a plain record of every application, and the agent's duplicate guard.

The agent adds or updates a row whenever it submits, stops at the Review page, or finds that you already
applied. You can add rows yourself (e.g. applications you sent by hand) with status `applied_manually`:
the agent never applies to a posting that matches a row by URL, by requisition ID (re-posts such as
R1234-1 count as R1234), or by the same company + same job title.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime

from . import config

PATH = config.ROOT / "applications.csv"
FIELDS = ["date", "company", "title", "req_id", "location", "status", "source", "url", "job_id", "notes"]
# Statuses that mean "do not apply to this role again".
BLOCKING = {"submitted", "ready_for_review", "approved", "already_applied", "applied_manually", "applied"}


def _norm(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def _url(u) -> str:
    u = str(u or "").strip().lower().split("?")[0].rstrip("/")
    return re.sub(r"/[a-z]{2}-[a-z]{2}/", "/", u)  # drop locale segments such as /en-US/


def base_req(req, url="") -> str:
    """Requisition ID without re-post suffixes; taken from the URL (…_R0054512) when not given."""
    r = str(req or "").strip().upper()
    if not r and url:
        m = re.search(r"_([A-Z]{0,4}-?\d{3,}(?:-\d+)?)$", str(url).split("?")[0].rstrip("/"), re.I)
        r = m.group(1).upper() if m else ""
    return re.sub(r"-\d{1,2}$", "", r)


def _local(iso) -> str | None:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


def load() -> list[dict]:
    if not PATH.exists():
        return []
    with open(PATH, newline="", encoding="utf-8") as f:
        return [{k: (v or "") for k, v in r.items() if k} for r in csv.DictReader(f)]


def _write(rows: list[dict]) -> None:
    tmp = PATH.with_name(PATH.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, PATH)
    os.chmod(PATH, 0o600)


def _row(job, status: str, source: str, notes: str, date: str | None = None) -> dict:
    return {"date": date or datetime.now().strftime("%Y-%m-%d %H:%M"), "company": job["company"],
            "title": job["title"], "req_id": job["req_id"] or "", "location": job["location"] or "",
            "status": status, "source": source, "url": job["url"] or "", "job_id": job["id"],
            "notes": (notes or "")[:200]}


def record(job, status: str, source: str = "agent", notes: str = "") -> None:
    """Add or update this posting's row (matched by job_id)."""
    rows = load()
    for i, r in enumerate(rows):
        if r.get("job_id") == job["id"]:
            rows[i] = _row(job, status, source, notes or r.get("notes", ""))
            break
    else:
        rows.append(_row(job, status, source, notes))
    _write(rows)


def duplicate_of(job, rows: list[dict] | None = None) -> str | None:
    """Why this posting counts as already applied (from applications.csv), or None."""
    rows = load() if rows is None else rows
    comp, title, url = _norm(job["company"]), _norm(job["title"]), _url(job["url"])
    req = base_req(job["req_id"], job["url"])
    for r in rows:
        if r.get("status", "").strip().lower() not in BLOCKING or (r.get("job_id") and r.get("job_id") == job["id"]):
            continue
        when = f"{r.get('status')} {r.get('date', '')}".strip()
        if url and _url(r.get("url")) == url:
            return f"same posting already {when}"
        if _norm(r.get("company")) != comp:
            continue
        if req and base_req(r.get("req_id"), r.get("url")) == req:
            return f"same requisition {req} already {when}"
        if title and _norm(r.get("title")) == title:
            return f"same role already {when}"
    return None


def sync_from_db(con) -> int:
    """Make sure every application in the database has a row (e.g. after an upgrade). Returns rows added."""
    rows = load()
    have = {r.get("job_id") for r in rows}
    added = 0
    for j in con.execute("SELECT * FROM jobs WHERE status IN "
                         "('submitted','ready_for_review','approved','already_applied')"):
        if j["id"] in have:
            continue
        source = "workday" if j["status"] == "already_applied" else "agent"
        rows.append(_row(j, j["status"], source, "", _local(j["applied_at"] or j["updated_at"])))
        added += 1
    if added or not PATH.exists():
        _write(rows)
    return added
