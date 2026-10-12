"""Calls to Claude through the Claude Code CLI, signed in with your claude.ai account.

Every call runs with NO tools (--tools ""), so text from job postings can never make
Claude run commands; Claude only returns JSON that this program validates and applies.
The prompt goes over stdin (never on the command line).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone

from . import config, db

log = logging.getLogger("agent.claude")


class UsageLimited(Exception):
    def __init__(self, msg: str, reset_at: datetime):
        super().__init__(msg)
        self.reset_at = reset_at


class ClaudeError(Exception):
    pass


LIMIT_RE = re.compile(r"usage limit|limit reached|rate limit|hit your limit|out of extra usage|quota", re.I)


def _reset_time(text: str) -> datetime:
    now = datetime.now(timezone.utc)
    if m := re.search(r"\|(\d{10})", text):  # "limit reached|1760000000"
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    return now + timedelta(hours=1)


def ask(prompt: str, *, system: str, schema: dict, model: str, purpose: str, timeout: int | None = None,
        effort: str | None = None) -> dict:
    """One stateless Claude call returning schema-validated JSON."""
    cfg = config.settings()["claude"]
    timeout = timeout or cfg["timeout_seconds"]
    effort = effort or cfg.get("effort", {}).get(purpose.split("-")[0], cfg.get("default_effort", "low"))
    t0 = time.time()
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir=config.CLAUDE_CWD, delete=False) as f:
        f.write(system)
        sys_path = f.name
    os.chmod(sys_path, 0o600)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["PATH"] = f"{os.path.dirname(config.CLAUDE_BIN)}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    cmd = [config.CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
           "--system-prompt-file", sys_path, "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--effort", effort, "--json-schema", json.dumps(schema)]
    con = db.connect()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                           cwd=config.CLAUDE_CWD, env=env)
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"Claude timed out after {timeout}s") from e
    finally:
        os.unlink(sys_path)
    out = p.stdout.strip()
    try:
        d = json.loads(out) if out else {}
    except json.JSONDecodeError:
        d = {"is_error": True, "result": out[-2000:]}
    text = f"{d.get('result', '')} {p.stderr[-1000:]}"
    ok = p.returncode == 0 and not d.get("is_error")
    db.record_usage(con, purpose, model, d.get("usage") or {}, d.get("total_cost_usd"), ok)
    log.info("claude %-13s %s/%s %.1fs in=%s out=%s", purpose, model, effort, time.time() - t0,
             (d.get("usage") or {}).get("cache_read_input_tokens", 0) + (d.get("usage") or {}).get("input_tokens", 0)
             + (d.get("usage") or {}).get("cache_creation_input_tokens", 0), (d.get("usage") or {}).get("output_tokens", 0))
    if not ok:
        if LIMIT_RE.search(text):
            reset = _reset_time(text)
            db.kv_set(con, "claude_cooldown_until", reset.isoformat())
            raise UsageLimited(text.strip()[:300], reset)
        raise ClaudeError(text.strip()[:500] or f"claude exited {p.returncode}")
    res = d.get("structured_output")
    if res is None:  # fall back to parsing the text result
        m = re.search(r"\{.*\}", d.get("result", ""), re.S)
        if not m:
            raise ClaudeError("no JSON in Claude response")
        res = json.loads(m.group(0))
    return res


def cooldown_active(con) -> datetime | None:
    v = db.kv_get(con, "claude_cooldown_until")
    if not v:
        return None
    t = datetime.fromisoformat(v)
    return t if t > datetime.now(timezone.utc) else None
