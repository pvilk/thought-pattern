#!/usr/bin/env python3
"""Extract themes/problems for a single Wispr week into weeks/<W>.md."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _config import load as load_config, resolve_path
from _llm import call_anthropic

CFG = load_config()
ENTRIES_DIR = resolve_path(CFG, "entries_dir")
WEEKS_DIR = resolve_path(CFG, "weeks_dir")
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PLACEHOLDER = "_LLM theme pass: populated by build_themes.py_"


def parse_week_label(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", label)
    if not m:
        raise ValueError(f"Invalid week label: {label!r}")
    return int(m.group(1)), int(m.group(2))


def list_day_files(label: str) -> list[Path]:
    year, _ = parse_week_label(label)
    d = ENTRIES_DIR / str(year) / label
    return sorted(p for p in d.glob("*.md")) if d.is_dir() else []


def strip_details(s: str) -> str:
    return re.sub(r"<details>.*?</details>\s*", "", s, flags=re.DOTALL)


def build_corpus(files: list[Path]) -> str:
    parts = []
    for p in files:
        text = re.sub(r"^#\s.*?\n+", "", p.read_text(), count=1)
        parts.append(f"# {p.stem}\n\n{strip_details(text).strip()}\n")
    return "\n\n".join(parts)


def has_themes(content: str) -> bool:
    m = re.search(r"## On my mind\s*\n+(.*?)(?=\n## )", content, flags=re.DOTALL)
    return bool(m and m.group(1).strip()) and PLACEHOLDER not in (m.group(1) if m else "")


THEMES_RE = re.compile(
    r"##\s*On my mind\s*\n+(.*?)(?=\n##\s*Problems\s*I[\'’]m solving|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)
PROBLEMS_RE = re.compile(
    r"##\s*Problems\s*I[\'’]m solving\s*\n+(.*?)\Z",
    flags=re.DOTALL | re.IGNORECASE,
)


def parse_model_output(text: str) -> tuple[str, str]:
    om = THEMES_RE.search(text)
    pm = PROBLEMS_RE.search(text)
    if not om or not pm:
        raise ValueError(f"Malformed model output: {text[:600]}")
    return om.group(1).strip(), pm.group(1).strip()


def replace_sections(content: str, themes: str, problems: str) -> str:
    new = re.sub(
        r"(## On my mind\s*\n+).*?(?=\n## Problems I[\'’]m solving)",
        f"## On my mind\n\n{themes}\n",
        content, count=1, flags=re.DOTALL,
    )
    new = re.sub(
        r"(## Problems I[\'’]m solving\s*\n+).*?(?=\n## Daily files)",
        f"## Problems I'm solving\n\n{problems}\n",
        new, count=1, flags=re.DOTALL,
    )
    return new


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    parse_week_label(args.week)
    week_path = WEEKS_DIR / f"{args.week}.md"
    if not week_path.exists():
        sys.exit(f"Week file not found: {week_path}")
    files = list_day_files(args.week)
    if not files:
        sys.exit(f"No entries for week {args.week}")

    content = week_path.read_text()
    if has_themes(content) and not args.force:
        print(f"{args.week} already themed; pass --force to regenerate.")
        return

    print(f"Building themes for {args.week} from {len(files)} day file(s)…")
    prompt = (PROMPTS_DIR / "themes.md").read_text() + "\n\n## Source\n\n" + build_corpus(files)
    response = call_anthropic(prompt)

    try:
        themes, problems = parse_model_output(response)
    except ValueError as e:
        sys.stderr.write(f"\n{e}\nLeaving week file unchanged.\n")
        sys.exit(2)

    if args.dry_run:
        print(f"\n## On my mind\n\n{themes}\n\n## Problems I'm solving\n\n{problems}\n")
        return

    new_content = replace_sections(content, themes, problems)
    if new_content == content:
        sys.exit("Section replacement made no change.")
    tmp = week_path.with_suffix(week_path.suffix + ".tmp")
    tmp.write_text(new_content)
    tmp.replace(week_path)
    print(f"Updated {week_path}")


if __name__ == "__main__":
    main()
