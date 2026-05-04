#!/usr/bin/env python3
"""Build a local self-contained weekly viewer at data/viewer/index.html.

Reads every available week from:
  - data/weeks/<W>.md         (voice / Wispr themes)
  - data/master/50_weeks/<W>.md  (meeting themes)
  - data/digests/<W>.md       (unified digest, preferred when present)

Bundles all weeks into a single HTML page with prev/next navigation,
keyboard shortcuts, and a week jump-list. Open the file in any browser
(no server required).

Usage:
    python3 src/build_viewer.py
    open data/viewer/index.html
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from _config import load as load_config, resolve_path

CFG = load_config()
WEEKS_DIR = resolve_path(CFG, "weeks_dir")
MEETING_WEEKS_DIR = resolve_path(CFG, "master_dir") / "50_weeks"
DIGESTS_DIR = resolve_path(CFG, "digests_dir")
ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = ROOT / "data" / "viewer"


def parse_week_label(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", label)
    if not m:
        raise ValueError(label)
    return int(m.group(1)), int(m.group(2))


def week_range(label: str) -> tuple[date, date]:
    y, w = parse_week_label(label)
    jan1 = date(y, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - timedelta(days=jan1_offset)
    sunday = w01_sunday + timedelta(days=(w - 1) * 7)
    return sunday, sunday + timedelta(days=6)


def discover_weeks() -> list[str]:
    labels = set()
    for d in (WEEKS_DIR, MEETING_WEEKS_DIR, DIGESTS_DIR):
        if d.is_dir():
            for p in d.glob("*.md"):
                if re.fullmatch(r"\d{4}-W\d{1,2}", p.stem):
                    labels.add(p.stem)
    return sorted(labels, key=parse_week_label)


# Matches `(<vertex>, <vertex>, ...)` for any source. The leading character
# is the source code (w=Wispr, m=Fathom, g=Granola, n=Apple Notes, a=Alice
# fixture, plus any future single-letter source). Match any ASCII letter so
# new sources don't need a code change to get their tags stripped.
VERTEX_PAREN_RE = re.compile(
    r"\s*\((?:(?:[a-zA-Z][\w-]+\.[\w-]+(?:\.[\w-]+)?)(?:[,;]\s*)?)+\)"
)


def strip_vertex_anchors(md: str) -> str:
    """Remove `(w20260415.034, ...)` style anchors from rendered markdown.

    The anchors are useful inside the source files for traceability but clutter
    the reading view. Stripping them makes weekly digests readable.
    """
    text = VERTEX_PAREN_RE.sub("", md)
    # Trim trailing whitespace left over after removing a trailing anchor
    text = re.sub(r" +$", "", text, flags=re.MULTILINE)
    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


ITEM_RE = re.compile(
    r"^-\s*\*\*([^*]+?)\*\*\s*[—\-:]\s*(.+?)(?=\n-\s*\*\*|\n##|\Z)",
    flags=re.DOTALL | re.MULTILINE,
)


def parse_items_from_section(text: str, header_pattern: str, end_pattern: str) -> list[tuple[str, str]]:
    """Extract bullet items (`- **Name**: summary`) from a markdown section."""
    if not text:
        return []
    section_re = re.compile(
        rf"##\s*{header_pattern}\s*\n+(.*?)(?=\n##|\Z)",
        flags=re.DOTALL | re.IGNORECASE,
    )
    m = section_re.search(text)
    if not m:
        return []
    body = m.group(1).strip()
    items = []
    for im in ITEM_RE.finditer(body):
        name = im.group(1).strip()
        # Collapse internal newlines/whitespace runs in the summary
        summary = re.sub(r"\s+", " ", im.group(2).strip())
        items.append((name, summary))
    return items


def _normalize_name(name: str) -> str:
    return re.sub(r"[^\w\s]", " ", name.lower()).strip()


def merge_sources(
    voice: list[tuple[str, str]], meeting: list[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Merge items from two sources, marking exact-name matches as 'both'.

    Returns a list of (source, name, summary) tuples. Source ∈ {voice, meeting, both}.
    Order: cross-source first (most interesting), then voice-only, then meeting-only.
    """
    voice_by_norm = {_normalize_name(n): (n, s) for n, s in voice}
    meeting_by_norm = {_normalize_name(n): (n, s) for n, s in meeting}

    merged: list[tuple[str, str, str]] = []
    matched: set[str] = set()
    for v_norm, (v_name, v_summary) in voice_by_norm.items():
        if v_norm in meeting_by_norm:
            m_name, m_summary = meeting_by_norm[v_norm]
            # Use the longer of the two summaries (more detail wins)
            chosen = v_summary if len(v_summary) >= len(m_summary) else m_summary
            merged.append(("both", v_name, chosen))
            matched.add(v_norm)
    for v_norm, (v_name, v_summary) in voice_by_norm.items():
        if v_norm not in matched:
            merged.append(("voice", v_name, v_summary))
    for m_norm, (m_name, m_summary) in meeting_by_norm.items():
        if m_norm not in matched:
            merged.append(("meeting", m_name, m_summary))
    return merged


def render_items(items: list[tuple[str, str, str]]) -> str:
    """Render merged items as plain markdown bullets, no source pills."""
    lines = []
    for _source, name, summary in items:
        lines.append(f"- **{name}**: {summary}")
    return "\n".join(lines)


def has_themed_content(label: str) -> bool:
    """True iff the week has at least one Themes or Problems item.

    Auditor "What I noticed" sections aren't shown in the viewer anymore, so
    they don't qualify a week as themed on their own. Weeks where Wispr
    export ran but theming hasn't fired yet (placeholder file only) return
    False so they never surface until they're actually populated.
    """
    voice_path = WEEKS_DIR / f"{label}.md"
    meeting_path = MEETING_WEEKS_DIR / f"{label}.md"

    voice_text = voice_path.read_text() if voice_path.exists() else ""
    meeting_text = meeting_path.read_text() if meeting_path.exists() else ""

    voice_themes = parse_items_from_section(voice_text, "On my mind", r".+")
    meeting_themes = parse_items_from_section(meeting_text, "On my mind", r".+")
    voice_problems = parse_items_from_section(voice_text, r"Problems\s*I[\'’]m solving", r".+")
    meeting_problems = parse_items_from_section(meeting_text, r"Problems\s*I[\'’]m solving", r".+")
    return bool(voice_themes or meeting_themes or voice_problems or meeting_problems)


def assemble_week_markdown(label: str) -> str:
    """Build a clean unified weekly view.

    Drops Stats / Voice-wrapper / Meetings-this-week sections.
    Consolidates voice + meeting themes/problems into ONE list each, with
    source pills marking which corpus surfaced each item. Auditor sections
    (What I noticed) are intentionally NOT rendered — the viewer is the
    weekly recap surface; cross-week analysis lives in
    data/master/20_trends/ and digest emails for users who want it.
    """
    voice_path = WEEKS_DIR / f"{label}.md"
    meeting_path = MEETING_WEEKS_DIR / f"{label}.md"

    voice_text = voice_path.read_text() if voice_path.exists() else ""
    meeting_text = meeting_path.read_text() if meeting_path.exists() else ""

    # Themes
    voice_themes = parse_items_from_section(voice_text, "On my mind", r".+")
    meeting_themes = parse_items_from_section(meeting_text, "On my mind", r".+")
    themes = merge_sources(voice_themes, meeting_themes)

    # Problems
    voice_problems = parse_items_from_section(voice_text, r"Problems\s*I[\'’]m solving", r".+")
    meeting_problems = parse_items_from_section(meeting_text, r"Problems\s*I[\'’]m solving", r".+")
    problems = merge_sources(voice_problems, meeting_problems)

    parts: list[str] = []

    if themes:
        parts += ["## On my mind", "", render_items(themes), ""]
    if problems:
        parts += ["## Problems I'm solving", "", render_items(problems), ""]
    if not themes and not problems:
        # Distinguish "no raw data captured" from "raw data exists but theming
        # hasn't run yet." If a Wispr stub exists with the placeholder LLM line,
        # the user just needs to run the theming pass.
        unthemed = (
            voice_path.exists()
            and "_LLM theme pass" in voice_text
        )
        if unthemed:
            parts.append(
                "_This week is captured but not themed yet. "
                "Click the sync pill in the corner to run the LLM theming pass._"
            )
        else:
            parts.append(
                "_No content for this week._ "
                "_Either no dictation/meetings happened, or the exporters haven't run since._"
            )

    return strip_vertex_anchors("\n".join(parts))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Local-only viewer; never want a stale cache after a rebuild -->
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Wispr Thoughts</title>
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<script src="https://cdn.jsdelivr.net/npm/marked@13/marked.min.js"></script>
<style>
  /* Newspaper palette: warm cream paper, dark warm-black ink, classic blue
     links. Light is the default; data-theme="dark" on <html> flips to a
     warm-dark inverse. Theme toggle in the top-right persists choice in
     localStorage. */
  :root {
    color-scheme: light;
    --bg: #faf6ec; --fg: #1c1916; --muted: #756f63; --accent: #1d4ed8;
    --border: #e6dfd0; --code-bg: #f3eedf; --hover: #f0e9d8;
    --shadow: 0 12px 40px rgba(28,25,22,0.12), 0 2px 6px rgba(28,25,22,0.06);
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #1a1614; --fg: #ece5d4; --muted: #8c8479; --accent: #93c5fd;
    --border: #2d2723; --code-bg: #221c19; --hover: #2a221d;
    --shadow: 0 12px 40px rgba(0,0,0,0.55), 0 2px 6px rgba(0,0,0,0.4);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", system-ui, sans-serif;
    line-height: 1.55;
  }

  /* Hero */
  .hero {
    max-width: 880px; margin: 0 auto; padding: 80px 24px 12px; text-align: center;
    position: relative;
  }

  /* Top-right cluster: theme toggle + sync pill */
  .corner-controls {
    position: absolute; top: 24px; right: 24px;
    display: flex; align-items: center; gap: 8px;
  }
  .theme-toggle {
    background: var(--bg); border: 1px solid var(--border);
    color: var(--muted); cursor: pointer; padding: 0;
    width: 30px; height: 30px; border-radius: 999px;
    display: inline-flex; align-items: center; justify-content: center;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .theme-toggle:hover { background: var(--hover); color: var(--fg); }
  .theme-toggle svg { width: 14px; height: 14px; }
  /* Show moon in light mode, sun in dark mode */
  .theme-toggle .icon-sun { display: none; }
  :root[data-theme="dark"] .theme-toggle .icon-moon { display: none; }
  :root[data-theme="dark"] .theme-toggle .icon-sun  { display: block; }

  /* Sync pill */
  .sync-pill {
    background: var(--bg); border: 1px solid var(--border);
    color: var(--muted); cursor: pointer; padding: 6px 12px;
    border-radius: 999px; font-size: 12px; font-family: inherit;
    display: inline-flex; align-items: center; gap: 8px;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
    font-variant-numeric: tabular-nums;
  }
  .sync-pill:hover { background: var(--hover); color: var(--fg); }
  .sync-pill:disabled { cursor: not-allowed; opacity: 0.7; }
  .sync-dot {
    width: 7px; height: 7px; border-radius: 999px;
    background: var(--muted); display: inline-block;
  }
  .sync-pill.fresh .sync-dot      { background: rgb(34,197,94); }
  .sync-pill.stale .sync-dot      { background: rgb(245,158,11); }
  .sync-pill.very-stale .sync-dot { background: rgb(239,68,68); }
  .sync-pill.offline .sync-dot    { background: var(--muted); }
  .sync-pill.syncing .sync-dot {
    background: rgb(74,108,247);
    animation: sync-pulse 1.2s ease-in-out infinite;
  }
  @keyframes sync-pulse {
    0%, 100% { opacity: 0.4; transform: scale(0.8); }
    50%      { opacity: 1;   transform: scale(1.2); }
  }
  @media (max-width: 560px) {
    .corner-controls { position: static; justify-content: center; margin: 0 auto 16px; }
  }
  .hero h1 {
    font-family: ui-serif, "New York", Charter, Cambria, Georgia, serif;
    font-weight: 500; font-size: clamp(48px, 8vw, 72px);
    letter-spacing: -0.02em; line-height: 1.05;
    margin: 0 0 20px;
  }
  .nav-row {
    display: inline-flex; align-items: center; gap: 6px;
    color: var(--muted); font-size: 16px; font-variant-numeric: tabular-nums;
  }
  .nav-btn {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 22px; line-height: 1; padding: 4px 10px; border-radius: 6px;
    transition: background 0.1s, color 0.1s;
  }
  .nav-btn:hover:not(:disabled) { background: var(--hover); color: var(--fg); }
  .nav-btn:disabled { opacity: 0.25; cursor: not-allowed; }
  .date-range { padding: 0 4px; }

  /* Command palette + settings modal share this body lock */
  body.palette-open { overflow: hidden; overscroll-behavior: none; }
  .palette-overlay {
    position: fixed; inset: 0; z-index: 100; display: none;
    background: rgba(0,0,0,0.45); backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    overscroll-behavior: contain;
  }
  .palette-overlay.open { touch-action: none; }
  .palette-overlay.open {
    display: flex; align-items: flex-start; justify-content: center; padding-top: 16vh;
  }
  .palette {
    width: 92%; max-width: 560px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 14px;
    box-shadow: var(--shadow); overflow: hidden;
  }
  .palette input {
    width: 100%; padding: 18px 22px; font-size: 16px;
    background: transparent; border: none; color: var(--fg);
    border-bottom: 1px solid var(--border); outline: none;
    font-family: inherit;
  }
  .palette input::placeholder { color: var(--muted); }
  .palette ul {
    list-style: none; margin: 0; padding: 6px;
    max-height: 50vh; overflow-y: auto;
    overscroll-behavior: contain;
  }
  .palette li {
    padding: 10px 14px; border-radius: 8px; cursor: pointer;
    color: var(--fg); font-variant-numeric: tabular-nums;
  }
  .palette li.active { background: var(--hover); }
  .palette li.empty {
    color: var(--muted); justify-content: center; cursor: default; padding: 20px 14px;
  }
  .palette li.empty:hover { background: transparent; }

  main {
    max-width: 760px; margin: 0 auto; padding: 8px 24px 96px;
  }
  main h1 { font-size: 28px; margin-top: 0; margin-bottom: 8px; line-height: 1.2; }
  main h2 { font-size: 20px; margin-top: 32px; margin-bottom: 12px; padding-bottom: 6px;
            border-bottom: 1px solid var(--border); }
  main h3 { font-size: 16px; margin-top: 20px; }
  main ul { padding-left: 22px; }
  main li { margin: 6px 0; }
  main li strong { color: var(--accent); }
  main p { margin: 8px 0; }

  /* Source pills next to each item */
  .src-tag {
    display: inline-block; font-size: 10px; font-weight: 600;
    padding: 1px 7px; border-radius: 999px; margin-right: 8px;
    text-transform: uppercase; letter-spacing: 0.04em;
    vertical-align: 1px; line-height: 1.4;
  }
  .src-tag.voice {
    background: rgba(74,108,247,0.12); color: var(--accent);
    border: 1px solid rgba(74,108,247,0.3);
  }
  .src-tag.meeting {
    background: rgba(34,197,94,0.12); color: rgb(22,163,74);
    border: 1px solid rgba(34,197,94,0.3);
  }
  .src-tag.both {
    background: rgba(245,158,11,0.14); color: rgb(180,83,9);
    border: 1px solid rgba(245,158,11,0.4);
  }
  main code {
    background: var(--code-bg); padding: 2px 6px; border-radius: 4px;
    font-family: ui-monospace, "SF Mono", monospace; font-size: 0.9em;
  }
  main pre { background: var(--code-bg); padding: 12px; border-radius: 8px; overflow-x: auto; }
  main blockquote {
    border-left: 3px solid var(--border); margin-left: 0; padding-left: 16px;
    color: var(--muted);
  }
  details { margin: 8px 0; }
  summary { cursor: pointer; color: var(--muted); font-size: 0.9em; }
  .empty { color: var(--muted); font-style: italic; padding: 40px 0; text-align: center; }
  /* Settings modal (sync pill click) */
  .sync-overlay {
    position: fixed; inset: 0; z-index: 110; display: none;
    background: rgba(0,0,0,0.45); backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }
  .sync-overlay.open {
    display: flex; align-items: flex-start; justify-content: center;
    padding: 6vh 16px; overflow-y: auto;
  }
  .sync-modal {
    width: 100%; max-width: 600px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 14px;
    box-shadow: var(--shadow); padding: 22px 24px 18px;
  }
  .sync-header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin-bottom: 4px;
  }
  .sync-header h2 {
    font-family: ui-serif, "New York", Charter, Cambria, Georgia, serif;
    font-weight: 500; font-size: 26px; letter-spacing: -0.01em;
    margin: 0; line-height: 1.15;
  }
  .sync-subtitle {
    color: var(--muted); margin: 0 0 16px; font-size: 13px;
  }
  .settings-card {
    border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; margin: 0 0 12px;
    background: var(--bg);
  }
  .settings-card h3 {
    margin: 0 0 4px; font-size: 13px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    border: none; padding: 0;
  }
  .settings-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center; column-gap: 14px; margin: 10px 0;
  }
  .settings-row.input {
    grid-template-columns: minmax(0, 1fr) minmax(220px, auto);
  }
  .settings-row .label-text {
    font-size: 14px; color: var(--fg);
  }
  .settings-row .label-sub {
    color: var(--muted); font-size: 12px; margin-top: 2px; line-height: 1.4;
  }
  .settings-row .label-sub a {
    color: var(--accent); text-decoration: none;
  }
  .settings-row .label-sub a:hover { text-decoration: underline; }
  .settings-row .control {
    justify-self: end; display: flex; align-items: center; gap: 8px; min-width: 0;
  }
  .settings-row input[type="text"],
  .settings-row input[type="email"],
  .settings-row input[type="password"],
  .settings-row select {
    background: var(--code-bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 10px; font-size: 13px; font-family: inherit;
    width: 100%; max-width: 100%; outline: none;
    min-width: 0;
  }
  .settings-row .control select { width: auto; min-width: 110px; }
  .settings-row input:focus,
  .settings-row select:focus {
    border-color: var(--accent);
  }
  /* iOS-style toggle */
  .toggle {
    position: relative; display: inline-block;
    width: 38px; height: 22px; flex-shrink: 0;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle .slider {
    position: absolute; cursor: pointer; inset: 0;
    background-color: var(--border); border-radius: 999px;
    transition: 0.18s;
  }
  .toggle .slider::before {
    position: absolute; content: "";
    height: 16px; width: 16px; left: 3px; bottom: 3px;
    background-color: white; border-radius: 999px;
    transition: 0.18s;
    box-shadow: 0 1px 2px rgba(0,0,0,0.3);
  }
  .toggle input:checked + .slider { background-color: var(--accent); }
  .toggle input:checked + .slider::before { transform: translateX(16px); }
  .toggle input:disabled + .slider { opacity: 0.5; cursor: not-allowed; }

  .source-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center; column-gap: 12px; margin: 8px 0; font-size: 13px;
  }
  .source-row .src-name {
    color: var(--fg); display: flex; flex-direction: column; min-width: 0;
  }
  .source-row .src-name .src-hint {
    color: var(--muted); font-size: 11px; margin-top: 2px;
  }
  .source-row .src-status {
    font-size: 11px; padding: 2px 8px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.05em;
    font-weight: 600; white-space: nowrap;
  }
  .source-extra {
    grid-column: 1 / -1; padding-top: 6px;
    display: flex; gap: 8px; align-items: center;
  }
  .source-extra input[type="text"] {
    flex: 1; background: var(--code-bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 6px 10px; font-size: 12px;
    font-family: ui-monospace, "SF Mono", monospace;
    outline: none;
  }
  .source-extra input:focus { border-color: var(--accent); }
  .src-status.detected {
    background: rgba(34,197,94,0.14); color: rgb(22,163,74);
    border: 1px solid rgba(34,197,94,0.3);
  }
  .src-status.missing {
    background: rgba(107,107,107,0.14); color: var(--muted);
    border: 1px solid var(--border);
  }
  .src-status.disabled {
    background: transparent; color: var(--muted);
    border: 1px dashed var(--border);
  }
  .sync-log {
    background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin: 0 0 14px;
    font-family: ui-monospace, "SF Mono", monospace; font-size: 12px;
    line-height: 1.55; color: var(--fg);
    max-height: 38vh; overflow-y: auto;
    white-space: pre-wrap; word-wrap: break-word;
  }
  .sync-actions {
    display: flex; justify-content: space-between; align-items: center;
    gap: 10px; margin-top: 4px;
  }
  .sync-actions .left { color: var(--muted); font-size: 12px; }
  .sync-actions .right { display: flex; gap: 10px; }
  .sync-btn {
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 16px; font-size: 13px; cursor: pointer;
    font-family: inherit; transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .sync-btn:disabled { cursor: not-allowed; opacity: 0.5; }
  .sync-btn-secondary { background: transparent; color: var(--muted); }
  .sync-btn-secondary:hover:not(:disabled) { background: var(--hover); color: var(--fg); }
  .sync-btn-primary {
    background: var(--accent); border-color: var(--accent); color: white;
  }
  .sync-btn-primary:hover:not(:disabled) { filter: brightness(1.05); }
  .test-result {
    font-size: 12px; margin-top: 8px;
  }
  .test-result.ok { color: rgb(22,163,74); }
  .test-result.err { color: rgb(220,38,38); }

  /* Prompts modal (P key, footer link, click outside to close) */
  .prompts-overlay {
    position: fixed; inset: 0; z-index: 100; display: none;
    background: rgba(0,0,0,0.45); backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }
  .prompts-overlay.open {
    display: flex; align-items: flex-start; justify-content: center;
    padding: 6vh 16px; overflow-y: auto;
  }
  .prompts-modal {
    width: 100%; max-width: 720px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 14px;
    box-shadow: var(--shadow); padding: 24px 28px 20px;
  }
  .prompts-header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin-bottom: 4px;
  }
  .prompts-header h2 {
    font-family: ui-serif, "New York", Charter, Cambria, Georgia, serif;
    font-weight: 500; font-size: 28px; letter-spacing: -0.01em;
    margin: 0; line-height: 1.15;
  }
  .prompts-close {
    background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 28px; line-height: 1; padding: 0 6px;
    border-radius: 6px; transition: background 0.1s, color 0.1s;
  }
  .prompts-close:hover { background: var(--hover); color: var(--fg); }
  .prompts-intro {
    color: var(--muted); margin: 0 0 24px; font-size: 14px;
  }
  .prompts-intro code {
    background: var(--code-bg); padding: 1px 6px; border-radius: 4px;
    font-family: ui-monospace, "SF Mono", monospace; font-size: 13px;
  }
  .prompts-modal h3 {
    font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted);
    margin: 24px 0 10px; border-bottom: none; padding: 0;
  }
  .prompt-card {
    position: relative; margin: 0 0 12px;
    background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden;
  }
  .prompt-card pre {
    margin: 0; padding: 14px 80px 14px 16px;
    background: transparent; border: none;
    font-family: ui-monospace, "SF Mono", monospace;
    font-size: 13px; line-height: 1.55;
    white-space: pre-wrap; word-wrap: break-word;
    color: var(--fg);
  }
  .prompt-card pre code {
    background: transparent; padding: 0; border-radius: 0;
    font-size: inherit; color: inherit;
  }
  .copy-btn {
    position: absolute; top: 10px; right: 10px;
    background: var(--bg); border: 1px solid var(--border);
    color: var(--muted); cursor: pointer;
    padding: 4px 10px; border-radius: 6px;
    font-size: 12px; font-family: inherit;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .copy-btn:hover { background: var(--hover); color: var(--fg); }
  .copy-btn.copied {
    background: rgba(34,197,94,0.15);
    border-color: rgba(34,197,94,0.4);
    color: rgb(22,163,74);
  }
  .meta {
    color: var(--muted); font-size: 12px; padding: 32px 24px 48px;
    text-align: center;
  }
  .meta a {
    color: var(--muted); text-decoration: underline;
    text-decoration-color: var(--border); text-underline-offset: 3px;
  }
  .meta a:hover { color: var(--fg); text-decoration-color: var(--fg); }
  kbd {
    background: var(--code-bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 6px; font-family: ui-monospace, "SF Mono", monospace; font-size: 0.85em;
  }
</style>
</head>
<body>

<div class="hero">
  <div class="corner-controls">
    <button class="theme-toggle" id="themeToggle" title="Toggle light/dark" aria-label="Toggle theme">
      <svg class="icon-moon" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
      <svg class="icon-sun" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="4"/>
        <line x1="12" y1="2"  x2="12" y2="4"/>
        <line x1="12" y1="20" x2="12" y2="22"/>
        <line x1="4.93"  y1="4.93"  x2="6.34"  y2="6.34"/>
        <line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/>
        <line x1="2"  y1="12" x2="4"  y2="12"/>
        <line x1="20" y1="12" x2="22" y2="12"/>
        <line x1="4.93"  y1="19.07" x2="6.34"  y2="17.66"/>
        <line x1="17.66" y1="6.34"  x2="19.07" y2="4.93"/>
      </svg>
    </button>
    <button class="sync-pill" id="syncPill" title="Click to sync your data">
      <span class="sync-dot" id="syncDot"></span>
      <span class="sync-label" id="syncLabel">Checking…</span>
    </button>
  </div>
  <h1>Weekly Digest</h1>
  <div class="nav-row">
    <button class="nav-btn" id="prev" title="Previous week (←)">‹</button>
    <span class="date-range" id="date-range"></span>
    <button class="nav-btn" id="next" title="Next week (→)">›</button>
  </div>
</div>

<main id="content"></main>

<div class="sync-overlay" id="syncOverlay">
  <div class="sync-modal" role="dialog" aria-label="Settings">
    <div class="sync-header">
      <h2 id="syncModalTitle">Settings</h2>
      <button class="prompts-close" id="syncClose" aria-label="Close">×</button>
    </div>
    <p class="sync-subtitle" id="syncSubtitle">Auto-sync schedule and source detection.</p>

    <div class="sync-body" id="syncBody">

      <div class="settings-card">
        <h3>Auto-sync</h3>
        <div class="settings-row">
          <div class="label-text">Run weekly automatically</div>
          <div class="control">
            <label class="toggle">
              <input type="checkbox" id="autoSyncToggle">
              <span class="slider"></span>
            </label>
          </div>
        </div>
        <div class="settings-row">
          <div class="label-text">Schedule</div>
          <div class="control" style="color: var(--muted); font-size: 13px;">
            Sundays at 8:00 AM
          </div>
        </div>
        <!-- Always visible. Two states: caught up vs N weeks pending. -->
        <div class="settings-row" id="backfillRow">
          <div>
            <div class="label-text" id="backfillTitle">Past weeks</div>
            <div class="label-sub" id="backfillCountLabel">Checking…</div>
          </div>
          <div class="control">
            <button class="sync-btn sync-btn-secondary" id="backfillStart" disabled>Checking…</button>
          </div>
        </div>
      </div>

      <div class="settings-card">
        <h3>Sources</h3>
        <div id="sourcesList"></div>
      </div>

      <pre class="sync-log" id="syncLog" hidden></pre>
    </div>

    <div class="sync-actions" id="syncActions">
      <div class="left" id="settingsSavedHint"></div>
      <div class="right">
        <button class="sync-btn sync-btn-secondary" id="syncCancel">Close</button>
        <!-- Hidden by default. Only appears mid-sync (Stop sync) or just-after (Reload viewer). -->
        <button class="sync-btn sync-btn-primary" id="syncStart" hidden>Sync now</button>
      </div>
    </div>
  </div>
</div>

<div class="prompts-overlay" id="promptsOverlay">
  <div class="prompts-modal" role="dialog" aria-label="Prompts to ask">
    <div class="prompts-header">
      <h2>Prompts to ask</h2>
      <button class="prompts-close" id="promptsClose" aria-label="Close">×</button>
    </div>
    <p class="prompts-intro">
      Open <code>~/code/wispr-thoughts</code> in Claude Code and paste any of these.
      The agent will read your <code>data/</code> folder and answer using what you've actually said.
    </p>

    <h3>Time-travel</h3>
    <div class="prompt-card">
      <pre><code id="p1">Read all my themes from May through July 2024. What was I working on then? In hindsight, were those actually problems worth solving, or was I optimizing the wrong things?</code></pre>
      <button class="copy-btn" data-target="p1">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p2">Find the first week any of my current major projects shows up across my weeks. How long has each one actually been on my mind, and how has my framing of it shifted between then and now?</code></pre>
      <button class="copy-btn" data-target="p2">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p3">What was I thinking about exactly one year ago today? Have I made progress on those threads, abandoned them, or replaced them with same-shaped problems under different names?</code></pre>
      <button class="copy-btn" data-target="p3">Copy</button>
    </div>

    <h3>Pattern detection</h3>
    <div class="prompt-card">
      <pre><code id="p4">Across all my weeks, find every theme that recurs in 5+ weeks. Group them by underlying drive — be aggressive about merging things that look different but share a lever. I want to see whether what looks like 12 threads is really 4.</code></pre>
      <button class="copy-btn" data-target="p4">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p5">Find topics that always show up in the same week as another topic, even when I never link them explicitly. Cross-pollinations I've never noticed.</code></pre>
      <button class="copy-btn" data-target="p5">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p6">When I'm dictating in Wispr, what topics correlate with which apps were on screen? Am I doing my best thinking somewhere specific (Cursor vs Slack vs Chrome)?</code></pre>
      <button class="copy-btn" data-target="p6">Copy</button>
    </div>

    <h3>Blind spots</h3>
    <div class="prompt-card">
      <pre><code id="p7">What do I keep almost-saying but never quite getting to? Half-finished thoughts, themes that surface and disappear, problems I name but never actually frame.</code></pre>
      <button class="copy-btn" data-target="p7">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p8">What problems have I been "solving" for 8+ weeks where my framing hasn't shifted? Don't be diplomatic.</code></pre>
      <button class="copy-btn" data-target="p8">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p9">If you only saw my last 90 days of dictation and meetings, what would you say I'm avoiding?</code></pre>
      <button class="copy-btn" data-target="p9">Copy</button>
    </div>

    <h3>Outsider view</h3>
    <div class="prompt-card">
      <pre><code id="p10">Pretend you're a senior operator who only has my voice and meetings to advise me. Based purely on what I've said the last month, what's the one piece of advice you'd give? Cite specific themes/problems.</code></pre>
      <button class="copy-btn" data-target="p10">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p11">Looking across everything I've said, what does it reveal about my real priorities, not my stated ones? Where do my words and my actual time disagree?</code></pre>
      <button class="copy-btn" data-target="p11">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p12">If a board member or co-founder only had my voice and meetings to evaluate me, what would they conclude about my strategic clarity, focus, and execution quality?</code></pre>
      <button class="copy-btn" data-target="p12">Copy</button>
    </div>

    <h3>Voice and identity</h3>
    <div class="prompt-card">
      <pre><code id="p13">What are the 5-10 phrases or mental frames I use repeatedly across both dictation AND meetings? Build me a glossary of my distinctive vocabulary.</code></pre>
      <button class="copy-btn" data-target="p13">Copy</button>
    </div>
    <div class="prompt-card">
      <pre><code id="p14">How does my voice in solo dictation differ from my voice in meetings? Different problems, different framing, different vocabulary, different hedges? What does the gap say about what I share vs. what stays internal?</code></pre>
      <button class="copy-btn" data-target="p14">Copy</button>
    </div>
  </div>
</div>

<div class="palette-overlay" id="paletteOverlay">
  <div class="palette" role="dialog" aria-label="Jump to week">
    <input id="paletteInput" type="text"
           placeholder="Jump to a week — try 'April', '2025', or 'W17'"
           autocomplete="off" spellcheck="false">
    <ul id="paletteList"></ul>
  </div>
</div>

<div class="meta">
  <kbd>←</kbd> <kbd>→</kbd> navigate · <kbd>⌘ K</kbd> jump · <kbd>T</kbd> today · <kbd>P</kbd> <a href="#" id="promptsTrigger">prompts</a>
</div>

<script>
  // ---------- Theme toggle (must run before any paint) ----------
  const THEME_KEY = 'wispr-theme';
  function applyTheme(theme) {
    if (theme === 'dark') document.documentElement.dataset.theme = 'dark';
    else delete document.documentElement.dataset.theme;
  }
  try {
    applyTheme(localStorage.getItem(THEME_KEY) || 'light');
  } catch {}
  document.getElementById('themeToggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch {}
  });

  const WEEKS = __WEEKS_JSON__;

  // Default to the most recently *completed* week (Saturday in the past), not
  // the in-progress current week. Today's date in local YYYY-MM-DD so the
  // comparison matches the week's Saturday ISO date.
  function todayISOLocal() {
    const d = new Date();
    const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }
  function defaultWeekIdx() {
    const today = todayISOLocal();
    for (let i = WEEKS.length - 1; i >= 0; i--) {
      if (WEEKS[i].sat && WEEKS[i].sat < today) return i;
    }
    return WEEKS.length - 1;
  }
  let idx = defaultWeekIdx();

  // Allow inline HTML (source pills) inside parsed markdown
  marked.use({ gfm: true, breaks: false, mangle: false });

  // Pre-build lowercased search corpus per week (label, range, full month names, year)
  WEEKS.forEach(w => { w._search = (w.label + " " + w.range + " " + w.search).toLowerCase(); });

  // ---------- Render main page ----------
  function render() {
    const w = WEEKS[idx];
    document.title = 'Weekly Digest · ' + w.range;
    document.getElementById('date-range').textContent = w.range;
    document.getElementById('content').innerHTML = marked.parse(w.markdown || '');
    document.getElementById('prev').disabled = idx === 0;
    document.getElementById('next').disabled = idx === WEEKS.length - 1;
    window.scrollTo(0, 0);
  }

  document.getElementById('prev').onclick = () => { if (idx > 0) { idx--; render(); } };
  document.getElementById('next').onclick = () => { if (idx < WEEKS.length - 1) { idx++; render(); } };

  // ---------- Command palette (⌘K) ----------
  let paletteOpen = false;
  let paletteSelected = 0;
  let paletteResults = [];

  const overlayEl = document.getElementById('paletteOverlay');
  const inputEl   = document.getElementById('paletteInput');
  const listEl    = document.getElementById('paletteList');

  function openPalette() {
    paletteOpen = true;
    overlayEl.classList.add('open');
    document.body.classList.add('palette-open');
    inputEl.value = '';
    inputEl.focus();
    filterPalette('');
  }
  function closePalette() {
    paletteOpen = false;
    overlayEl.classList.remove('open');
    document.body.classList.remove('palette-open');
  }
  function filterPalette(q) {
    q = q.trim().toLowerCase();
    const reversed = WEEKS.slice().reverse();  // newest first
    paletteResults = q
      ? reversed.filter(w => w._search.includes(q))
      : reversed;
    paletteSelected = 0;
    renderPaletteList();
  }
  function jumpTo(week) {
    idx = WEEKS.indexOf(week);
    closePalette();
    render();
  }
  function renderPaletteList() {
    listEl.innerHTML = '';
    if (paletteResults.length === 0) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = 'No matching weeks';
      listEl.appendChild(li);
      return;
    }
    paletteResults.forEach((w, i) => {
      const li = document.createElement('li');
      if (i === paletteSelected) li.classList.add('active');
      li.textContent = w.range;
      li.onclick = () => jumpTo(w);
      li.onmouseenter = () => {
        if (paletteSelected !== i) {
          paletteSelected = i;
          listEl.querySelectorAll('li').forEach((el, j) =>
            el.classList.toggle('active', j === i));
        }
      };
      listEl.appendChild(li);
    });
    const active = listEl.querySelector('li.active');
    if (active) active.scrollIntoView({ block: 'nearest' });
  }

  inputEl.addEventListener('input', e => filterPalette(e.target.value));
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      paletteSelected = Math.min(paletteSelected + 1, paletteResults.length - 1);
      renderPaletteList();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      paletteSelected = Math.max(paletteSelected - 1, 0);
      renderPaletteList();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (paletteResults[paletteSelected]) jumpTo(paletteResults[paletteSelected]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closePalette();
    }
  });
  overlayEl.addEventListener('click', e => {
    if (e.target === overlayEl) closePalette();
  });

  // ---------- Backfill row (lives inside the Auto-sync card) ----------
  const backfillRowEl    = document.getElementById('backfillRow');
  const backfillTitleEl  = document.getElementById('backfillTitle');
  const backfillCountEl  = document.getElementById('backfillCountLabel');
  const backfillStartEl  = document.getElementById('backfillStart');
  let LAST_BACKFILL_COUNT = null;

  function setBackfillCaughtUp() {
    backfillTitleEl.textContent  = 'Past weeks';
    backfillCountEl.textContent  = 'All themes are up to date.';
    backfillStartEl.textContent  = 'Caught up';
    backfillStartEl.disabled     = true;
  }
  function setBackfillPending(count) {
    backfillTitleEl.textContent = count === 1
      ? '1 past week needs theming'
      : `${count} past weeks need theming`;
    backfillCountEl.textContent =
      'Re-run themes for weeks captured before auto-sync started.';
    backfillStartEl.textContent = `Backfill ${count}`;
    backfillStartEl.disabled    = false;
  }
  function setBackfillError(msg) {
    backfillTitleEl.textContent = 'Past weeks';
    backfillCountEl.textContent = msg;
    backfillStartEl.textContent = 'Retry';
    backfillStartEl.disabled    = false;
  }

  async function refreshBackfillRow() {
    if (!HAS_API) {
      setBackfillError('Server offline. Start it: python3 src/serve.py');
      return;
    }
    try {
      const r = await fetch('/api/backfill', { cache: 'no-store' });
      if (!r.ok) {
        setBackfillError(`Couldn't check: HTTP ${r.status}`);
        return;
      }
      const data = await r.json();
      const count = data.unthemed_count || 0;
      LAST_BACKFILL_COUNT = count;
      if (count === 0) setBackfillCaughtUp();
      else             setBackfillPending(count);
    } catch (e) {
      setBackfillError(`Couldn't check: ${e}`);
    }
  }

  backfillStartEl.addEventListener('click', async () => {
    // If we already know the count is 0, just re-check and exit — no point
    // hitting the POST endpoint when nothing's pending.
    if (LAST_BACKFILL_COUNT === 0) {
      await refreshBackfillRow();
      return;
    }
    const originalLabel = backfillStartEl.textContent;
    backfillStartEl.disabled = true;
    backfillStartEl.textContent = 'Starting…';
    try {
      const r = await fetch('/api/backfill', { method: 'POST' });
      if (r.status === 409) {
        backfillCountEl.textContent = 'A sync is already running. Try again when it finishes.';
        backfillStartEl.disabled = false;
        backfillStartEl.textContent = originalLabel;
        return;
      }
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        backfillCountEl.textContent = `Failed: ${data.error || r.status}`;
        backfillStartEl.disabled = false;
        backfillStartEl.textContent = originalLabel;
        return;
      }
      // Nothing-to-do response: refresh state and bail out
      if (!data.job_id) {
        await refreshBackfillRow();
        return;
      }
      // Real job started — switch the modal into log-streamer mode
      syncTitleEl.textContent = 'Backfilling…';
      syncSubtitleEl.hidden = true;
      syncStartEl.hidden = false;
      syncStartEl.disabled = false;
      syncStartEl.textContent = 'Stop sync';
      syncLogEl.hidden = false;
      syncLogEl.textContent = `Backfilling ${data.weeks ? data.weeks.length : '…'} weeks…\n`;
      if (syncPollHandle) clearInterval(syncPollHandle);
      syncPollHandle = setInterval(pollSyncLog, 1000);
      pollSyncLog();
    } catch (e) {
      backfillCountEl.textContent = `Failed: ${e}`;
      backfillStartEl.disabled = false;
      backfillStartEl.textContent = originalLabel;
    }
  });

  // ---------- Sync pill (polls /api/status, click → settings modal) ----------
  const syncPillEl  = document.getElementById('syncPill');
  const syncDotEl   = document.getElementById('syncDot');
  const syncLabelEl = document.getElementById('syncLabel');

  // file:// origins can't talk to localhost API; pill still opens, but only
  // shows a "start the server" hint
  const HAS_API = window.location.protocol === 'http:' || window.location.protocol === 'https:';

  let LAST_STATUS  = null;
  let LAST_SCHEDULE = null;

  function setSyncPill(state, label) {
    syncPillEl.classList.remove('fresh', 'stale', 'very-stale', 'offline', 'syncing');
    if (state) syncPillEl.classList.add(state);
    syncLabelEl.textContent = label;
  }

  function pillLabelFromState(status, schedule) {
    if (!status) return 'Click to set up';
    if (status.syncing) return 'Syncing…';
    if (status.freshness === 'never' && (!status.last_sync)) return 'Click to set up';
    return 'synced';
  }

  function pillStateFromStatus(status, schedule) {
    if (!status) return 'offline';
    if (status.syncing) return 'syncing';
    return status.freshness;
  }

  async function refreshStatus() {
    if (!HAS_API) {
      setSyncPill('offline', 'Click to set up');
      // Pill stays clickable; the modal explains how to start the server.
      syncPillEl.disabled = false;
      return;
    }
    try {
      const [sRes, schRes] = await Promise.all([
        fetch('/api/status',   { cache: 'no-store' }),
        fetch('/api/schedule', { cache: 'no-store' }),
      ]);
      LAST_STATUS  = sRes.ok ? await sRes.json() : null;
      LAST_SCHEDULE = schRes.ok ? await schRes.json() : null;
      // Pill stays clickable even while syncing — opens the settings drawer
      // so the user can still toggle auto-sync, paste API keys, etc. The
      // "Sync now" button inside the drawer is what gates concurrent runs.
      syncPillEl.disabled = false;
      setSyncPill(
        pillStateFromStatus(LAST_STATUS, LAST_SCHEDULE),
        pillLabelFromState(LAST_STATUS, LAST_SCHEDULE),
      );
    } catch (e) {
      LAST_STATUS = null; LAST_SCHEDULE = null;
      setSyncPill('offline', 'Click to set up');
      syncPillEl.disabled = false;
    }
  }
  refreshStatus();
  setInterval(refreshStatus, 30000);

  // ---------- Settings modal ----------
  const syncOverlayEl   = document.getElementById('syncOverlay');
  const syncCloseEl     = document.getElementById('syncClose');
  const syncCancelEl    = document.getElementById('syncCancel');
  const syncStartEl     = document.getElementById('syncStart');
  const syncSubtitleEl  = document.getElementById('syncSubtitle');
  const syncLogEl       = document.getElementById('syncLog');
  const syncTitleEl     = document.getElementById('syncModalTitle');
  const settingsSavedEl = document.getElementById('settingsSavedHint');

  const autoSyncToggleEl = document.getElementById('autoSyncToggle');
  // Schedule is fixed at Sunday 8:00 AM. No selectors; the constants below
  // are what every install/remove call uses.
  const FIXED_WEEKDAY = 0;
  const FIXED_HOUR    = 8;
  const FIXED_MINUTE  = 0;

  const sourcesListEl = document.getElementById('sourcesList');

  let syncOpen = false;
  let syncPollHandle = null;
  let settingsLoaded = false;
  let savedHintTimer = null;

  function showSavedHint(text) {
    if (savedHintTimer) clearTimeout(savedHintTimer);
    settingsSavedEl.textContent = text;
    savedHintTimer = setTimeout(() => { settingsSavedEl.textContent = ''; }, 2200);
  }


  async function loadSettings() {
    try {
      const [setRes, schRes] = await Promise.all([
        fetch('/api/settings',  { cache: 'no-store' }),
        fetch('/api/schedule', { cache: 'no-store' }),
      ]);
      if (!setRes.ok || !schRes.ok) throw new Error('settings load failed');
      const settings = await setRes.json();
      const schedule = await schRes.json();

      autoSyncToggleEl.checked = !!schedule.installed;


      // Sources
      sourcesListEl.innerHTML = '';
      const SRC_LABELS = {
        wispr:   'Wispr Flow (voice dictation)',
        fathom:  'Fathom (meeting transcripts)',
        granola: 'Granola (meeting cache)',
        notes:   'Apple Notes (parallel corpus)',
      };
      Object.entries(settings.sources || {}).forEach(([name, info]) => {
        const row = document.createElement('div');
        row.className = 'source-row';

        const nameEl = document.createElement('div');
        nameEl.className = 'src-name';
        const title = document.createElement('div');
        title.textContent = SRC_LABELS[name] || name;
        nameEl.appendChild(title);
        if (info.hint) {
          const hint = document.createElement('div');
          hint.className = 'src-hint';
          hint.textContent = info.hint;
          nameEl.appendChild(hint);
        }
        row.appendChild(nameEl);

        const tag = document.createElement('span');
        tag.className = 'src-status ' + (info.enabled ? (info.detected ? 'detected' : 'missing') : 'disabled');
        tag.textContent = info.enabled
          ? (info.detected ? 'Detected' : 'Set up')
          : 'Disabled';
        row.appendChild(tag);

        const toggleLabel = document.createElement('label');
        toggleLabel.className = 'toggle';
        const toggleInput = document.createElement('input');
        toggleInput.type = 'checkbox';
        toggleInput.checked = !!info.enabled;
        toggleInput.addEventListener('change', () => saveSourceFromUI(name, toggleInput.checked));
        const slider = document.createElement('span');
        slider.className = 'slider';
        toggleLabel.appendChild(toggleInput); toggleLabel.appendChild(slider);
        row.appendChild(toggleLabel);

        // Fathom: inline API key input when enabled but no key found
        if (name === 'fathom' && info.enabled && !info.detected) {
          const extra = document.createElement('div');
          extra.className = 'source-extra';
          const inp = document.createElement('input');
          inp.type = 'text';
          inp.placeholder = 'Paste your Fathom API key';
          inp.spellcheck = false;
          inp.autocomplete = 'off';
          const btn = document.createElement('button');
          btn.className = 'sync-btn sync-btn-secondary';
          btn.textContent = 'Save';
          btn.addEventListener('click', async () => {
            const key = inp.value.trim();
            if (!key) return;
            btn.disabled = true; btn.textContent = 'Saving…';
            try {
              const r = await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ fathom_api_key: key }),
              });
              if (r.ok) {
                showSavedHint('Fathom key saved to ~/.zshrc.');
                loadSettings();
              } else {
                const data = await r.json().catch(() => ({}));
                showSavedHint(`Save failed: ${data.error || r.status}`);
                btn.disabled = false; btn.textContent = 'Save';
              }
            } catch (e) {
              showSavedHint(`Save failed: ${e}`);
              btn.disabled = false; btn.textContent = 'Save';
            }
          });
          extra.appendChild(inp); extra.appendChild(btn);
          row.appendChild(extra);
        }

        sourcesListEl.appendChild(row);
      });

      settingsLoaded = true;
    } catch (e) {
      console.error('settings load error', e);
      settingsLoaded = false;
    }
  }

  async function saveSourceFromUI(name, enabled) {
    try {
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sources: { [name]: { enabled } } }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        showSavedHint(`Save failed: ${data.error || r.status}`);
        return;
      }
      showSavedHint(`${name} ${enabled ? 'enabled' : 'disabled'}.`);
      loadSettings();
    } catch (e) {
      showSavedHint(`Save failed: ${e}`);
    }
  }

  async function saveScheduleFromUI() {
    if (!settingsLoaded) return;
    const enabled = autoSyncToggleEl.checked;
    try {
      const r = await fetch('/api/schedule', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          enabled,
          weekday: FIXED_WEEKDAY,
          hour:    FIXED_HOUR,
          minute:  FIXED_MINUTE,
        }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        showSavedHint(`Schedule failed: ${data.error || r.status}`);
        return;
      }
      showSavedHint(enabled ? 'Auto-sync enabled.' : 'Auto-sync disabled.');
      refreshStatus();
    } catch (e) {
      showSavedHint(`Schedule failed: ${e}`);
    }
  }

  // Wire change handler for the auto-sync toggle. Schedule is fixed at
  // Sunday 8 AM, so there are no other inputs to listen on.
  autoSyncToggleEl.addEventListener('change', saveScheduleFromUI);

  function openSyncModal() {
    syncOpen = true;
    document.body.classList.add('palette-open');
    syncTitleEl.textContent = 'Settings';
    syncSubtitleEl.hidden = false;
    syncLogEl.hidden = true;
    syncLogEl.textContent = '';
    syncStartEl.hidden = true;
    syncStartEl.disabled = false;
    syncCancelEl.textContent = 'Close';
    settingsSavedEl.textContent = '';
    syncOverlayEl.classList.add('open');
    if (HAS_API) {
      loadSettings();
      refreshBackfillRow();
      // If a sync is already in flight (e.g., launchd just fired), attach
      // to it so the user can watch + stop it.
      if (LAST_STATUS && LAST_STATUS.syncing) {
        syncTitleEl.textContent = 'Syncing…';
        syncStartEl.hidden = false;
        syncStartEl.disabled = false;
        syncStartEl.textContent = 'Stop sync';
        syncCancelEl.textContent = 'Hide';
        syncLogEl.hidden = false;
        syncLogEl.textContent = 'Tailing in-progress sync…\n';
        if (syncPollHandle) clearInterval(syncPollHandle);
        syncPollHandle = setInterval(pollSyncLog, 1000);
        pollSyncLog();
      }
    }
  }
  function closeSyncModal() {
    syncOpen = false;
    document.body.classList.remove('palette-open');
    if (syncPollHandle) { clearInterval(syncPollHandle); syncPollHandle = null; }
    syncOverlayEl.classList.remove('open');
  }
  syncCloseEl.addEventListener('click', closeSyncModal);
  syncCancelEl.addEventListener('click', closeSyncModal);
  syncOverlayEl.addEventListener('click', e => {
    if (e.target === syncOverlayEl) closeSyncModal();
  });

  async function startSync() {
    syncTitleEl.textContent = 'Syncing…';
    syncLogEl.hidden = false;
    syncLogEl.textContent = 'Starting sync…\n';
    syncStartEl.disabled = true;
    syncStartEl.textContent = 'Stop sync';
    syncStartEl.disabled = false;
    syncCancelEl.textContent = 'Hide';

    try {
      const r = await fetch('/api/sync', { method: 'POST' });
      if (r.status === 409) {
        syncLogEl.textContent += '\nA sync was already in progress; tailing that one.\n';
      } else if (!r.ok) {
        syncLogEl.textContent += `\nFailed to start sync (${r.status}).\n`;
        syncStartEl.disabled = false;
        return;
      }
    } catch (e) {
      syncLogEl.textContent += `\nServer unreachable: ${e}.\n`;
      syncStartEl.disabled = false;
      return;
    }

    refreshStatus();
    syncPollHandle = setInterval(pollSyncLog, 1000);
    pollSyncLog();
  }

  async function pollSyncLog() {
    try {
      const r = await fetch('/api/sync/log', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (data.lines && data.lines.length) {
        syncLogEl.textContent = data.lines.join('\n') + '\n';
        syncLogEl.scrollTop = syncLogEl.scrollHeight;
      }
      if (data.finished) {
        clearInterval(syncPollHandle); syncPollHandle = null;
        // Idle = no job ever ran; don't surface that as "failed". Quietly
        // reset the modal back to the settings view.
        if (data.status === 'idle') {
          syncTitleEl.textContent = 'Settings';
          syncSubtitleEl.hidden = false;
          syncLogEl.hidden = true;
          syncStartEl.hidden = true;
          syncCancelEl.textContent = 'Close';
          refreshStatus();
          return;
        }
        const ok = data.status === 'completed';
        const cancelled = data.status === 'cancelled';
        syncTitleEl.textContent = ok ? 'Sync complete' : (cancelled ? 'Sync stopped' : 'Sync failed');
        if (ok) {
          syncStartEl.hidden = false;
          syncStartEl.disabled = false;
          syncStartEl.textContent = 'Reload viewer';
        } else {
          syncStartEl.hidden = true;
        }
        syncCancelEl.textContent = 'Close';
        if (ok) {
          setTimeout(() => { window.location.reload(); }, 1200);
        }
        refreshStatus();
      }
    } catch (e) {
      syncLogEl.textContent += `\nPoll error: ${e}\n`;
    }
  }

  syncStartEl.addEventListener('click', async () => {
    const label = syncStartEl.textContent;
    if (label === 'Reload viewer') {
      window.location.reload();
      return;
    }
    if (label === 'Stop sync') {
      syncStartEl.disabled = true;
      syncStartEl.textContent = 'Stopping…';
      try {
        await fetch('/api/sync/cancel', { method: 'POST' });
      } catch (e) {
        syncLogEl.textContent += `\nCancel error: ${e}\n`;
      }
      // Polling will pick up the cancelled state and reset the button.
      return;
    }
    if (!HAS_API) {
      // file:// origin: tell the user to start the server
      syncLogEl.hidden = false;
      syncLogEl.textContent =
        'Start the server, then re-open this page from http://127.0.0.1:8080/:\n\n' +
        '  python3 src/serve.py\n';
      return;
    }
    startSync();
  });

  syncPillEl.addEventListener('click', () => {
    if (syncPillEl.disabled) return;
    openSyncModal();
  });

  // ---------- Prompts modal (P) ----------
  let promptsOpen = false;
  const promptsOverlayEl = document.getElementById('promptsOverlay');
  const promptsCloseEl   = document.getElementById('promptsClose');
  const promptsTriggerEl = document.getElementById('promptsTrigger');

  function openPrompts() {
    promptsOpen = true;
    promptsOverlayEl.classList.add('open');
    document.body.classList.add('palette-open');
  }
  function closePrompts() {
    promptsOpen = false;
    promptsOverlayEl.classList.remove('open');
    document.body.classList.remove('palette-open');
  }
  promptsCloseEl.addEventListener('click', closePrompts);
  promptsTriggerEl.addEventListener('click', e => { e.preventDefault(); openPrompts(); });
  promptsOverlayEl.addEventListener('click', e => {
    if (e.target === promptsOverlayEl) closePrompts();
  });

  // ---------- Global keyboard ----------
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      paletteOpen ? closePalette() : openPalette();
      return;
    }
    if (e.key === 'Escape' && syncOpen) { closeSyncModal(); return; }
    if (e.key === 'Escape' && promptsOpen) { closePrompts(); return; }
    if (paletteOpen || syncOpen) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'p' || e.key === 'P') {
      e.preventDefault();
      promptsOpen ? closePrompts() : openPrompts();
      return;
    }
    if (promptsOpen) return;
    if (e.key === 'ArrowLeft'  && idx > 0)                  { idx--; render(); }
    if (e.key === 'ArrowRight' && idx < WEEKS.length - 1)   { idx++; render(); }
    if (e.key === 't' || e.key === 'T')                     { idx = defaultWeekIdx(); render(); }
  });

  render();

  // ---------- Copy buttons in the Prompts section ----------
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      const text = target.textContent.trim();
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        // Fallback for older browsers / file:// origins without clipboard access
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
      }
      const orig = btn.textContent;
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = orig;
        btn.classList.remove('copied');
      }, 1500);
    });
  });
</script>
</body>
</html>
"""


def main() -> None:
    all_labels = discover_weeks()
    # Hide weeks whose theming pass hasn't run yet — captured-but-not-themed
    # placeholder files exist for the in-progress week as soon as Wispr export
    # runs, but the user only "unlocks" a week by accumulating dictation /
    # meetings AND running theme extraction. The viewer should mirror that.
    labels = [w for w in all_labels if has_themed_content(w)]
    if not labels:
        sys.exit(
            "No themed weeks yet. Run `python3 src/weekly_email.py` to build the "
            "first one, or `./demo.sh` for a synthetic preview."
        )
    skipped = len(all_labels) - len(labels)
    skipped_note = f" (skipped {skipped} not-yet-themed)" if skipped else ""
    print(f"Building viewer for {len(labels)} weeks: {labels[0]} → {labels[-1]}{skipped_note}")

    weeks_data = []
    for label in labels:
        sun, sat = week_range(label)
        if sun.year == sat.year and sun.month == sat.month:
            range_str = f"{sun.strftime('%b')} {sun.day} to {sat.day}, {sat.year}"
        elif sun.year == sat.year:
            range_str = f"{sun.strftime('%b')} {sun.day} to {sat.strftime('%b')} {sat.day}, {sat.year}"
        else:
            range_str = f"{sun.strftime('%b')} {sun.day}, {sun.year} to {sat.strftime('%b')} {sat.day}, {sat.year}"
        search_hints = " ".join([
            label,
            sun.strftime("%B %Y"), sat.strftime("%B %Y"),
            sun.strftime("%b %Y"), sat.strftime("%b %Y"),
            str(sun.year), str(sat.year),
        ])
        weeks_data.append({
            "label": label,
            "range": range_str,
            "search": search_hints,
            "sat": sat.isoformat(),  # Saturday end-date for "completed" check
            "markdown": assemble_week_markdown(label),
        })

    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    out_path = VIEWER_DIR / "index.html"
    html = HTML_TEMPLATE.replace("__WEEKS_JSON__", json.dumps(weeks_data))
    out_path.write_text(html)
    print(f"Wrote {out_path}")
    print(f"Open it: open {out_path}")


if __name__ == "__main__":
    main()
