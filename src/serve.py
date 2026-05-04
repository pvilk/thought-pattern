#!/usr/bin/env python3
"""Wispr Thoughts viewer server.

Tiny stdlib HTTP server that serves data/viewer/ over http://127.0.0.1:8080
and exposes a JSON API for the sync pill in the viewer:

    GET  /                  -> data/viewer/index.html
    GET  /<path>            -> any static asset under data/viewer/
    GET  /api/status        -> { last_sync, freshness, syncing, port }
    POST /api/sync          -> { job_id } or 409 if already running
    GET  /api/sync/log      -> { job_id, status, lines, finished } for the most
                                recent job; viewer polls every 1s during a run
    GET  /api/settings      -> { email, schedule, sources, paths }
    POST /api/settings      -> { ok } applies a partial settings dict
    POST /api/settings/email/test -> { ok, error? } sends a test email
    GET  /api/schedule      -> { installed, weekday, hour, minute, next_run, ... }
    POST /api/schedule      -> { ok } installs/removes the launchd job
    GET  /api/backfill      -> { unthemed_count, weeks: list[str] }
    POST /api/backfill      -> { job_id } runs themes for every unthemed week
    GET  /api/delivery      -> { enabled, from_email, to_email, key_set }
    POST /api/delivery      -> save delivery config + (optional) Resend API key
    POST /api/delivery/test -> send a test email through the configured backend

Bound to 127.0.0.1 only. Never reachable from outside the laptop. No auth on
purpose; the network bind is the access boundary.

Usage:
    python3 src/serve.py                 # default port 8080
    python3 src/serve.py --port 8081
    THOUGHTPATTERN_PORT=8081 python3 src/serve.py

The server runs the existing weekly_email.py pipeline as the sync job; this
module never re-implements pipeline logic, it just orchestrates and streams
progress back to the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import _delivery
import _schedule
import _settings
from _config import load as load_config, resolve_path
from _delivery import keys as _delivery_keys

CFG = load_config()


def _hydrate_env_from_zshrc() -> None:
    """Pull `export FOO_API_KEY=...` lines from ~/.zshrc into our env on boot.

    serve.py launches as a child of whatever shell ran it, so it may or may
    not have the user's API keys depending on how the shell was invoked. The
    cron-side launchd job sources .zshrc via `zsh -lc`; we mirror that here
    so manually-triggered /api/sync runs see the same env.

    Only loads keys we don't already have. Whitelisted to *_API_KEY shapes so
    we don't leak unrelated env. Ignores quotes around the value.
    """
    import re as _re
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return
    pat = _re.compile(r"^\s*export\s+([A-Z][A-Z0-9_]*_API_KEY)\s*=\s*['\"]?(.+?)['\"]?\s*$")
    try:
        for line in zshrc.read_text().splitlines():
            m = pat.match(line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2)
    except OSError:
        pass


_hydrate_env_from_zshrc()
ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = ROOT / "data" / "viewer"
LOGS_DIR = resolve_path(CFG, "logs_dir")
PIPELINE_LOG = LOGS_DIR / "wispr-thoughts.log"

DEFAULT_PORT = int(os.environ.get("THOUGHTPATTERN_PORT", CFG.get("server", {}).get("port", 8080)))
HOST = "127.0.0.1"

# Freshness thresholds (hours since last successful pipeline run)
FRESH_HRS = 24
STALE_HRS = 72


# --- Job tracking ------------------------------------------------------------

# Single in-process slot for the current/last sync job. Concurrent syncs are
# refused with 409. A new sync replaces this dict.
_JOB_LOCK = threading.Lock()
_CURRENT_JOB: dict | None = None


def _new_job() -> dict:
    job_id = uuid.uuid4().hex[:12]
    log_path = LOGS_DIR / f"server-sync-{job_id}.log"
    return {
        "id": job_id,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "exit_code": None,
        "log_path": str(log_path),
    }


def _start_sync_thread(job: dict) -> None:
    """Run weekly_email.py in a thread, stream stdout+stderr into job's log file."""
    def runner():
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["python3", str(ROOT / "src" / "weekly_email.py")]
        with log_path.open("w") as fh:
            fh.write(f"# job {job['id']} started {job['started_at']}\n")
            fh.write(f"# command: {' '.join(cmd)}\n\n")
            fh.flush()
            # start_new_session=True so we get a process group; cancel can
            # then signal the whole tree (export_wispr child, LLM subprocs).
            proc = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, text=True,
                start_new_session=True,
            )
            with _JOB_LOCK:
                job["pid"] = proc.pid
            proc.wait()
            fh.write(f"\n# job exited with code {proc.returncode}\n")
        with _JOB_LOCK:
            if job["status"] == "running":
                job["status"] = "completed" if proc.returncode == 0 else "failed"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            job["exit_code"] = proc.returncode

    t = threading.Thread(target=runner, daemon=True)
    t.start()


# --- Freshness ---------------------------------------------------------------


def _last_pipeline_run_at() -> datetime | None:
    """Most recent successful pipeline-run timestamp.

    weekly_email.py writes timestamped lines like
    '[2026-04-30 08:14:32] === wispr-thoughts 2026-W17 ===' to the rolling log.
    If that log isn't present yet (e.g., user only ran build_themes.py
    standalone), fall back to the most recent mtime among themed week files,
    which only get touched when the theming pipeline runs successfully.
    """
    last_ts: datetime | None = None

    # Path 1: parse the rolling pipeline log
    if PIPELINE_LOG.exists():
        try:
            with PIPELINE_LOG.open() as fh:
                for line in fh:
                    if line.startswith("[") and "]" in line[:25]:
                        raw = line[1:line.index("]")]
                        try:
                            ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                            last_ts = ts.replace(tzinfo=timezone.utc)
                        except ValueError:
                            continue
        except OSError:
            pass

    # Path 2: fall back to themed-week mtimes
    if last_ts is None:
        candidates: list[float] = []
        for d in (
            resolve_path(CFG, "weeks_dir"),
            resolve_path(CFG, "master_dir") / "50_weeks",
            resolve_path(CFG, "digests_dir"),
        ):
            if d.is_dir():
                for p in d.glob("*.md"):
                    try:
                        candidates.append(p.stat().st_mtime)
                    except OSError:
                        continue
        if candidates:
            last_ts = datetime.fromtimestamp(max(candidates), tz=timezone.utc)

    return last_ts


def _freshness() -> tuple[str, str | None]:
    """Return (label, iso_timestamp_or_None). Labels: fresh, stale, very-stale, never."""
    ts = _last_pipeline_run_at()
    if ts is None:
        return "never", None
    age_hrs = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if age_hrs < FRESH_HRS:
        return "fresh", ts.isoformat()
    if age_hrs < STALE_HRS:
        return "stale", ts.isoformat()
    return "very-stale", ts.isoformat()


# --- Backfill helpers --------------------------------------------------------


def _unthemed_completed_weeks() -> list[str]:
    """Return labels of completed weeks that have raw data and need theming.

    A week is backfillable only when at least one raw input file exists
    (voice export `data/weeks/<W>.md` or meetings raw data). The earlier
    looser check returned weeks discoverable via *any* artifact, including
    old orphan files in `data/master/50_weeks/` that have no source data —
    `build_themes.py` then aborted on every backfill attempt.
    """
    from datetime import date
    import build_viewer
    today = date.today()
    weeks_dir = resolve_path(CFG, "weeks_dir")
    meetings_dir = resolve_path(CFG, "meetings_dir")
    out = []
    for label in build_viewer.discover_weeks():
        try:
            _, sat = build_viewer.week_range(label)
        except ValueError:
            continue
        if sat >= today:
            continue
        if build_viewer.has_themed_content(label):
            continue
        # Only include if there's something to actually theme. Voice file is
        # the canonical "this week was captured" marker; absence of meeting
        # data alone isn't enough since themes-meetings runs over fathom +
        # granola directories, not a per-week file.
        voice_file = weeks_dir / f"{label}.md"
        has_meetings = (
            meetings_dir.is_dir()
            and any(meetings_dir.rglob(f"*_{label[:4]}-*.md"))  # rough date match
        )
        if voice_file.exists() or has_meetings:
            out.append(label)
    return out


def _start_backfill_thread(job: dict, weeks: list[str]) -> None:
    """Run themes for each week sequentially, then trends, then rebuild viewer.

    Skips per-week steps whose input doesn't exist instead of aborting the
    whole batch. Only a `build_trends` or `build_viewer` failure terminates
    the job — those run over the aggregate, so a real failure means the
    intermediate results aren't usable.
    """
    weeks_dir = resolve_path(CFG, "weeks_dir")

    def runner():
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("w") as fh:
            fh.write(f"# backfill job {job['id']} started {job['started_at']}\n")
            fh.write(f"# {len(weeks)} weeks to theme: {', '.join(weeks)}\n\n")
            fh.flush()
            failed = False
            successes = 0

            def run_step(label: str, cmd: list[str]) -> int:
                fh.write(f"\n=== {label} ===\n")
                fh.flush()
                proc = subprocess.Popen(
                    cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, text=True,
                    start_new_session=True,
                )
                with _JOB_LOCK:
                    job["pid"] = proc.pid
                proc.wait()
                return proc.returncode

            for w in weeks:
                with _JOB_LOCK:
                    if job["status"] == "cancelled":
                        fh.write("\n# cancelled by user\n")
                        return

                voice_path = weeks_dir / f"{w}.md"
                if voice_path.exists():
                    rc = run_step(f"themes-voice    {w}", ["python3", "src/build_themes.py", "--week", w])
                    if rc != 0:
                        fh.write(f"\n# themes-voice {w} failed (exit {rc}) — continuing with next week\n")
                    else:
                        successes += 1
                else:
                    fh.write(f"\n=== themes-voice    {w} ===\n# skipped: no voice file at {voice_path}\n")

                # themes-meetings is opportunistic — exits 1 when no meetings;
                # we treat that as benign.
                run_step(f"themes-meetings {w}", ["python3", "src/build_themes_meetings.py", "--week", w])

            # Aggregate steps: only run if we actually themed something new
            if successes > 0:
                rc = run_step("trends", ["python3", "src/build_trends.py"])
                if rc != 0:
                    fh.write(f"\n# trends failed (exit {rc})\n")
                    failed = True
                rc = run_step("viewer", ["python3", "src/build_viewer.py"])
                if rc != 0:
                    fh.write(f"\n# viewer rebuild failed (exit {rc})\n")
                    failed = True
            else:
                fh.write("\n# no weeks were themable; nothing to roll up\n")

            fh.write(f"\n# backfill {'failed' if failed else 'complete'} ({successes} weeks newly themed)\n")

        # Append a line to the rolling pipeline log so the pill's freshness
        # check picks up the backfill the same way it picks up weekly syncs.
        # Without this, the pill keeps showing "synced 6h ago" even after a
        # 30-week backfill just landed.
        if successes > 0 and not failed:
            try:
                PIPELINE_LOG.parent.mkdir(parents=True, exist_ok=True)
                with PIPELINE_LOG.open("a") as plf:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    plf.write(f"[{ts}] === wispr-thoughts backfill: {successes} weeks themed ===\n")
            except OSError:
                pass

        with _JOB_LOCK:
            if job["status"] == "running":
                job["status"] = "failed" if failed else "completed"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            job["exit_code"] = 1 if failed else 0

    t = threading.Thread(target=runner, daemon=True)
    t.start()


# --- API key persistence ----------------------------------------------------


def _persist_fathom_key(key: str) -> None:
    """Write FATHOM_API_KEY to ~/.zshrc and the current process env."""
    _delivery_keys.persist_env_key("FATHOM_API_KEY", key)


# --- Source detection (read-only for the settings drawer) -------------------


def _detect_source(name: str, source_cfg: dict) -> dict:
    """Return {detected, hint} describing the source's reachability."""
    if name == "wispr":
        db = source_cfg.get("db_path", "")
        ok = bool(db) and Path(db).exists()
        return {"detected": ok, "hint": "" if ok else "Wispr Flow not installed"}
    if name == "granola":
        d = source_cfg.get("data_dir", "")
        ok = bool(d) and Path(d).is_dir()
        return {"detected": ok, "hint": "" if ok else "Granola not installed"}
    if name == "fathom":
        env_var = source_cfg.get("api_key_env", "FATHOM_API_KEY")
        if os.environ.get(env_var, "").strip():
            return {"detected": True, "hint": ""}
        # Fall back to checking ~/.zshrc since the launchd job sources it via
        # `bash -lc`. The interactive server may not have inherited the var
        # but the cron will read it from the rc file.
        zshrc = Path.home() / ".zshrc"
        if zshrc.exists():
            try:
                if any(
                    line.strip().startswith(f"export {env_var}=") and len(line.strip()) > len(f"export {env_var}=")
                    for line in zshrc.read_text().splitlines()
                ):
                    return {"detected": True, "hint": ""}
            except OSError:
                pass
        return {"detected": False, "hint": "API key needed"}
    return {"detected": False, "hint": ""}


# --- HTTP handler ------------------------------------------------------------


class WisprThoughtsHandler(BaseHTTPRequestHandler):
    server_version = "WisprThoughts/0.1"

    # Keep server logs concise: one line per request, no two-line default.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}\n")

    # ---- Routing ------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            return self._status()
        if path == "/api/sync/log":
            return self._sync_log()
        if path == "/api/settings":
            return self._get_settings()
        if path == "/api/schedule":
            return self._get_schedule()
        if path == "/api/backfill":
            return self._get_backfill()
        if path == "/api/delivery":
            return self._get_delivery()
        return self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/sync":
            return self._start_sync()
        if path == "/api/sync/cancel":
            return self._cancel_sync()
        if path == "/api/settings":
            return self._post_settings()
        if path == "/api/schedule":
            return self._post_schedule()
        if path == "/api/backfill":
            return self._start_backfill()
        if path == "/api/delivery":
            return self._post_delivery()
        if path == "/api/delivery/test":
            return self._post_delivery_test()
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown POST endpoint")

    # ---- Helpers for body parsing ------------------------------------------

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # ---- Static file serving ------------------------------------------------

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (VIEWER_DIR / rel).resolve()
        # Block path traversal: target must stay under VIEWER_DIR
        try:
            target.relative_to(VIEWER_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside viewer dir")
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, f"Not found: {rel}")
            return
        ctype = self._content_type(target)
        try:
            data = target.read_bytes()
        except OSError as e:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Read failed: {e}")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Aggressive no-cache for the local viewer so users never see stale
        # HTML after the build has updated. The static asset is one file we
        # own end-to-end; every request gets revalidated.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _content_type(path: Path) -> str:
        ext = path.suffix.lower()
        return {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
            ".md":   "text/markdown; charset=utf-8",
        }.get(ext, "application/octet-stream")

    # ---- API ----------------------------------------------------------------

    def _status(self) -> None:
        label, iso = _freshness()
        with _JOB_LOCK:
            syncing = _CURRENT_JOB is not None and _CURRENT_JOB["status"] == "running"
        body = {
            "last_sync": iso,
            "freshness": label,
            "syncing": syncing,
            "port": self.server.server_address[1],
        }
        self._json(HTTPStatus.OK, body)

    def _start_sync(self) -> None:
        global _CURRENT_JOB
        with _JOB_LOCK:
            if _CURRENT_JOB is not None and _CURRENT_JOB["status"] == "running":
                self._json(HTTPStatus.CONFLICT, {
                    "error": "Sync already in progress",
                    "job_id": _CURRENT_JOB["id"],
                })
                return
            _CURRENT_JOB = _new_job()
            job = _CURRENT_JOB
        _start_sync_thread(job)
        self._json(HTTPStatus.ACCEPTED, {"job_id": job["id"], "status": job["status"]})

    def _cancel_sync(self) -> None:
        import signal
        with _JOB_LOCK:
            job = _CURRENT_JOB
            if job is None or job.get("status") != "running":
                self._json(HTTPStatus.OK, {"ok": True, "note": "no running job"})
                return
            pid = job.get("pid")
            job["status"] = "cancelled"
        if pid:
            try:
                # Negative pid signals the whole process group, killing the
                # parent + any child subprocess (export_wispr, build_themes...)
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        # Drop the disk lock that weekly_email.py wrote so the next manual
        # run isn't blocked by a stale entry.
        lock = ROOT / ".weekly-email.lock"
        if lock.exists():
            try:
                lock.unlink()
            except OSError:
                pass
        self._json(HTTPStatus.OK, {"ok": True})

    def _sync_log(self) -> None:
        with _JOB_LOCK:
            job = None if _CURRENT_JOB is None else dict(_CURRENT_JOB)
        if job is None:
            self._json(HTTPStatus.OK, {
                "job_id": None, "status": "idle", "lines": [], "finished": True,
            })
            return
        log_path = Path(job["log_path"])
        try:
            lines = log_path.read_text().splitlines() if log_path.exists() else []
        except OSError:
            lines = []
        # Cap at last 200 lines so the JSON stays small for long runs
        tail = lines[-200:]
        self._json(HTTPStatus.OK, {
            "job_id": job["id"],
            "status": job["status"],
            "lines": tail,
            "finished": job["status"] in ("completed", "failed"),
            "exit_code": job["exit_code"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        })

    # ---- Settings -----------------------------------------------------------

    def _get_settings(self) -> None:
        cfg = load_config()
        schedule_cfg = cfg.get("schedule", {}) or {}
        sources_cfg = cfg.get("sources", {}) or {}
        body = {
            "schedule": {
                "weekday": int(schedule_cfg.get("weekday", 0)),
                "hour":    int(schedule_cfg.get("hour", 9)),
                "minute":  int(schedule_cfg.get("minute", 0)),
            },
            # Apple Notes is exported via a separate one-shot script and never
            # mixed into the digest pipeline, so it doesn't belong in the
            # settings drawer's sources panel. Keep it in config for the CLI.
            "sources": {
                name: {
                    "enabled": bool(s.get("enabled", False)),
                    **_detect_source(name, s),
                }
                for name, s in sources_cfg.items()
                if isinstance(s, dict) and name != "notes"
            },
        }
        self._json(HTTPStatus.OK, body)

    def _post_settings(self) -> None:
        data = self._read_json_body()
        if data is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return

        updates: dict[tuple[str, str], object] = {}
        if "schedule" in data and isinstance(data["schedule"], dict):
            s = data["schedule"]
            if "weekday" in s: updates[("schedule", "weekday")] = int(s["weekday"])
            if "hour"    in s: updates[("schedule", "hour")]    = int(s["hour"])
            if "minute"  in s: updates[("schedule", "minute")]  = int(s["minute"])

        if "sources" in data and isinstance(data["sources"], dict):
            for name, info in data["sources"].items():
                if not isinstance(info, dict) or "enabled" not in info:
                    continue
                key = (f"sources.{name}", "enabled")
                if key in _settings.WRITABLE:
                    updates[key] = bool(info["enabled"])

        # Fathom API key is special: written to ~/.zshrc as an export line so
        # both interactive shells and the launchd job (via `bash -lc`) inherit it.
        fathom_key = data.get("fathom_api_key")
        if isinstance(fathom_key, str) and fathom_key.strip():
            try:
                _persist_fathom_key(fathom_key.strip())
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"fathom key: {exc}"})
                return

        if updates:
            try:
                _settings.write_keys(updates)
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"write: {exc}"})
                return

        self._json(HTTPStatus.OK, {"ok": True})

    # ---- Delivery (Resend) --------------------------------------------------

    def _get_delivery(self) -> None:
        self._json(HTTPStatus.OK, _delivery.is_configured())

    def _post_delivery(self) -> None:
        data = self._read_json_body()
        if data is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return

        # API key first (so a single POST can save key + addresses + toggle)
        key = data.get("resend_api_key")
        if isinstance(key, str) and key.strip():
            try:
                _delivery_keys.persist_env_key("RESEND_API_KEY", key.strip())
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"key: {exc}"})
                return

        # Then config fields. Surgical writes via _settings.
        updates: dict[tuple[str, str], object] = {}
        if "enabled"    in data: updates[("delivery", "enabled")]    = bool(data["enabled"])
        if "from_email" in data: updates[("delivery", "from_email")] = str(data["from_email"]).strip()
        if "to_email"   in data: updates[("delivery", "to_email")]   = str(data["to_email"]).strip()
        if updates:
            # Make sure these keys are writable (added in this commit)
            for k in updates:
                _settings.WRITABLE.setdefault(k, "")
            try:
                _settings.write_keys(updates)
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"write: {exc}"})
                return

        self._json(HTTPStatus.OK, {"ok": True, **_delivery.is_configured()})

    def _post_delivery_test(self) -> None:
        try:
            resp = _delivery.send_test()
        except _delivery.ResendError as e:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": e.message, "status": e.status})
            return
        except Exception as e:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return
        self._json(HTTPStatus.OK, {"ok": True, "id": resp.get("id")})

    # ---- Backfill -----------------------------------------------------------

    def _get_backfill(self) -> None:
        """Report which weeks have raw data but no themes (yet)."""
        unthemed = _unthemed_completed_weeks()
        self._json(HTTPStatus.OK, {
            "unthemed_count": len(unthemed),
            "weeks": unthemed,
        })

    def _start_backfill(self) -> None:
        global _CURRENT_JOB
        with _JOB_LOCK:
            if _CURRENT_JOB is not None and _CURRENT_JOB["status"] == "running":
                self._json(HTTPStatus.CONFLICT, {
                    "error": f"A {_CURRENT_JOB.get('kind', 'sync')} job is already running",
                    "job_id": _CURRENT_JOB["id"],
                })
                return
            unthemed = _unthemed_completed_weeks()
            if not unthemed:
                self._json(HTTPStatus.OK, {"ok": True, "note": "nothing to backfill"})
                return
            _CURRENT_JOB = _new_job()
            _CURRENT_JOB["kind"] = "backfill"
            _CURRENT_JOB["weeks"] = unthemed
            job = _CURRENT_JOB
        _start_backfill_thread(job, unthemed)
        self._json(HTTPStatus.ACCEPTED, {
            "job_id": job["id"],
            "status": job["status"],
            "weeks": unthemed,
        })

    # ---- Schedule -----------------------------------------------------------

    def _get_schedule(self) -> None:
        cfg = load_config()
        s = cfg.get("schedule", {}) or {}
        info = _schedule.status(
            int(s.get("weekday", 0)),
            int(s.get("hour", 9)),
            int(s.get("minute", 0)),
        )
        self._json(HTTPStatus.OK, info)

    def _post_schedule(self) -> None:
        data = self._read_json_body()
        if data is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        cfg = load_config()
        s = cfg.get("schedule", {}) or {}
        weekday = int(data.get("weekday", s.get("weekday", 0)))
        hour    = int(data.get("hour",    s.get("hour", 9)))
        minute  = int(data.get("minute",  s.get("minute", 0)))
        enabled = bool(data.get("enabled", True))

        # Persist schedule values (so subsequent restarts see the same time)
        try:
            _settings.write_keys({
                ("schedule", "weekday"): weekday,
                ("schedule", "hour"):    hour,
                ("schedule", "minute"):  minute,
            })
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"write: {exc}"})
            return

        if enabled:
            r = _schedule.install(weekday, hour, minute)
        else:
            r = _schedule.remove()
        if not r.get("ok"):
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": r.get("output", "schedule failed")})
            return
        self._json(HTTPStatus.OK, {"ok": True, "info": _schedule.status(weekday, hour, minute)})

    # ---- Helpers ------------------------------------------------------------

    def _json(self, code: HTTPStatus, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# --- Main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-open the browser on startup")
    args = ap.parse_args()

    if not VIEWER_DIR.exists() or not (VIEWER_DIR / "index.html").is_file():
        sys.exit(
            f"Viewer not built yet. Run:\n"
            f"  python3 src/build_viewer.py\n"
            f"then restart the server."
        )

    try:
        server = ThreadingHTTPServer((HOST, args.port), WisprThoughtsHandler)
    except OSError as e:
        sys.exit(
            f"Could not bind to {HOST}:{args.port}: {e}\n"
            f"Try a different port: THOUGHTPATTERN_PORT=8081 python3 src/serve.py"
        )

    url = f"http://{HOST}:{args.port}/"
    print(f"Wispr Thoughts viewer: {url}", file=sys.stderr)
    print(f"  data/viewer/ from {VIEWER_DIR}", file=sys.stderr)
    print(f"  pipeline log:    {PIPELINE_LOG}", file=sys.stderr)
    print(f"  ctrl-c to stop", file=sys.stderr)

    if not args.no_browser:
        try:
            subprocess.run(["open", url], check=False)
        except FileNotFoundError:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
