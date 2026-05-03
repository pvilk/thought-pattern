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


def extract_section(text: str, header_pattern: str) -> tuple[str, str] | None:
    """Pull (heading, body) for a section. Returns None if not present."""
    pat = re.compile(
        rf"##\s*({header_pattern})\s*\n+(.*?)(?=\n##|\Z)",
        flags=re.DOTALL,
    )
    m = pat.search(text)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def assemble_week_markdown(label: str) -> str:
    """Build a clean unified weekly view.

    Drops Stats / Voice-wrapper / Meetings-this-week sections.
    Consolidates voice + meeting themes/problems into ONE list each, with
    source pills marking which corpus surfaced each item.
    Keeps the auditor sections (What I noticed) when a digest exists.
    """
    voice_path = WEEKS_DIR / f"{label}.md"
    meeting_path = MEETING_WEEKS_DIR / f"{label}.md"
    digest_path = DIGESTS_DIR / f"{label}.md"

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

    # Auditor sections (only present when a digest was generated for this week)
    auditor_sections: list[tuple[str, str]] = []
    if digest_path.exists():
        digest_text = digest_path.read_text()
        for pattern in [r"What I noticed \(in conversation\)", r"What I noticed[^—\n(]*"]:
            sec = extract_section(digest_text, pattern)
            if sec and sec[0] not in {h for h, _ in auditor_sections}:
                auditor_sections.append(sec)

    parts: list[str] = []

    if themes:
        parts += ["## On my mind", "", render_items(themes), ""]
    if problems:
        parts += ["## Problems I'm solving", "", render_items(problems), ""]
    for heading, body in auditor_sections:
        parts += [f"## {heading}", "", body, ""]
    if not themes and not problems and not auditor_sections:
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
<title>Wispr Thoughts</title>
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<script src="https://cdn.jsdelivr.net/npm/marked@13/marked.min.js"></script>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b; --accent: #4a6cf7;
    --border: #e5e5e7; --code-bg: #f5f5f7; --hover: #f0f0f0;
    --shadow: 0 12px 40px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f0f10; --fg: #e8e8ea; --muted: #8a8a8e; --accent: #7c8fff;
            --border: #2a2a2d; --code-bg: #1a1a1d; --hover: #1f1f22;
            --shadow: 0 12px 40px rgba(0,0,0,0.55), 0 2px 6px rgba(0,0,0,0.4); }
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

  /* Sync pill, top-right of hero */
  .sync-pill {
    position: absolute; top: 24px; right: 24px;
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
    .sync-pill { position: static; margin: 0 auto 16px; }
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

  /* Command palette */
  body.palette-open { overflow: hidden; }
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
  @media (prefers-color-scheme: dark) {
    .src-tag.voice { color: var(--accent); border-color: rgba(124,143,255,0.4); }
    .src-tag.meeting { color: rgb(74,222,128); border-color: rgba(74,222,128,0.4); }
    .src-tag.both { color: rgb(251,191,36); border-color: rgba(251,191,36,0.5); }
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
  /* Sync modal (sync pill click) */
  .sync-overlay {
    position: fixed; inset: 0; z-index: 110; display: none;
    background: rgba(0,0,0,0.45); backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }
  .sync-overlay.open {
    display: flex; align-items: flex-start; justify-content: center;
    padding: 12vh 16px; overflow-y: auto;
  }
  .sync-modal {
    width: 100%; max-width: 560px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 14px;
    box-shadow: var(--shadow); padding: 22px 24px 18px;
  }
  .sync-header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin-bottom: 12px;
  }
  .sync-header h2 {
    font-family: ui-serif, "New York", Charter, Cambria, Georgia, serif;
    font-weight: 500; font-size: 24px; letter-spacing: -0.01em;
    margin: 0; line-height: 1.15;
  }
  .sync-intro {
    color: var(--muted); margin: 0 0 16px; font-size: 14px; line-height: 1.55;
  }
  .sync-log {
    background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin: 0 0 16px;
    font-family: ui-monospace, "SF Mono", monospace; font-size: 12px;
    line-height: 1.55; color: var(--fg);
    max-height: 50vh; overflow-y: auto;
    white-space: pre-wrap; word-wrap: break-word;
  }
  .sync-actions {
    display: flex; justify-content: flex-end; gap: 10px;
  }
  .sync-btn {
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 16px; font-size: 14px; cursor: pointer;
    font-family: inherit; transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .sync-btn:disabled { cursor: not-allowed; opacity: 0.5; }
  .sync-btn-secondary { background: transparent; color: var(--muted); }
  .sync-btn-secondary:hover:not(:disabled) { background: var(--hover); color: var(--fg); }
  .sync-btn-primary {
    background: var(--accent); border-color: var(--accent); color: white;
  }
  .sync-btn-primary:hover:not(:disabled) { filter: brightness(1.05); }

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
  @media (prefers-color-scheme: dark) {
    .copy-btn.copied { color: rgb(74,222,128); }
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
  <button class="sync-pill" id="syncPill" title="Click to sync your data">
    <span class="sync-dot" id="syncDot"></span>
    <span class="sync-label" id="syncLabel">Checking…</span>
  </button>
  <h1>Weekly Digest</h1>
  <div class="nav-row">
    <button class="nav-btn" id="prev" title="Previous week (←)">‹</button>
    <span class="date-range" id="date-range"></span>
    <button class="nav-btn" id="next" title="Next week (→)">›</button>
  </div>
</div>

<main id="content"></main>

<div class="sync-overlay" id="syncOverlay">
  <div class="sync-modal" role="dialog" aria-label="Sync your data">
    <div class="sync-header">
      <h2 id="syncModalTitle">Sync now</h2>
      <button class="prompts-close" id="syncClose" aria-label="Close">×</button>
    </div>
    <div class="sync-body" id="syncBody">
      <p class="sync-intro" id="syncIntro">
        Re-pulls your enabled sources (Wispr Flow, Fathom, Granola), re-themes
        the most recent completed week, and rebuilds the viewer. Roughly two
        minutes. The viewer will reload when it finishes.
      </p>
      <pre class="sync-log" id="syncLog" hidden></pre>
    </div>
    <div class="sync-actions" id="syncActions">
      <button class="sync-btn sync-btn-secondary" id="syncCancel">Cancel</button>
      <button class="sync-btn sync-btn-primary" id="syncStart">Sync now</button>
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
  const WEEKS = __WEEKS_JSON__;
  let idx = WEEKS.length - 1;

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

  // ---------- Sync pill (polls /api/status, click → sync modal) ----------
  const syncPillEl  = document.getElementById('syncPill');
  const syncDotEl   = document.getElementById('syncDot');
  const syncLabelEl = document.getElementById('syncLabel');

  // file:// origins can't talk to localhost API; the pill shows "Server offline"
  const HAS_API = window.location.protocol === 'http:' || window.location.protocol === 'https:';

  function setSyncPill(state, label) {
    syncPillEl.classList.remove('fresh', 'stale', 'very-stale', 'offline', 'syncing');
    if (state) syncPillEl.classList.add(state);
    syncLabelEl.textContent = label;
  }

  function relativeTime(iso) {
    if (!iso) return 'never synced';
    const then = new Date(iso).getTime();
    const ageMin = Math.floor((Date.now() - then) / 60000);
    if (ageMin < 1)   return 'just synced';
    if (ageMin < 60)  return `synced ${ageMin}m ago`;
    const ageHr = Math.floor(ageMin / 60);
    if (ageHr < 24)   return `synced ${ageHr}h ago`;
    const ageDay = Math.floor(ageHr / 24);
    return `synced ${ageDay}d ago`;
  }

  async function refreshStatus() {
    if (!HAS_API) {
      setSyncPill('offline', 'Server offline');
      syncPillEl.disabled = true;
      return;
    }
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('status ' + r.status);
      const s = await r.json();
      if (s.syncing) {
        setSyncPill('syncing', 'Syncing…');
        syncPillEl.disabled = true;
        return;
      }
      syncPillEl.disabled = false;
      const labels = {
        fresh:        relativeTime(s.last_sync),
        stale:        'Ready for update',
        'very-stale': 'Stale, sync now',
        never:        'Never synced',
      };
      setSyncPill(s.freshness, labels[s.freshness] || relativeTime(s.last_sync));
    } catch (e) {
      setSyncPill('offline', 'Server offline');
      syncPillEl.disabled = true;
    }
  }
  refreshStatus();
  setInterval(refreshStatus, 30000);

  // ---------- Sync modal ----------
  const syncOverlayEl = document.getElementById('syncOverlay');
  const syncCloseEl   = document.getElementById('syncClose');
  const syncCancelEl  = document.getElementById('syncCancel');
  const syncStartEl   = document.getElementById('syncStart');
  const syncIntroEl   = document.getElementById('syncIntro');
  const syncLogEl     = document.getElementById('syncLog');
  const syncTitleEl   = document.getElementById('syncModalTitle');

  let syncOpen = false;
  let syncPollHandle = null;

  function openSyncModal() {
    syncOpen = true;
    // Reset to the confirm view
    syncTitleEl.textContent = 'Sync now';
    syncIntroEl.hidden = false;
    syncLogEl.hidden = true;
    syncLogEl.textContent = '';
    syncStartEl.disabled = false;
    syncStartEl.textContent = 'Sync now';
    syncCancelEl.textContent = 'Cancel';
    syncOverlayEl.classList.add('open');
  }
  function closeSyncModal() {
    syncOpen = false;
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
    syncIntroEl.hidden = true;
    syncLogEl.hidden = false;
    syncLogEl.textContent = 'Starting sync…\n';
    syncStartEl.disabled = true;
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
        const ok = data.status === 'completed';
        syncTitleEl.textContent = ok ? 'Sync complete' : 'Sync failed';
        syncStartEl.disabled = false;
        syncStartEl.textContent = 'Reload viewer';
        syncCancelEl.textContent = 'Close';
        if (ok) {
          // Reload after a short pause so the user sees the success state
          setTimeout(() => { window.location.reload(); }, 1200);
        }
        refreshStatus();
      }
    } catch (e) {
      // Server might have restarted mid-sync; surface but keep polling
      syncLogEl.textContent += `\nPoll error: ${e}\n`;
    }
  }

  syncStartEl.addEventListener('click', () => {
    if (syncStartEl.textContent === 'Reload viewer') {
      window.location.reload();
      return;
    }
    startSync();
  });

  // Pill click opens the modal
  syncPillEl.addEventListener('click', () => {
    if (!HAS_API || syncPillEl.disabled) return;
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
    if (e.key === 't' || e.key === 'T')                     { idx = WEEKS.length - 1; render(); }
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
    labels = discover_weeks()
    if not labels:
        sys.exit("No weeks found in data/weeks, data/master/50_weeks, or data/digests.")

    print(f"Building viewer for {len(labels)} weeks: {labels[0]} → {labels[-1]}")

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
