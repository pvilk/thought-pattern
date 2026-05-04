"""Wispr Thoughts launchd schedule helpers.

Renders scripts/wispr-thoughts.plist.template into
~/Library/LaunchAgents/io.wisprthoughts.weekly.plist and registers it via
launchctl. Used by the settings drawer (serve.py /api/schedule) and any
manual install path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "scripts" / "wispr-thoughts.plist.template"
LABEL = "io.wisprthoughts.weekly"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"

WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _uid() -> int:
    return os.getuid()


def _domain() -> str:
    return f"gui/{_uid()}"


def _service_target() -> str:
    return f"{_domain()}/{LABEL}"


def render_plist(weekday: int, hour: int, minute: int) -> str:
    """Render the template with the given schedule. Returns plist XML."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Missing template: {TEMPLATE_PATH}")
    tmpl = TEMPLATE_PATH.read_text()
    return (
        tmpl
        .replace("{{LABEL}}", LABEL)
        .replace("{{INSTALL_PATH}}", str(ROOT))
        .replace("{{HOME}}", str(Path.home()))
        .replace("{{PYTHON}}", sys.executable)
        .replace("{{WEEKDAY}}", str(weekday))
        .replace("{{HOUR}}", str(hour))
        .replace("{{MINUTE}}", str(minute))
    )


def is_installed() -> bool:
    return PLIST_PATH.exists()


def install(weekday: int, hour: int, minute: int) -> dict:
    """Write the plist and load it via launchctl bootstrap.

    Idempotent: if already installed, bootout first then bootstrap fresh so the
    new schedule takes effect. Returns {ok: bool, output: str}.
    """
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(render_plist(weekday, hour, minute))

    # Bootout first if loaded; ignore failures (might not be loaded yet).
    subprocess.run(
        ["launchctl", "bootout", _service_target()],
        capture_output=True, text=True,
    )

    r = subprocess.run(
        ["launchctl", "bootstrap", _domain(), str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "output": (r.stdout + r.stderr).strip() or "launchctl bootstrap failed"}

    # Make sure the agent isn't disabled by a prior `launchctl disable` call.
    subprocess.run(
        ["launchctl", "enable", _service_target()],
        capture_output=True, text=True,
    )
    return {"ok": True, "output": "installed"}


def remove() -> dict:
    """Bootout and delete the plist."""
    output_lines = []
    if PLIST_PATH.exists():
        r = subprocess.run(
            ["launchctl", "bootout", _service_target()],
            capture_output=True, text=True,
        )
        output_lines.append((r.stdout + r.stderr).strip())
        try:
            PLIST_PATH.unlink()
        except OSError as e:
            return {"ok": False, "output": f"failed to delete plist: {e}"}
    return {"ok": True, "output": "\n".join([s for s in output_lines if s]) or "removed"}


def _next_run(weekday: int, hour: int, minute: int) -> str:
    """Compute the next launch time as ISO8601 in UTC. Naive: doesn't account
    for missed firings while the Mac was asleep, but launchd handles those."""
    now = datetime.now()
    days_ahead = (weekday - now.isoweekday() % 7) % 7
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if target <= now:
        target = target + timedelta(days=7)
    # Convert local-naive to UTC-aware via system time
    return target.astimezone(timezone.utc).isoformat()


def status(weekday: int, hour: int, minute: int) -> dict:
    """Return current state visible to the UI."""
    info: dict = {
        "installed": is_installed(),
        "label": LABEL,
        "plist_path": str(PLIST_PATH),
        "weekday": weekday,
        "weekday_name": WEEKDAY_NAMES[weekday] if 0 <= weekday < 7 else str(weekday),
        "hour": hour,
        "minute": minute,
        "next_run_iso": None,
        "last_run_iso": None,
        "last_exit_code": None,
    }
    if info["installed"]:
        info["next_run_iso"] = _next_run(weekday, hour, minute)

        # Parse `launchctl print` for last exit + last run timestamp
        r = subprocess.run(
            ["launchctl", "print", _service_target()],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("last exit code"):
                    parts = line.split("=")
                    if len(parts) > 1:
                        try:
                            info["last_exit_code"] = int(parts[1].strip())
                        except ValueError:
                            pass

        # last_run_iso from log file mtime as a stand-in (launchctl print
        # doesn't expose last-run-time on modern macOS)
        log = Path.home() / "Library" / "Logs" / "wispr-thoughts.out.log"
        if log.exists():
            try:
                ts = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
                info["last_run_iso"] = ts.isoformat()
            except OSError:
                pass

    return info
