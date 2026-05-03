#!/usr/bin/env python3
"""Wispr auditor: generate '## What I noticed' for one week."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _config import load as load_config, resolve_path
from _llm import call_anthropic
from _parsers import WeekRecord, load_all_weeks

CFG = load_config()
WEEKS_DIR = resolve_path(CFG, "weeks_dir")
MASTER_DIR = resolve_path(CFG, "master_dir")
TRENDS_DIR = MASTER_DIR / "20_trends"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

CONTEXT_LOOKBACK_WEEKS = 4
METRICS_LOOKBACK_WEEKS = 8


def find_week(weeks: list[WeekRecord], label: str) -> tuple[int, WeekRecord]:
    for i, w in enumerate(weeks):
        if w.label == label:
            return i, w
    raise SystemExit(f"Week {label} not found in {WEEKS_DIR}")


def fmt_week(w: WeekRecord) -> str:
    out = [f"### {w.label}", "", "**On my mind:**"]
    out.extend([f"- **{t.name}**: {t.summary}" for t in w.themes] or ["- _no themes_"])
    out += ["", "**Problems I'm solving:**"]
    out.extend([f"- **{p.name}**: {p.summary}" for p in w.problems] or ["- _no problems_"])
    out.append("")
    return "\n".join(out)


def fmt_metrics(weeks: list[WeekRecord]) -> str:
    out = ["**Per-week stats (last 8 weeks):**", "",
           "| Week | Entries | Words | Days | Themes | Problems |",
           "|---|---:|---:|---:|---:|---:|"]
    for w in weeks:
        active = f"{w.stats.days_active}/7" if w.stats.days_active is not None else "?"
        out.append(f"| {w.label} | {w.stats.entries} | {w.stats.words:,} | {active} | {len(w.themes)} | {len(w.problems)} |")
    return "\n".join(out)


def read_optional(path: Path) -> str:
    return path.read_text() if path.exists() else "_(not yet generated)_"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    weeks = load_all_weeks(WEEKS_DIR)
    if not weeks:
        sys.exit("No week files parsed.")

    idx, target = find_week(weeks, args.week)
    context = weeks[max(0, idx - CONTEXT_LOOKBACK_WEEKS):idx]
    metrics = weeks[max(0, idx - METRICS_LOOKBACK_WEEKS + 1):idx + 1]

    parts = ["## This week (target)", "", fmt_week(target),
             "## Prior weeks (context, oldest to newest)", ""]
    if context:
        parts.extend(fmt_week(w) for w in context)
    else:
        parts.append("_first week of corpus, no prior context._\n")
    parts += ["## Unresolved threads (current)", "",
              read_optional(TRENDS_DIR / "unresolved_threads.md"), "",
              "## Consistency metrics (last 8 weeks)", "",
              fmt_metrics(metrics), ""]

    prompt = (PROMPTS_DIR / "auditor.md").read_text() + "\n\n## Inputs\n\n" + "\n".join(parts)
    response = call_anthropic(prompt).strip()

    if args.out:
        args.out.write_text(response + "\n")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(response + "\n")


if __name__ == "__main__":
    main()
