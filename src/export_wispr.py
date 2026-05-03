#!/usr/bin/env python3
"""Export Wispr Flow History to per-day markdown files, week-tagged.

Reads a snapshot of the Wispr Flow SQLite (path from config.sources.wispr.db_path)
and writes one markdown file per day under entries/<YYYY>/<YYYY-Www>/<YYYY-MM-DD>.md.

Preserves any existing themed week files at weeks/<YYYY-Www>.md so re-running
this script does not wipe out content from build_themes.py.

Week labeling: Sunday-Saturday weeks. W01 = the week containing Jan 1 of that
year. The week's "year" is the year of its Saturday, so the week containing
Jan 1 is always W01 of that year, even if its Sunday falls in late December.

Usage:
    python3 src/export_wispr.py
    python3 src/export_wispr.py --refresh-snapshot     # re-pull live DB first
    python3 src/export_wispr.py --since 2026-01-01     # only entries after a date
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from _config import load as load_config, resolve_path

CFG = load_config()
WISPR_CFG = CFG.get("sources", {}).get("wispr", {})
LIVE_DB = Path(WISPR_CFG.get("db_path", "")).expanduser()
ENTRIES_DIR = resolve_path(CFG, "entries_dir")
WEEKS_DIR = resolve_path(CFG, "weeks_dir")
SNAPSHOT = ENTRIES_DIR.parent / "flow.sqlite.snapshot"
INDEX_PATH = ENTRIES_DIR.parent / "00_index.md"

PLACEHOLDER = "_LLM theme pass: populated by build_themes.py_"


def week_tag(d: date) -> tuple[int, int]:
    days_since_sunday = (d.weekday() + 1) % 7
    sunday = d - timedelta(days=days_since_sunday)
    saturday = sunday + timedelta(days=6)
    week_year = saturday.year
    jan1 = date(week_year, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - timedelta(days=jan1_offset)
    week_num = ((sunday - w01_sunday).days // 7) + 1
    return (week_year, week_num)


def week_label(d: date) -> str:
    y, w = week_tag(d)
    return f"{y}-W{w:02d}"


def week_range(week_year: int, week_num: int) -> tuple[date, date]:
    jan1 = date(week_year, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - timedelta(days=jan1_offset)
    sunday = w01_sunday + timedelta(days=(week_num - 1) * 7)
    return sunday, sunday + timedelta(days=6)


APP_DISPLAY = {
    "com.google.Chrome": "Chrome",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "com.anthropic.claudefordesktop": "Claude",
    "dev.warp.Warp-Stable": "Warp",
    "com.apple.MobileSMS": "Messages",
    "com.apple.Terminal": "Terminal",
    "com.apple.Notes": "Notes",
    "com.apple.finder": "Finder",
    "com.apple.Safari": "Safari",
    "com.electron.wispr-flow": "Wispr",
    "com.figma.Desktop": "Figma",
}


def app_short(app: str | None) -> str:
    if not app:
        return "?"
    return APP_DISPLAY.get(app, app.split(".")[-1] if "." in app else app)


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def parse_ts(ts: str) -> datetime:
    s = ts.strip().replace(" +", "+").replace(" -", "-")
    if " " in s and "T" not in s:
        date_part, _, rest = s.partition(" ")
        s = f"{date_part}T{rest}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(re.sub(r"\.\d+", "", s))


def canonical_text(formatted, edited, asr) -> str:
    for t in (edited, formatted, asr):
        if t and t.strip():
            return t.strip()
    return ""


def slug_id(ts: datetime, idx: int) -> str:
    return f"w{ts.strftime('%Y%m%d')}.{idx:03d}"


def parse_ax_context(raw):
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    ax = obj.get("ax_context") if isinstance(obj, dict) else None
    return [str(x) for x in ax if x] if isinstance(ax, list) else []


def snapshot_live(force: bool = False) -> None:
    if SNAPSHOT.exists() and not force:
        return
    if not LIVE_DB.exists():
        sys.exit(
            f"Wispr Flow DB not found at {LIVE_DB}\n"
            f"Edit sources.wispr.db_path in config.local.toml, or set sources.wispr.enabled = false."
        )
    print(f"Snapshotting live DB → {SNAPSHOT}")
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["sqlite3", str(LIVE_DB), f".backup '{SNAPSHOT}'"])


def fetch_rows(since: date | None) -> list[dict]:
    conn = sqlite3.connect(SNAPSHOT)
    conn.row_factory = sqlite3.Row
    q = """
        SELECT transcriptEntityId AS id, asrText, formattedText, editedText,
               timestamp, app, url, additionalContext, duration, numWords, language
        FROM History
        WHERE status = 'fulfilled' OR status IS NULL
              OR formattedText IS NOT NULL OR editedText IS NOT NULL
    """
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    parsed = []
    for r in rows:
        if not r.get("timestamp"):
            continue
        ts = parse_ts(r["timestamp"]).astimezone()
        if since and ts.date() < since:
            continue
        text = canonical_text(r.get("formattedText"), r.get("editedText"), r.get("asrText"))
        if not text:
            continue
        r["ts_local"] = ts
        r["canonical_text"] = text
        r["ax_context"] = parse_ax_context(r.get("additionalContext"))
        parsed.append(r)
    parsed.sort(key=lambda r: r["ts_local"])
    return parsed


def write_day_file(d: date, entries: list[dict]) -> Path:
    y, w = week_tag(d)
    wlabel = f"{y}-W{w:02d}"
    out_dir = ENTRIES_DIR / str(y) / wlabel
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{d.isoformat()}.md"

    apps = Counter(app_short(e.get("app")) for e in entries)
    domains = Counter(domain_of(e.get("url")) for e in entries if domain_of(e.get("url")))
    total_words = sum((e.get("numWords") or 0) for e in entries)
    span_start = entries[0]["ts_local"].strftime("%H:%M")
    span_end = entries[-1]["ts_local"].strftime("%H:%M")

    lines: list[str] = []
    lines.append(f"# {d.isoformat()} ({d.strftime('%A')}) · {wlabel}")
    lines.append("")
    lines.append(f"- Entries: **{len(entries)}** · Words: **{total_words:,}** · Span: {span_start} → {span_end}")
    if apps:
        lines.append(f"- Apps: " + ", ".join(f"{a} ({c})" for a, c in apps.most_common(5)))
    if domains:
        lines.append(f"- Domains: " + ", ".join(f"{a} ({c})" for a, c in domains.most_common(5)))
    lines.append("")
    lines.append("---")
    lines.append("")

    by_index: dict[str, int] = defaultdict(int)
    for e in entries:
        ts = e["ts_local"]
        date_key = ts.strftime("%Y%m%d")
        by_index[date_key] += 1
        idx = by_index[date_key]
        vid = slug_id(ts, idx)
        ctx = " · ".join(filter(None, [
            ts.strftime("%H:%M:%S"),
            app_short(e.get("app")),
            domain_of(e.get("url")),
        ]))
        ax = e.get("ax_context") or []
        ax_str = (" · ax: " + ", ".join(ax[:8])) if ax else ""
        lines.append(f"## `{vid}` · {ctx}{ax_str}")
        lines.append("")
        lines.append(e["canonical_text"])
        lines.append("")
        asr = (e.get("asrText") or "").strip()
        if asr and asr != e["canonical_text"]:
            lines.append("<details><summary>raw asr</summary>")
            lines.append("")
            lines.append(asr)
            lines.append("")
            lines.append("</details>")
            lines.append("")
        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def _read_existing_theme_sections(week_path: Path) -> tuple[str, str]:
    """Preserve themes/problems content from a prior build_themes run."""
    if not week_path.exists():
        return "", ""
    try:
        content = week_path.read_text()
    except OSError:
        return "", ""
    on_mind = re.search(
        r"## On my mind\s*\n+(.*?)(?=\n## Problems I[\'’]m solving|\Z)",
        content, flags=re.DOTALL,
    )
    problems = re.search(
        r"## Problems I[\'’]m solving\s*\n+(.*?)(?=\n## Daily files|\Z)",
        content, flags=re.DOTALL,
    )
    on_body = on_mind.group(1).strip() if on_mind else ""
    pr_body = problems.group(1).strip() if problems else ""
    if PLACEHOLDER in on_body:
        on_body = ""
    if PLACEHOLDER in pr_body:
        pr_body = ""
    return on_body, pr_body


def write_week_file(wlabel: str, entries: list[dict]) -> Path:
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = WEEKS_DIR / f"{wlabel}.md"
    y_str, w_str = wlabel.split("-W")
    y, w = int(y_str), int(w_str)
    sun, sat = week_range(y, w)

    apps = Counter(app_short(e.get("app")) for e in entries)
    domains = Counter(domain_of(e.get("url")) for e in entries if domain_of(e.get("url")))
    ax_terms = Counter()
    for e in entries:
        for t in (e.get("ax_context") or []):
            ax_terms[t] += 1
    total_words = sum((e.get("numWords") or 0) for e in entries)
    days_active = len({e["ts_local"].date() for e in entries})

    lines: list[str] = []
    lines.append(f"# {wlabel} ({sun.strftime('%b %d')} to {sat.strftime('%b %d %Y')})")
    lines.append("")
    lines.append("## Stats")
    lines.append("")
    lines.append(f"- Entries: **{len(entries):,}** · Words: **{total_words:,}** · Active days: **{days_active}/7**")
    if apps:
        lines.append("- Top apps: " + ", ".join(f"{a} ({c})" for a, c in apps.most_common(8)))
    if domains:
        lines.append("- Top domains: " + ", ".join(f"{a} ({c})" for a, c in domains.most_common(8)))
    if ax_terms:
        lines.append("- Top on-screen terms (ax_context): " +
                     ", ".join(f"{t} ({c})" for t, c in ax_terms.most_common(20)))
    lines.append("")

    on_body, pr_body = _read_existing_theme_sections(out_path)
    lines.append("## On my mind")
    lines.append("")
    lines.append(on_body if on_body else PLACEHOLDER)
    lines.append("")
    lines.append("## Problems I'm solving")
    lines.append("")
    lines.append(pr_body if pr_body else PLACEHOLDER)
    lines.append("")

    lines.append("## Daily files")
    lines.append("")
    by_day: dict[date, list[dict]] = defaultdict(list)
    for e in entries:
        by_day[e["ts_local"].date()].append(e)
    for d in sorted(by_day.keys()):
        rel = f"../entries/{d.year}/{wlabel}/{d.isoformat()}.md"
        lines.append(f"- [{d.isoformat()} ({d.strftime('%a')})]({rel}): {len(by_day[d])} entries")
    lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def write_index(weeks: dict[str, list[dict]]) -> None:
    lines: list[str] = []
    lines.append("# Wispr Thoughts: Wispr corpus index")
    lines.append("")
    total_entries = sum(len(v) for v in weeks.values())
    total_words = sum((e.get("numWords") or 0) for v in weeks.values() for e in v)
    sorted_weeks = sorted(weeks.keys())
    lines.append(f"- Weeks: **{len(weeks)}** ({sorted_weeks[0]} to {sorted_weeks[-1]})")
    lines.append(f"- Entries: **{total_entries:,}** · Words: **{total_words:,}**")
    lines.append(f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    INDEX_PATH.write_text("\n".join(lines))


def main() -> None:
    if not WISPR_CFG.get("enabled", True):
        print("Wispr source disabled in config.local.toml; skipping.")
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-snapshot", action="store_true")
    ap.add_argument("--since", type=str, help="YYYY-MM-DD")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    if args.refresh_snapshot or not SNAPSHOT.exists():
        snapshot_live(force=args.refresh_snapshot)

    since = date.fromisoformat(args.since) if args.since else None

    if args.clean:
        for d in (ENTRIES_DIR, WEEKS_DIR):
            if d.exists():
                shutil.rmtree(d)

    rows = fetch_rows(since)
    print(f"Loaded {len(rows):,} entries")

    by_day: dict[date, list[dict]] = defaultdict(list)
    by_week: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = r["ts_local"].date()
        by_day[d].append(r)
        by_week[week_label(d)].append(r)

    print(f"Writing {len(by_day)} day files…")
    for d, ents in sorted(by_day.items()):
        write_day_file(d, ents)

    print(f"Writing {len(by_week)} week files…")
    for wlabel, ents in sorted(by_week.items()):
        write_week_file(wlabel, ents)

    write_index(by_week)
    print(f"Index → {INDEX_PATH}")
    print("Done.")


if __name__ == "__main__":
    main()
