#!/usr/bin/env python3
"""Export Fathom meetings via API to per-meeting markdown.

Reads FATHOM_API_KEY from the environment variable named in config.sources.fathom.api_key_env
(defaults to FATHOM_API_KEY). Pages through `/external/v1/meetings`, fetches each
transcript, and writes `<meetings_dir>/fathom/<YYYY-MM-DD>_<slug>_<id>.md`.

Re-running is safe; meetings already on disk are skipped unless --refresh.

Usage:
    export FATHOM_API_KEY=fathom_api_key_xxx
    python3 src/export_fathom.py
    python3 src/export_fathom.py --since 2025-01-01
    python3 src/export_fathom.py --refresh                # re-download all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from _config import load as load_config, resolve_path

CFG = load_config()
FATHOM_CFG = CFG.get("sources", {}).get("fathom", {})
API_KEY_ENV = FATHOM_CFG.get("api_key_env", "FATHOM_API_KEY")
MEETINGS_DIR = resolve_path(CFG, "meetings_dir") / "fathom"
API_BASE = "https://api.fathom.ai/external/v1"


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "untitled"


def api_get(path: str, params: dict, key: str) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{qs}" if params else f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Api-Key": key,
        "Accept": "application/json",
    })
    ctx = ssl.create_default_context()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}


def list_meetings(key: str, since: str | None) -> list[dict]:
    """Page through /meetings with transcript + summary inlined.

    Fathom's per-meeting `/meetings/{id}/transcript` endpoint 404s; the
    supported path is to request `include_transcript=true&include_summary=true`
    on the listing endpoint and pull both bodies out of each item.
    """
    out = []
    cursor = None
    while True:
        params = {
            "limit": 100,
            "include_transcript": "true",
            "include_summary": "true",
        }
        if cursor:
            params["cursor"] = cursor
        if since:
            params["created_after"] = since
        data = api_get("/meetings", params, key)
        items = data.get("items", [])
        out.extend(items)
        cursor = data.get("next_cursor")
        if not cursor or not items:
            break
    return out


def write_meeting(meeting: dict, force: bool) -> Path | None:
    """Write one meeting markdown file. Reads transcript/summary inlined on the meeting."""
    mid = str(meeting.get("recording_id") or meeting.get("id") or "")
    title = meeting.get("meeting_title") or meeting.get("title") or "Untitled"
    started = meeting.get("started_at") or meeting.get("created_at")
    if not started or not mid:
        return None
    d = datetime.fromisoformat(started.replace("Z", "+00:00")).date()
    fname = f"{d.isoformat()}_{slugify(title)}_{mid}.md"
    out_path = MEETINGS_DIR / fname
    if out_path.exists() and not force:
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "",
             f"- **Date:** {d.isoformat()}",
             f"- **Meeting id:** `{mid}`",
             f"- **Source:** Fathom",
             f"- **URL:** {meeting.get('share_url') or meeting.get('url', '')}",
             ""]
    summary = (
        meeting.get("default_summary_markdown_formatted")
        or meeting.get("summary")
        or meeting.get("ai_summary")
    )
    if summary:
        lines += ["## Summary", "", str(summary).strip(), ""]
    turns = (
        meeting.get("transcript_messages")
        or meeting.get("transcript")
        or meeting.get("turns")
        or []
    )
    if isinstance(turns, list) and turns:
        lines += ["## Transcript", ""]
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            speaker = (
                (turn.get("speaker") or {}).get("name")
                if isinstance(turn.get("speaker"), dict) else None
            ) or turn.get("speaker_name") or turn.get("speaker") or ""
            text = (turn.get("text") or turn.get("content") or "").strip()
            if not text:
                continue
            vid = f"m{mid}.t{i+1:03d}"
            lines.append(f"### `{vid}` _{speaker}_\n")
            lines.append(f"> {text}\n")

    out_path.write_text("\n".join(lines))
    return out_path


def main() -> None:
    if not FATHOM_CFG.get("enabled", False):
        print("Fathom source disabled in config.local.toml; skipping.")
        return
    key = os.environ.get(API_KEY_ENV)
    if not key:
        sys.exit(f"Set ${API_KEY_ENV} with your Fathom API key.")

    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=str, help="ISO date (e.g. 2025-01-01)")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    print("Listing meetings (with transcripts inlined)…")
    meetings = list_meetings(key, args.since)
    print(f"Got {len(meetings)} meetings; writing markdown…")
    written = 0
    for m in meetings:
        try:
            if write_meeting(m, args.refresh):
                written += 1
        except Exception as e:
            mid = m.get("recording_id") or m.get("id")
            sys.stderr.write(f"  failed {mid}: {e}\n")
    print(f"Done. {written} meetings written to {MEETINGS_DIR}")


if __name__ == "__main__":
    main()
