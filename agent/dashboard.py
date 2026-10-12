"""Writes Dashboard.html: ranked internships, statuses, and anything needing your attention."""
from __future__ import annotations

import html
import json
from datetime import datetime

from . import config, db

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e4e4e0;--accent:#2f6fde;--warn:#b25b00;--ok:#1f7a3f}
@media (prefers-color-scheme: dark){:root{--bg:#161617;--fg:#ececee;--muted:#9a9aa1;--card:#1f1f21;--line:#333336;--accent:#7aa7ff;--warn:#f0a050;--ok:#5cc489}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)} .tiles{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}
.tile b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
a{color:var(--accent);text-decoration:none} .warn{color:var(--warn)} .ok{color:var(--ok)}
.wrap{overflow-x:auto}
"""


def _rows(con, where, order="score DESC", limit=200):
    return con.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT {limit}").fetchall()


def _table(rows, cols):
    if not rows:
        return '<p class="muted">Nothing here yet.</p>'
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    body = "".join("<tr>" + "".join(f"<td>{fn(r)}</td>" for _, fn in cols) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def _reason(r):
    try:
        t = json.loads(r["triage"] or "{}")
    except json.JSONDecodeError:
        t = {}
    return html.escape(t.get("reason", ""))


def _result(r):
    try:
        return html.escape((json.loads(r["result"] or "{}") or {}).get("reason", ""))
    except json.JSONDecodeError:
        return html.escape(r["result"] or "")


def render(con) -> None:
    e = html.escape
    link = lambda r: f'<a href="{e(r["url"])}">{e(r["title"])}</a>'
    count = lambda w: con.execute(f"SELECT COUNT(*) FROM jobs WHERE {w}").fetchone()[0]
    today = datetime.now().strftime("%Y-%m-%d")
    tiles = [("Submitted", count("status='submitted'")), ("Ready for review", count("status='ready_for_review'")),
             ("Queued", count("status='queued'")), ("Need you", count("status IN ('needs_human','needs_answer')")),
             ("Found (relevant)", count("status NOT IN ('filtered','closed')")), ("Scanned", count("1=1"))]
    qs = con.execute("SELECT * FROM questions WHERE status='pending' ORDER BY id").fetchall()
    sites_bad = con.execute("SELECT name,last_error FROM sites WHERE ok=0").fetchall()
    parts = [f"<main><h1>Internship Agent</h1><div class='muted'>Updated {datetime.now():%b %d, %I:%M %p} · "
             f"mode: <b>{e(config.settings()['mode'])}</b> · last scan: {e(str(db.kv_get(con, 'last_discovery', 'never')))}</div>",
             "<div class='tiles'>" + "".join(f"<div class='tile'><b>{v}</b>{k}</div>" for k, v in tiles) + "</div>"]
    if qs:
        parts.append("<h2 class='warn'>Questions only you can answer</h2><p class='muted'>Run <code>./agent.sh answer</code>.</p>")
        parts.append(_table(qs, [("Question", lambda q: e(q["question"])), ("Job", lambda q: e(q["job_id"].split("|")[0]))]))
    parts.append("<h2>Needs your attention</h2>")
    parts.append(_table(_rows(con, "status IN ('needs_human','needs_answer','ready_for_review')", "updated_at DESC"),
                        [("Company", lambda r: e(r["company"])), ("Role", link), ("Status", lambda r: e(r["status"])),
                         ("Details", _result)]))
    parts.append("<h2>Submitted</h2>")
    parts.append(_table(_rows(con, "status='submitted'", "applied_at DESC"),
                        [("When", lambda r: e((r["applied_at"] or "")[:16])), ("Company", lambda r: e(r["company"])),
                         ("Role", link), ("Score", lambda r: r["score"] or "")]))
    parts.append("<h2>Queue (best matches first)</h2>")
    parts.append(_table(_rows(con, "status='queued'"),
                        [("Score", lambda r: r["score"] or ""), ("Company", lambda r: e(r["company"])), ("Role", link),
                         ("Location", lambda r: e(r["location"] or "")), ("Why", _reason)]))
    parts.append("<h2>Skipped by Claude</h2>")
    parts.append(_table(_rows(con, "status='skipped'", "score DESC", 80),
                        [("Score", lambda r: r["score"] or ""), ("Company", lambda r: e(r["company"])), ("Role", link), ("Why", _reason)]))
    if sites_bad:
        parts.append("<h2 class='muted'>Career sites that failed to load</h2>")
        parts.append(_table(sites_bad, [("Company", lambda s: e(s["name"])), ("Error", lambda s: e((s["last_error"] or "")[:160]))]))
    parts.append(f"<p class='muted'>Generated {today}. Data stays on this Mac.</p></main>")
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta http-equiv='refresh' content='300'><title>Internship Agent</title>"
           f"<style>{CSS}</style></head><body>{''.join(parts)}</body></html>")
    config.DASHBOARD.write_text(doc)
