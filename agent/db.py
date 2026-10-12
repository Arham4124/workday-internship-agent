"""SQLite state: jobs, accounts, questions, events, key/value state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  company TEXT, host TEXT, tenant TEXT, site TEXT,
  title TEXT, location TEXT, country TEXT, url TEXT, external_path TEXT,
  posted_on TEXT, req_id TEXT, description TEXT,
  status TEXT DEFAULT 'new',
  score INTEGER, triage TEXT, pay TEXT,
  attempts INTEGER DEFAULT 0, result TEXT, run_dir TEXT,
  first_seen TEXT, updated_at TEXT, applied_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS accounts (
  tenant TEXT PRIMARY KEY, host TEXT, email TEXT,
  password_source TEXT, created_at TEXT, verified INTEGER DEFAULT 0, last_ok TEXT
);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT, question TEXT, context TEXT, status TEXT DEFAULT 'pending',
  answer TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sites (
  url TEXT PRIMARY KEY, name TEXT, ok INTEGER, last_checked TEXT, last_error TEXT,
  intern_facet TEXT, jobs_seen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT, job_id TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS usage (
  ts TEXT, purpose TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cache_read INTEGER, cache_write INTEGER, cost_usd REAL, ok INTEGER
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def kv_get(con, key, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def kv_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (key, json.dumps(value)))
    con.commit()


def event(con, job_id, kind, detail=""):
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), job_id, kind, detail[:4000]))
    con.commit()


def set_job(con, job_id, **fields):
    fields["updated_at"] = now()
    for k, v in list(fields.items()):
        if isinstance(v, (dict, list)):
            fields[k] = json.dumps(v, default=str)
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    con.commit()


def job(con, job_id):
    return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def record_usage(con, purpose, model, usage: dict, cost, ok):
    con.execute(
        "INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?)",
        (now(), purpose, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
         usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0),
         cost or 0.0, int(ok)),
    )
    con.commit()
