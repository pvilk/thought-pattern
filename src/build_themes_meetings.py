#!/usr/bin/env python3
"""Apply the weekly-themes pipeline to per-meeting markdown.

Reads `<meetings_dir>/{fathom,granola}/<YYYY-MM-DD>_<slug>_<id>.md`, groups
them by Sunday-Saturday week, and writes themed digests to
`<master_dir>/50_weeks/<YYYY-Www>.md`.

Usage:
    python3 src/build_themes_meetings.py --week 2025-W04
    python3 src/build_themes_meetings.py --list
    python3 src/build_themes_meetings.py --all-missing
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from _config import load as load_config, resolve_path
from _llm import call_anthropic

CFG = load_config()
MEETINGS_DIR = resolve_path(CFG, "meetings_dir")
WEEKS_DIR = resolve_path(CFG, "master_dir") / "50_weeks"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PLACEHOLDER = "_LLM theme pass: populated by build_themes_meetings.py_"


def week_tag(d: date) -> tuple[int, int]:
    days_since_sunday = (d.weekday() + 1) % 7
    sunday = d - timedelta(days=days_since_sunday)
    saturday = sunday + timedelta(days=6)
    week_year = saturday.year
    jan1 = date(week_year, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - timedelta(days=jan1_offset)
    week_num = ((sunday - w01_sunday).days // 7) + 1
    return week_year, week_num


def week_label(d: date) -> str:
    y, w = week_tag(d)
    return f"{y}-W{w:02d}"


def week_range(label: str) -> tuple[date, date]:
    y, w = re.match(r"(\d{4})-W(\d{1,2})", label).groups()
    y, w = int(y), int(w)
    jan1 = date(y, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - timedelta(days=jan1_offset)
    sunday = w01_sunday + timedelta(days=(w - 1) * 7)
    return sunday, sunday + timedelta(days=6)


# Accept both `<date>_<slug>_<id>.md` (export format) and `<date>_<id>.md`
# (per-meeting digest format from older Fathom pipelines).
FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(?:(.+)_)?(\w+)\.md$")


def list_meetings_by_week() -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    if not MEETINGS_DIR.exists():
        return out
    for source_dir in MEETINGS_DIR.iterdir():
        if not source_dir.is_dir():
            continue
        for p in source_dir.glob("*.md"):
            m = FILENAME_RE.search(p.name)
            if not m:
                continue
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            out[week_label(d)].append(p)
    return out


def build_corpus(paths: list[Path]) -> str:
    return "\n\n".join(p.read_text().strip() for p in sorted(paths))


THEMES_RE = re.compile(
    r"##\s*On my mind\s*\n+(.*?)(?=\n##\s*Problems\s*I[\'’]m solving|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)
PROBLEMS_RE = re.compile(
    r"##\s*Problems\s*I[\'’]m solving\s*\n+(.*?)\Z",
    flags=re.DOTALL | re.IGNORECASE,
)


def parse_output(text: str) -> tuple[str, str]:
    om = THEMES_RE.search(text)
    pm = PROBLEMS_RE.search(text)
    if not om or not pm:
        raise ValueError(f"Malformed model output: {text[:600]}")
    return om.group(1).strip(), pm.group(1).strip()


def has_themes(content: str) -> bool:
    m = THEMES_RE.search(content)
    if not m:
        return False
    body = m.group(1).strip()
    return bool(body) and PLACEHOLDER not in body


def assemble_week(label: str, paths: list[Path], themes: str, problems: str) -> str:
    sun, sat = week_range(label)
    lines = [
        f"# {label} ({sun.strftime('%b %d')} to {sat.strftime('%b %d %Y')}) Meetings",
        "",
        "## Stats",
        "",
        f"- Meetings: **{len(paths)}**",
        "",
        "## On my mind", "",
        themes if themes else PLACEHOLDER,
        "",
        "## Problems I'm solving", "",
        problems if problems else PLACEHOLDER,
        "",
        "## Meetings this week",
        "",
    ]
    for p in sorted(paths):
        m = FILENAME_RE.search(p.name)
        if not m:
            continue
        date_str, slug, mid = m.group(1), m.group(2), m.group(3)
        lines.append(f"- {date_str}: [{slug}](../../{p.relative_to(MEETINGS_DIR.parent.parent)}) (`{mid}`)")
    lines.append("")
    return "\n".join(lines)


def build_one(label: str, force: bool, dry_run: bool) -> bool:
    by_week = list_meetings_by_week()
    paths = by_week.get(label, [])
    if not paths:
        sys.exit(f"No meetings for week {label}")

    out_path = WEEKS_DIR / f"{label}.md"
    if out_path.exists() and has_themes(out_path.read_text()) and not force:
        print(f"{label} already themed; pass --force.")
        return True

    print(f"  {label}: {len(paths)} meetings…")
    prompt = (
        (PROMPTS_DIR / "themes.md").read_text()
        + "\n\n## Source\n\n"
        + "This is meeting transcripts (not solo dictation). Each meeting has the user's "
        + "turns with vertex tags like `m<id>.t<NN>` (Fathom) or `g<id>.t<NN>` (Granola). "
        + "Use those as anchors.\n\n"
        + build_corpus(paths)
    )
    response = call_anthropic(prompt)
    try:
        themes, problems = parse_output(response)
    except ValueError as e:
        sys.stderr.write(f"  {label} parse failed: {e}\n")
        return False

    if dry_run:
        print(f"\n## On my mind\n\n{themes}\n\n## Problems I'm solving\n\n{problems}\n")
        return True

    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    content = assemble_week(label, paths, themes, problems)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(out_path)
    print(f"  → {out_path}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all-missing", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.list:
        for label, paths in sorted(list_meetings_by_week().items()):
            sun, sat = week_range(label)
            done = (WEEKS_DIR / f"{label}.md").exists() and has_themes((WEEKS_DIR / f"{label}.md").read_text())
            mark = "[themed]" if done else "[      ]"
            print(f"{mark} {label}  {sun} to {sat}  ({len(paths)} meetings)")
        return

    if args.all_missing:
        for label, _ in sorted(list_meetings_by_week().items()):
            out = WEEKS_DIR / f"{label}.md"
            if not out.exists() or not has_themes(out.read_text()):
                print(label)
        return

    if not args.week:
        sys.exit("Pass --week, --list, or --all-missing.")
    build_one(args.week, args.force, args.dry_run)


if __name__ == "__main__":
    main()
