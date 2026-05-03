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

from _config import load as load_config, resolve_path

CFG = load_config()
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
            proc = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, text=True,
            )
            proc.wait()
            fh.write(f"\n# job exited with code {proc.returncode}\n")
        with _JOB_LOCK:
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
        return self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/sync":
            return self._start_sync()
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown POST endpoint")

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
        self.send_header("Cache-Control", "no-store")
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
