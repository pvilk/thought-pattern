#!/usr/bin/env python3
"""Export Granola meeting transcripts to per-meeting markdown.

Granola is an Electron app that caches meeting documents + transcripts in
`~/Library/Application Support/Granola/cache-v6.json`. This script parses
that file and writes one markdown file per meeting to
`<meetings_dir>/<YYYY-MM-DD>_<slug>_<doc_id>.md`.

Schema (reverse-engineered, may break on Granola updates):
  cache.state.documents[<doc_id>] is a meeting doc with title, created_at,
                                    notes_markdown, summary, attendees
  cache.state.transcripts[<doc_id>] is a list of turn objects:
                                       {start_timestamp, end_timestamp, text, source}

Only meetings present in cache-v6.json are exported. Older meetings are
loaded from cloud on demand by the Granola app and aren't visible here.

Usage:
    python3 src/export_granola.py
    python3 src/export_granola.py --refresh        # re-export everything
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from _config import load as load_config, resolve_path

CFG = load_config()
GRANOLA_CFG = CFG.get("sources", {}).get("granola", {})
GRANOLA_DATA = Path(GRANOLA_CFG.get("data_dir", "")).expanduser()
CACHE_FILE = GRANOLA_DATA / "cache-v6.json"
MEETINGS_DIR = resolve_path(CFG, "meetings_dir") / "granola"


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "untitled"


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def export_meeting(doc_id: str, doc: dict, transcript: list[dict] | None, force: bool) -> Path | None:
    title = doc.get("title", "Untitled")
    created = doc.get("created_at")
    if not created:
        return None
    try:
        d = parse_iso(created).date()
    except (ValueError, TypeError):
        return None

    fname = f"{d.isoformat()}_{slugify(title)}_{doc_id}.md"
    out_path = MEETINGS_DIR / fname
    if out_path.exists() and not force:
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "",
             f"- **Date:** {d.isoformat()}",
             f"- **Document id:** `{doc_id}`",
             f"- **Source:** Granola",
             ""]

    notes = doc.get("notes_markdown") or doc.get("notes_plain") or ""
    if notes.strip():
        lines += ["## Notes", "", notes.strip(), ""]
    summary = doc.get("summary") or doc.get("overview")
    if isinstance(summary, str) and summary.strip():
        lines += ["## Summary", "", summary.strip(), ""]

    if transcript:
        lines += ["## Transcript", ""]
        for i, turn in enumerate(transcript):
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            ts = turn.get("start_timestamp", "")
            try:
                t = parse_iso(ts).strftime("%H:%M:%S")
            except (ValueError, TypeError):
                t = ""
            source = turn.get("source", "")
            vid = f"g{doc_id[:8]}.t{i+1:04d}"
            lines.append(f"### `{vid}` _{t} · {source}_")
            lines.append("")
            lines.append(f"> {text}")
            lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def main() -> None:
    if not GRANOLA_CFG.get("enabled", False):
        print("Granola source disabled in config.local.toml; skipping.")
        return
    if not CACHE_FILE.exists():
        sys.exit(f"Granola cache not found at {CACHE_FILE}\n"
                 f"Edit sources.granola.data_dir or disable in config.local.toml.")

    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-export existing meetings")
    args = ap.parse_args()

    with open(CACHE_FILE) as f:
        cache = json.load(f)
    state = cache.get("cache", {}).get("state", {})
    docs = state.get("documents", {})
    transcripts = state.get("transcripts", {})

    print(f"Granola cache: {len(docs)} documents, {len(transcripts)} transcripts in cache")
    written = 0
    for doc_id, doc in docs.items():
        out = export_meeting(doc_id, doc, transcripts.get(doc_id), args.refresh)
        if out:
            written += 1
    print(f"Exported {written} meetings to {MEETINGS_DIR}")
    print("Note: older meetings load on-demand from Granola cloud and aren't in this cache.")


if __name__ == "__main__":
    main()
