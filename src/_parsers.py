"""Shared parsers for week files (weeks/<YYYY-Www>.md).

Used by build_trends.py and build_auditor.py to extract themes, problems,
and per-week stats without duplicating regex logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VERTEX_RE = re.compile(r"w\d{8}\.\d{1,4}")
ITEM_RE = re.compile(r"^\s*-\s*\*\*([^*]+?)\*\*\s*[—\-:]\s*(.+)$", flags=re.MULTILINE)


@dataclass
class WeekItem:
    name: str
    summary: str
    anchors: list[str] = field(default_factory=list)


@dataclass
class WeekStats:
    entries: int = 0
    words: int = 0
    days_active: int | None = None
    top_apps: list[tuple[str, int]] = field(default_factory=list)
    top_ax_context: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class WeekRecord:
    label: str
    path: Path
    stats: WeekStats
    themes: list[WeekItem]
    problems: list[WeekItem]


def _section_body(content: str, header_pattern: str, next_header_pattern: str) -> str:
    """Extract the body of a section. Both arguments are regex patterns.

    For literal headers, callers should pass `re.escape(literal)` themselves;
    for headers that vary (e.g. straight vs curly apostrophe), pass the regex.
    """
    pat = re.compile(
        rf"##\s*{header_pattern}\s*\n+(.*?)(?=\n##\s*{next_header_pattern}|\Z)",
        flags=re.DOTALL,
    )
    m = pat.search(content)
    return m.group(1).strip() if m else ""


def _parse_items(body: str) -> list[WeekItem]:
    items: list[WeekItem] = []
    for m in ITEM_RE.finditer(body):
        name = m.group(1).strip()
        summary = m.group(2).strip()
        anchors = VERTEX_RE.findall(summary)
        items.append(WeekItem(name=name, summary=summary, anchors=anchors))
    return items


def _parse_count_pairs(text: str) -> list[tuple[str, int]]:
    """Parse strings like 'Chrome (98), Slack (22), Cursor (12)' → [('Chrome', 98), ...]."""
    pairs = re.findall(r"([^,()]+?)\s*\((\d+)\)", text)
    return [(name.strip(), int(count)) for name, count in pairs if name.strip()]


def parse_stats(content: str) -> WeekStats:
    """Read the ## Stats block from a week file."""
    body = _section_body(content, re.escape("Stats"), r"On my mind|Daily files|$")
    stats = WeekStats()
    if not body:
        return stats
    if m := re.search(r"Entries:\s*\*?\*?(\d[\d,]*)\*?\*?", body):
        stats.entries = int(m.group(1).replace(",", ""))
    if m := re.search(r"Words:\s*\*?\*?(\d[\d,]*)\*?\*?", body):
        stats.words = int(m.group(1).replace(",", ""))
    if m := re.search(r"Active days:\s*\*?\*?(\d+)\*?\*?", body):
        stats.days_active = int(m.group(1))
    if m := re.search(r"Top apps:\s*(.+)", body):
        stats.top_apps = _parse_count_pairs(m.group(1))
    if m := re.search(r"Top on-screen terms.*?:\s*(.+)", body):
        stats.top_ax_context = _parse_count_pairs(m.group(1))
    elif m := re.search(r"ax_context.*?:\s*(.+)", body):
        stats.top_ax_context = _parse_count_pairs(m.group(1))
    return stats


def parse_week_file(path: Path) -> WeekRecord:
    label = path.stem
    content = path.read_text()
    themes_body = _section_body(
        content,
        re.escape("On my mind"),
        r"Problems\s*I[\'’]m solving|Daily files|$",
    )
    problems_body = _section_body(
        content,
        r"Problems\s*I[\'’]m solving",
        r"Daily files|$",
    )
    return WeekRecord(
        label=label,
        path=path,
        stats=parse_stats(content),
        themes=_parse_items(themes_body),
        problems=_parse_items(problems_body),
    )


def load_all_weeks(weeks_dir: Path) -> list[WeekRecord]:
    paths = sorted(weeks_dir.glob("*.md"))
    records: list[WeekRecord] = []
    for p in paths:
        if not re.match(r"\d{4}-W\d{1,2}\.md$", p.name):
            continue
        records.append(parse_week_file(p))
    return records
