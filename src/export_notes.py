#!/usr/bin/env python3
"""Export Apple Notes via AppleScript/JXA to per-note markdown.

Apple Notes data lives at ~/Library/Group Containers/group.com.apple.notes/
NoteStore.sqlite, but macOS TCC blocks direct SQLite reads from terminals
without Full Disk Access. The Notes app's own AppleScript interface bypasses
that restriction (the app already has access to its own data), so this
exporter drives it via JXA (JavaScript for Automation) for cleaner JSON
output than classic AppleScript string concatenation.

Output: <notes_dir>/<YYYY-MM-DD>_<slug>_<short_id>.md, one file per note.
Notes live in their own corpus tree (data/notes/) and are NOT picked up by
the meeting themer. They're a parallel store, exported on demand and queried
separately when needed.

Resume-safe: re-running only fetches notes whose modification date is newer
than the local copy.

Usage:
    python3 src/export_notes.py
    python3 src/export_notes.py --refresh         # re-export everything
    python3 src/export_notes.py --since 2025-01-01
    python3 src/export_notes.py --limit 10        # smoke-test with 10 notes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from _config import load as load_config, resolve_path

CFG = load_config()
NOTES_CFG = CFG.get("sources", {}).get("notes", {})
# Notes live in their own data/notes/ tree, NOT under meetings_dir, so the
# meeting themer doesn't pull them into the digest. Notes are a parallel
# corpus exported on demand and queried separately.
NOTES_DIR = resolve_path(CFG, "notes_dir")


# --- JXA dump script ---------------------------------------------------------

# Pulls every note's metadata + HTML body and writes one JSON file. ObjC bridge
# handles the file write so we sidestep stdout buffer limits for large libraries.
# Two JXA modes. Each runs as a single short-lived osascript process so a
# single huge note (some users have multi-megabyte journal entries) can't
# stall the overall export.

JXA_LIST_FOLDERS = r"""
function run(argv) {
  const Notes = Application('Notes');
  const folders = Notes.folders();
  const out = [];
  for (let f = 0; f < folders.length; f++) {
    try {
      const name = folders[f].name();
      const count = folders[f].notes().length;
      out.push({ name: name, count: count });
    } catch (e) {}
  }
  return JSON.stringify(out);
}
"""

JXA_FETCH_BATCH = r"""
function run(argv) {
  const folderName = argv[0];
  const startIdx = parseInt(argv[1]);
  const batchSize = parseInt(argv[2]);
  const Notes = Application('Notes');
  const folder = Notes.folders.byName(folderName);
  const notes = folder.notes();
  const total = notes.length;
  const end = Math.min(startIdx + batchSize, total);
  const out = [];
  for (let i = startIdx; i < end; i++) {
    try {
      const n = notes[i];
      out.push({
        id: n.id(),
        name: n.name(),
        created: n.creationDate().toISOString(),
        modified: n.modificationDate().toISOString(),
        folder: folderName,
        body: n.body()
      });
    } catch (e) {
      // locked / errored notes: skip silently
    }
  }
  return JSON.stringify(out);
}
"""


SKIP_FOLDERS = {"Recently Deleted"}
DEFAULT_BATCH = 50


def list_folders() -> list[dict]:
    cmd = ["osascript", "-l", "JavaScript", "-e", JXA_LIST_FOLDERS]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"JXA folder list failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch_batch(folder_name: str, start_idx: int, batch_size: int) -> list[dict]:
    cmd = [
        "osascript", "-l", "JavaScript", "-e", JXA_FETCH_BATCH,
        folder_name, str(start_idx), str(batch_size),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Surface stderr but don't kill the whole export on a single bad batch
        print(
            f"  ! batch {folder_name}[{start_idx}:{start_idx+batch_size}] failed: "
            f"{result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"  ! batch {folder_name}[{start_idx}] parse failed: {e}", file=sys.stderr)
        return []


# --- Noise filter ------------------------------------------------------------

# Calibrated to drop pure-noise notes that would only dilute themes:
# API keys, env var assignments, raw URL collections, hex/base64 blobs,
# extremely short notes that aren't sentences. Each rule returns a reason
# string for the run summary.

_TAG_RE = re.compile(r"<[^>]+>")
_API_KEY_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]{3,}\s*=\s*[A-Za-z0-9+/=._\-]{15,}\s*$", re.MULTILINE)
_EXPORT_RE = re.compile(r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]{3,}\s*=", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_.\-]{60,}$")
# Raw API key prefixes: Anthropic, OpenAI, Slack, GitHub, AWS, generic sk-
_RAW_KEY_RE = re.compile(
    r"\b("
    r"sk-(?:proj-|ant-|live_|test_)?[A-Za-z0-9_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{20,}"
    r"|gh[ps]_[A-Za-z0-9]{30,}"
    r"|AKIA[A-Z0-9]{16}"
    r")\b"
)
_ECHO_KEY_RE = re.compile(r"^\s*(?:echo|set)\s+[A-Z][A-Z0-9_]{3,}\s*=", re.MULTILINE)


def _plain_text(html: str) -> str:
    """Best-effort plain-text extraction from Apple Notes HTML."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def noise_reason(rec: dict) -> str | None:
    """Return a short reason string if this note is noise, else None."""
    body = rec.get("body") or ""
    name = (rec.get("name") or "").strip()
    text = _plain_text(body)
    name_and_text = (name + " " + text).strip()

    # Strip the title from the body if Notes duplicated it as the first line
    if name and text.startswith(name):
        text = text[len(name):].lstrip(" :.\n")

    # Raw API key prefix anywhere in title or body — drop, even if there's
    # surrounding prose. Some users save keys with explanatory titles.
    if _RAW_KEY_RE.search(name_and_text):
        return "raw api key"

    if len(text) < 30 and not re.search(r"[.?!]", text):
        return "too short"

    # Pure FOO=BAR line (env var / shell var)
    if _API_KEY_RE.search(body) or _API_KEY_RE.search(text):
        non_key = _API_KEY_RE.sub("", text).strip()
        if len(non_key) < 40:
            return "api key / env var"

    # `export FOO=...`, `echo FOO=...`, `FOO=...`
    if (_EXPORT_RE.search(text) or _ECHO_KEY_RE.search(text)) and len(text) < 300:
        return "shell export"

    # Notes that are mostly URLs
    urls = _URL_RE.findall(text)
    if urls:
        without_urls = _URL_RE.sub("", text).strip()
        if len(without_urls) < 20:
            return "urls only"

    # Single long hex / base64 token with no surrounding prose
    longest_token = max((tok for tok in text.split()), key=len, default="")
    if len(longest_token) > 60 and _BLOB_RE.fullmatch(longest_token):
        if len(text) - len(longest_token) < 60:
            return "hex/base64 blob"

    return None


# --- HTML to Markdown --------------------------------------------------------

# Apple Notes bodies are HTML with a small tag set: div, br, h1-h3, b/strong,
# i/em, ul/ol/li, a, span, font. A minimal handler covers everything well
# enough for a digest pipeline.

class _NotesHTMLConverter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[str] = []  # 'ul' or 'ol'
        self.list_counters: list[int] = []
        self._in_link = False
        self._link_href = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("h1", "h2", "h3"):
            self._newline(2)
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "div":
            self._newline(1)
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("b", "strong"):
            self.parts.append("**")
        elif tag in ("i", "em"):
            self.parts.append("*")
        elif tag == "ul":
            self._newline(1)
            self.list_stack.append("ul")
            self.list_counters.append(0)
        elif tag == "ol":
            self._newline(1)
            self.list_stack.append("ol")
            self.list_counters.append(0)
        elif tag == "li":
            self._newline(1)
            depth = max(0, len(self.list_stack) - 1)
            self.parts.append("  " * depth)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.list_counters[-1] += 1
                self.parts.append(f"{self.list_counters[-1]}. ")
            else:
                self.parts.append("- ")
        elif tag == "a":
            self._in_link = True
            self._link_href = a.get("href", "") or ""
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3"):
            self._newline(2)
        elif tag == "div":
            pass
        elif tag in ("b", "strong"):
            self.parts.append("**")
        elif tag in ("i", "em"):
            self.parts.append("*")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.list_counters.pop()
            self._newline(1)
        elif tag == "a":
            text = "".join(self._link_text).strip()
            if self._link_href and text:
                self.parts.append(f"[{text}]({self._link_href})")
            else:
                self.parts.append(text)
            self._in_link = False
            self._link_href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)
        else:
            self.parts.append(data)

    def _newline(self, n: int) -> None:
        # Avoid runaway blank lines: cap consecutive newlines
        joined = "".join(self.parts[-4:])
        existing = len(joined) - len(joined.rstrip("\n"))
        need = max(0, n - existing)
        if need:
            self.parts.append("\n" * need)

    def get_markdown(self) -> str:
        text = "".join(self.parts)
        # Collapse 3+ newlines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str) -> str:
    if not html:
        return ""
    p = _NotesHTMLConverter()
    p.feed(html)
    p.close()
    return p.get_markdown()


# --- Filenames + IDs ---------------------------------------------------------

ID_TAIL_RE = re.compile(r"/p(\d+)/?$")


def short_id(note_id: str) -> str:
    """Extract the short numeric ID from x-coredata:// URIs.

    Apple Notes ids look like:
      x-coredata://<store-uuid>/ICNote/p12345
    The trailing pXX is the per-store primary key. Falls back to a hash of
    the full id if the format is unexpected.
    """
    m = ID_TAIL_RE.search(note_id or "")
    if m:
        return m.group(1)
    # Fallback: take alphanumerics, last 10 chars
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", note_id or "")
    return cleaned[-10:] or "unknown"


SLUG_RE = re.compile(r"[^\w\s-]")
SPACES_RE = re.compile(r"[\s_-]+")


def slugify(name: str) -> str:
    s = SLUG_RE.sub("", (name or "").lower())
    s = SPACES_RE.sub("-", s).strip("-")
    return (s[:60] or "untitled")


def parse_iso(ts: str) -> datetime:
    # JXA emits "2026-04-26T11:17:26.000Z"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# --- Existing-file metadata cache -------------------------------------------

# Parse out the modification date from existing files so we can skip notes
# whose mtime hasn't changed since the last export.

EXISTING_RE = re.compile(r"_(\d+)\.md$")
MOD_LINE_RE = re.compile(r"^- \*\*Modified:\*\*\s*(\S+)", re.MULTILINE)


def existing_mod_dates() -> dict[str, str]:
    """Map short_id → ISO modified timestamp from existing files."""
    out: dict[str, str] = {}
    if not NOTES_DIR.exists():
        return out
    for p in NOTES_DIR.glob("*.md"):
        m = EXISTING_RE.search(p.name)
        if not m:
            continue
        sid = m.group(1)
        try:
            head = p.read_text(errors="ignore")[:2048]
        except OSError:
            continue
        mm = MOD_LINE_RE.search(head)
        if mm:
            out[sid] = mm.group(1)
    return out


def find_existing_path(sid: str) -> Path | None:
    if not NOTES_DIR.exists():
        return None
    for p in NOTES_DIR.glob(f"*_{sid}.md"):
        return p
    return None


# --- Per-note markdown -------------------------------------------------------


def write_note(rec: dict, force: bool, mod_index: dict[str, str]) -> tuple[Path | None, str]:
    """Write one note as markdown. Returns (path, status)."""
    sid = short_id(rec["id"])
    name = (rec.get("name") or "Untitled").strip() or "Untitled"
    folder = (rec.get("folder") or "").strip() or "Notes"
    created = parse_iso(rec["created"])
    modified = parse_iso(rec["modified"])
    body_html = rec.get("body") or ""

    fname = f"{created.date().isoformat()}_{slugify(name)}_{sid}.md"
    out_path = NOTES_DIR / fname

    # Skip if existing file's recorded modification date matches and not forced
    if not force and sid in mod_index and mod_index[sid] == rec["modified"]:
        return out_path, "skip"

    # If the note's name or date changed since last export, the filename may
    # have changed too — remove the old file before writing the new one.
    if not force:
        prev = find_existing_path(sid)
        if prev and prev != out_path:
            try:
                prev.unlink()
            except OSError:
                pass

    md_body = html_to_markdown(body_html)

    lines = [
        f"# {name}",
        "",
        f"- **Date:** {created.date().isoformat()}",
        f"- **Created:** {created.isoformat()}",
        f"- **Modified:** {rec['modified']}",
        f"- **Folder:** {folder}",
        f"- **Source:** Apple Notes",
        f"- **Note id:** `{sid}`",
        "",
        "---",
        "",
        f"## `n{sid}.t01` Note body",
        "",
        md_body or "_(empty note)_",
        "",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(out_path)
    return out_path, "wrote"


# --- Main --------------------------------------------------------------------


def main() -> None:
    if not NOTES_CFG.get("enabled", False):
        print("Notes source disabled in config.local.toml; skipping.")
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-export every note")
    ap.add_argument("--since", help="only export notes created on/after YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of notes pulled")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                    help=f"notes per JXA call (default {DEFAULT_BATCH})")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel JXA worker count (default 1; try 4 for big libraries)")
    ap.add_argument("--show-noise", action="store_true",
                    help="print short titles of filtered noise notes for tuning")
    args = ap.parse_args()

    since_date: date | None = None
    if args.since:
        try:
            since_date = date.fromisoformat(args.since)
        except ValueError:
            sys.exit(f"--since must be YYYY-MM-DD, got {args.since!r}")

    print("Listing folders...", file=sys.stderr)
    folders = [f for f in list_folders() if f["name"] not in SKIP_FOLDERS]
    total_planned = sum(f["count"] for f in folders)
    print(f"  {len(folders)} folders, {total_planned:,} notes total", file=sys.stderr)

    mod_index = {} if args.refresh else existing_mod_dates()

    counts = {"wrote": 0, "skipped": 0, "filtered": 0, "noise_dropped": 0, "pulled": 0}
    noise_counts: dict[str, int] = {}
    counts_lock = threading.Lock()

    def process_batch(folder_name: str, offset: int, batch_size: int) -> tuple[int, int]:
        """Fetch and write one batch. Returns (records_seen, records_kept)."""
        records = fetch_batch(folder_name, offset, batch_size)
        seen = 0
        for rec in records:
            seen += 1
            try:
                created = parse_iso(rec["created"])
            except (KeyError, ValueError):
                with counts_lock:
                    counts["filtered"] += 1
                continue
            if since_date and created.date() < since_date:
                with counts_lock:
                    counts["filtered"] += 1
                continue
            reason = noise_reason(rec)
            if reason:
                with counts_lock:
                    counts["noise_dropped"] += 1
                    noise_counts[reason] = noise_counts.get(reason, 0) + 1
                if args.show_noise:
                    print(f"  [noise:{reason}] {(rec.get('name') or '')[:80]}",
                          file=sys.stderr)
                continue
            _, status = write_note(rec, force=args.refresh, mod_index=mod_index)
            with counts_lock:
                if status == "wrote":
                    counts["wrote"] += 1
                elif status == "skip":
                    counts["skipped"] += 1
        return offset, seen

    # Build the full list of (folder, offset) chunks across every folder.
    chunks: list[tuple[str, int]] = []
    for folder in folders:
        for offset in range(0, folder["count"], args.batch_size):
            chunks.append((folder["name"], offset))

    print(
        f"Fetching {len(chunks)} batches with {args.workers} worker(s)...",
        file=sys.stderr,
    )

    if args.workers <= 1:
        # Sequential path: keep tight per-folder progress reporting
        cur_folder = None
        for folder_name, offset in chunks:
            if args.limit and counts["pulled"] >= args.limit:
                break
            if folder_name != cur_folder:
                cur_folder = folder_name
                folder_count = next(f["count"] for f in folders if f["name"] == folder_name)
                print(f"[{folder_name}] {folder_count} notes", file=sys.stderr)
            _, seen = process_batch(folder_name, offset, args.batch_size)
            counts["pulled"] += seen
            folder_count = next(f["count"] for f in folders if f["name"] == folder_name)
            print(f"  {folder_name}: {min(offset + args.batch_size, folder_count)}/{folder_count}",
                  file=sys.stderr)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(process_batch, folder_name, offset, args.batch_size):
                    (folder_name, offset)
                for folder_name, offset in chunks
            }
            done = 0
            for fut in as_completed(futures):
                folder_name, offset = futures[fut]
                try:
                    _, seen = fut.result()
                except Exception as e:
                    print(f"  ! batch {folder_name}[{offset}] errored: {e}", file=sys.stderr)
                    continue
                with counts_lock:
                    counts["pulled"] += seen
                done += 1
                if done % 5 == 0 or done == len(chunks):
                    print(
                        f"  progress: {done}/{len(chunks)} batches, "
                        f"wrote={counts['wrote']} noise={counts['noise_dropped']}",
                        file=sys.stderr,
                    )

    summary = (
        f"Done. wrote={counts['wrote']} unchanged={counts['skipped']} "
        f"filtered={counts['filtered']} noise_dropped={counts['noise_dropped']}"
    )
    print(summary, file=sys.stderr)
    if noise_counts:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(noise_counts.items(), key=lambda x: -x[1]))
        print(f"  noise breakdown: {breakdown}", file=sys.stderr)
    print(f"  -> {NOTES_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
