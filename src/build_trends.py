#!/usr/bin/env python3
"""Cross-week trends: themes_over_time, consistency_metrics, unresolved_threads."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from _config import load as load_config, resolve_path
from _llm import call_anthropic
from _parsers import WeekRecord, load_all_weeks

CFG = load_config()
WEEKS_DIR = resolve_path(CFG, "weeks_dir")
MASTER_DIR = resolve_path(CFG, "master_dir")
TRENDS_DIR = MASTER_DIR / "20_trends"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CARRYOVER_LOOKBACK_WEEKS = 8


def normalize_name(name: str) -> str:
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def week_sort_key(label: str) -> tuple[int, int]:
    m = re.match(r"(\d{4})-W(\d{1,2})", label)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def build_themes_over_time(weeks: list[WeekRecord]) -> str:
    lines = ["# Themes over time", "", f"_Generated {now_stamp()} from {len(weeks)} weeks._", ""]
    theme_weeks: dict[str, list[str]] = {}
    theme_display: dict[str, str] = {}
    for w in weeks:
        seen = set()
        for t in w.themes:
            norm = normalize_name(t.name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            theme_weeks.setdefault(norm, []).append(w.label)
            theme_display.setdefault(norm, t.name)
    recurring = sorted(
        ((n, ws) for n, ws in theme_weeks.items() if len(ws) >= 3),
        key=lambda t: (-len(t[1]), t[0]),
    )
    lines.append("## Recurring themes (3+ weeks)")
    lines.append("")
    if not recurring:
        lines.append("_None yet; corpus may be too young or themes name-drift._\n")
    else:
        lines.append("| Theme | Weeks | First seen | Last seen |")
        lines.append("|---|---:|---|---|")
        for norm, ws in recurring:
            ws_sorted = sorted(ws, key=week_sort_key)
            lines.append(f"| {theme_display[norm]} | {len(ws)} | {ws_sorted[0]} | {ws_sorted[-1]} |")
        lines.append("")
    lines.append("## Per-week themes")
    lines.append("")
    for w in weeks:
        if not w.themes:
            continue
        lines.append(f"### {w.label}\n")
        for t in w.themes:
            anchors = f" ({', '.join(t.anchors[:3])})" if t.anchors else ""
            lines.append(f"- **{t.name}**{anchors}")
        lines.append("")
    return "\n".join(lines)


def build_consistency_metrics(weeks: list[WeekRecord]) -> str:
    lines = ["# Consistency metrics", "", f"_Generated {now_stamp()} from {len(weeks)} weeks._", ""]
    lines.append("## Per-week table\n")
    lines.append("| Week | Entries | Words | Active days | Themes | Problems | Top ax_context |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for w in weeks:
        active = f"{w.stats.days_active}/7" if w.stats.days_active is not None else "?"
        top_ax = ", ".join(name for name, _ in w.stats.top_ax_context[:5]) or "?"
        lines.append(
            f"| {w.label} | {w.stats.entries:,} | {w.stats.words:,} | {active} | "
            f"{len(w.themes)} | {len(w.problems)} | {top_ax} |"
        )
    lines.append("")
    if len(weeks) >= 5:
        recent = weeks[-1]
        trailing = weeks[-5:-1]
        avg_e = sum(w.stats.entries for w in trailing) / len(trailing)
        avg_w = sum(w.stats.words for w in trailing) / len(trailing)
        lines.append("## Most-recent week vs 4-week trailing average\n")
        lines.append(f"- **Latest:** {recent.label}")
        lines.append(f"- Entries: {recent.stats.entries} (avg {avg_e:.0f}, delta {recent.stats.entries - avg_e:+.0f})")
        lines.append(f"- Words: {recent.stats.words:,} (avg {avg_w:,.0f}, delta {recent.stats.words - avg_w:+,.0f})")
        lines.append("")
    return "\n".join(lines)


def build_unresolved_threads(weeks: list[WeekRecord]) -> str:
    if len(weeks) < 2:
        return f"# Unresolved threads\n\n_Generated {now_stamp()}._\n\n_Need 2+ weeks for carryover._\n"
    sections = []
    for w in weeks[-CARRYOVER_LOOKBACK_WEEKS:]:
        if not w.problems:
            continue
        sections.append(f"### {w.label}\n")
        for p in w.problems:
            sections.append(f"- **{p.name}**: {p.summary}")
        sections.append("")
    prompt = (PROMPTS_DIR / "carryover.md").read_text() + "\n\n## Source\n\n" + "\n".join(sections)
    response = call_anthropic(prompt)
    return (
        f"# Unresolved threads\n\n"
        f"_Generated {now_stamp()} from the last {min(CARRYOVER_LOOKBACK_WEEKS, len(weeks))} weeks._\n\n"
        f"{response.strip()}\n"
    )


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-llm", action="store_true")
    args = ap.parse_args()

    if not WEEKS_DIR.exists():
        sys.exit(f"Weeks directory not found: {WEEKS_DIR}")
    weeks = load_all_weeks(WEEKS_DIR)
    if not weeks:
        sys.exit("No week files parsed.")

    print(f"Parsed {len(weeks)} weeks ({weeks[0].label} → {weeks[-1].label})")
    write_atomic(TRENDS_DIR / "themes_over_time.md", build_themes_over_time(weeks))
    print(f"  wrote {TRENDS_DIR / 'themes_over_time.md'}")
    write_atomic(TRENDS_DIR / "consistency_metrics.md", build_consistency_metrics(weeks))
    print(f"  wrote {TRENDS_DIR / 'consistency_metrics.md'}")

    if args.skip_llm:
        print("  --skip-llm")
        return
    print("  building unresolved_threads.md (LLM)…")
    write_atomic(TRENDS_DIR / "unresolved_threads.md", build_unresolved_threads(weeks))
    print(f"  wrote {TRENDS_DIR / 'unresolved_threads.md'}")


if __name__ == "__main__":
    main()
