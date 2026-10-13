"""Paths and YAML config loading."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("INTERNSHIP_AGENT_HOME", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config"
DATA = ROOT / "data"
LOGS = DATA / "logs"
RUNS = DATA / "runs"
BROWSER_PROFILE = DATA / "browser"
CLAUDE_CWD = DATA / "claude_cwd"
PROMPTS = ROOT / "prompts"
DB_PATH = DATA / "agent.db"
LOCK_PATH = DATA / "cycle.lock"
DASHBOARD = ROOT / "Dashboard.html"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))

for _d in (DATA, LOGS, RUNS, BROWSER_PROFILE, CLAUDE_CWD):
    _d.mkdir(parents=True, exist_ok=True)


def _load(name: str):
    path = CONFIG / name
    if not path.exists():
        example = path.with_suffix(".example.yaml")
        raise SystemExit(f"{path} is missing. Run `./agent.sh init` (copies {example.name}), then fill it in "
                         f"- see QUESTIONNAIRE.md.")
    with open(path) as f:
        return yaml.safe_load(f)


def settings() -> dict:
    return _load("settings.yaml")


def profile() -> dict:
    return _load("profile.yaml")


def answers() -> list:
    return _load("answers.yaml") or []


def companies() -> list:
    return [c for c in (_load("companies.yaml") or []) if c.get("enabled", True)]


def append_answer(question: str, answer: str) -> None:
    items = answers()
    items.append({"question": question, "answer": answer})
    with open(CONFIG / "answers.yaml", "w") as f:
        f.write("# Answer bank (see header notes in git history / README).\n")
        yaml.safe_dump(items, f, sort_keys=False, allow_unicode=True, width=1000)


def doc_path(key: str) -> Path | None:
    rel = settings().get("documents", {}).get(key)
    if not rel:
        return None
    p = ROOT / rel
    return p if p.exists() else None


def profile_text(include_self_id: bool = False) -> str:
    """Profile as YAML text for prompts; self-ID data only when a page needs it."""
    p = dict(profile())
    if not include_self_id:
        p.pop("self_identification", None)
    return yaml.safe_dump(p, sort_keys=False, allow_unicode=True, width=1000)


def answers_text() -> str:
    return "\n".join(f"- Q: {a['question']}\n  A: {a['answer']}" for a in answers())


def set_setting(key: str, value) -> None:
    """Replace the value of `key:` in settings.yaml in place (keeps comments and layout)."""
    import re as _re
    path = CONFIG / "settings.yaml"
    text = path.read_text()
    new, n = _re.subn(rf"(?m)^(\s*{_re.escape(key)}:[ \t]*)([^#\n]*?)([ \t]*(#.*)?)$",
                      lambda m: f"{m.group(1)}{value}{m.group(3) if m.group(3).strip() else ''}", text, count=1)
    if not n:
        raise SystemExit(f"setting {key!r} not found in {path}")
    path.write_text(new)
