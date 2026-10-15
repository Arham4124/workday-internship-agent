"""Install / remove the once-a-day background job (a macOS LaunchAgent for this user only)."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from pathlib import Path

from . import config

LOG_DIR = Path.home() / "Library/Logs/InternshipAgent"


def _label() -> str:
    return config.settings()["schedule"].get("launchd_label") or "com.internship-agent.daily"


def _plist() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{_label()}.plist"


def install() -> None:
    hh, mm = (int(x) for x in config.settings()["schedule"]["daily_run_time"].split(":"))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LABEL, PLIST = _label(), _plist()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "Label": LABEL,
        "ProgramArguments": [str(config.ROOT / ".venv/bin/python"), "-m", "agent", "cycle"],
        "WorkingDirectory": str(config.ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path.home()}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            "HOME": str(Path.home()), "PYTHONUNBUFFERED": "1"},
        "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        # Also check at login and every 30 min: if the Mac was asleep, closed or offline at the run time, the
        # day's run starts at the first check when it is awake and online (it still runs only once a day).
        "StartInterval": 1800,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
        "Nice": 5,
    }
    with open(PLIST, "wb") as f:
        plistlib.dump(spec, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    hb = config.DATA / "heartbeat.json"
    hb.unlink(missing_ok=True)  # the first check after installing writes a full background check here
    before = 0
    r = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        print("launchctl bootstrap failed:", r.stderr.strip())
        return
    print(f"Installed: runs once a day at {hh:02d}:{mm:02d}, or at the first check after that (every 30 min)"
          " when the Mac is awake and online.")
    for _ in range(45):  # RunAtLoad starts it now; confirm it can reach the Desktop folder
        time.sleep(2)
        if hb.exists() and hb.stat().st_mtime > before:
            print("Background check:", json.dumps(json.loads(hb.read_text())))
            return
    log = (LOG_DIR / "launchd.log").read_text()[-1500:] if (LOG_DIR / "launchd.log").exists() else ""
    print("The background job did not report in within 90 s.")
    if "Operation not permitted" in log:
        print("macOS blocked background access to the Desktop folder (privacy protection).")
    print("Log tail:\n" + log)


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{_label()}"], capture_output=True)
    _plist().unlink(missing_ok=True)
    print("Removed the background job.")
