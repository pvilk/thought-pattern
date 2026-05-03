#!/usr/bin/env python3
"""Meetings auditor: '## What I noticed (in conversation)' for one week."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from _config import load as load_config, resolve_path
from _llm import call_anthropic

CFG = load_config()
WEEKS_DIR = resolve_path(CFG, "master_dir") / "50_weeks"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CONTEXT_LOOKBACK_WEEKS = 4

ITEM_RE = re.compile(r"^\s*-\s*\*\*([^*]+?)\*\*\s*[—\-:]\s*(.+)$", flags=re.MULTILINE)


@dataclass
class Item:
    name: str
    summary: str


@dataclass
class Week:
    label: str
    meetings: int = 0
    themes: list[Item] = field(default_factory=list)
    problems: list[Item] = field(default_factory=list)


def section_body(content: str, header: str, end: str) -> str:
    pat = re.compile(rf"##\s*{header}\s*\n+(.*?)(?=\n##\s*{end}|\Z)", flags=re.DOTALL)
    m = pat.search(content)
    return m.group(1).strip() if m else ""


def parse_items(body: str) -> list[Item]:
    return [Item(m.group(1).strip(), m.group(2).strip()) for m in ITEM_RE.finditer(body)]


def parse_week_file(path: Path) -> Week | None:
    if not path.exists():
        return None
    content = path.read_text()
    w = Week(label=path.stem)
    if m := re.search(r"Meetings:\s*\*\*(\d+)\*\*", content):
        w.meetings = int(m.group(1))
    w.themes = parse_items(section_body(content, re.escape("On my mind"), r"Problems\s*I[\'’]m solving|Meetings this week|$"))
    w.problems = parse_items(section_body(content, r"Problems\s*I[\'’]m solving", r"Meetings this week|$"))
    return w


def fmt(w: Week) -> str:
    out = [f"### {w.label}", "", f"_{w.meetings} meetings_", "", "**On my mind:**"]
    out.extend([f"- **{t.name}**: {t.summary}" for t in w.themes] or ["- _no themes_"])
    out += ["", "**Problems I'm solving:**"]
    out.extend([f"- **{p.name}**: {p.summary}" for p in w.problems] or ["- _no problems_"])
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    week_files = sorted(WEEKS_DIR.glob("*.md"))
    if not week_files:
        sys.exit(f"No themed meeting weeks at {WEEKS_DIR}")

    target_path = WEEKS_DIR / f"{args.week}.md"
    if not target_path.exists():
        sys.exit(f"Week {args.week} not themed yet; run build_themes_meetings.py first.")
    target = parse_week_file(target_path)

    idx = next((i for i, p in enumerate(week_files) if p.name == target_path.name), -1)
    context = [parse_week_file(p) for p in week_files[max(0, idx - CONTEXT_LOOKBACK_WEEKS):idx]]
    context = [c for c in context if c]

    if not target.themes and not target.problems:
        out = (
            "## What I noticed (in conversation)\n\n"
            f"- **Sparse week:** {target.meetings} meetings. No themed conversations to mirror.\n"
        )
        if args.out:
            args.out.write_text(out)
        else:
            sys.stdout.write(out)
        return

    parts = ["## This week (target)", "", fmt(target),
             "## Prior weeks (context, oldest to newest)", ""]
    if context:
        parts.extend(fmt(w) for w in context)
    else:
        parts.append("_first week of corpus._\n")

    prompt = (PROMPTS_DIR / "auditor_meetings.md").read_text() + "\n\n## Inputs\n\n" + "\n".join(parts)
    response = call_anthropic(prompt).strip()
    if args.out:
        args.out.write_text(response + "\n")
    else:
        sys.stdout.write(response + "\n")


if __name__ == "__main__":
    main()
