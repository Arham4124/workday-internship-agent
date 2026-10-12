"""macOS notifications (no personal details in them)."""
from __future__ import annotations

import subprocess

from . import config


def notify(title: str, message: str) -> None:
    if not config.settings().get("notifications", {}).get("macos", True):
        return
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')[:200]
    script = f'display notification "{esc(message)}" with title "Internship Agent" subtitle "{esc(title)}"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=10, capture_output=True)
    except Exception:
        pass
