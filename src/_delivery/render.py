"""Markdown → email-safe HTML and plain text.

Tiny stdlib renderer. Handles the structures that show up in our weekly
digests: H1-H4 headings, `- bullets`, `> quotes`, `**bold**`, `*italic*`,
inline `code`. Anything more exotic falls back to a plain `<p>`.

This was previously inlined in weekly_email.py; now it lives in _delivery
so adapters (Resend today, others later) share one rendering path.

The output style matches the local viewer's newspaper palette: cream
paper, warm-dark ink, classic blue accent on bullet titles.
"""

from __future__ import annotations

import datetime as _dt
import re

# Strip vertex anchors like (w20260415.034) from rendered text — they're
# useful in the source files for traceability but clutter email bodies.
_VERTEX_PAREN_RE = re.compile(
    r"\s*\((?:(?:[a-zA-Z][\w-]+\.[\w-]+(?:\.[\w-]+)?)(?:[,;]\s*)?)+\)"
)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_EM_RE   = re.compile(r"(?<!\w)\*(.+?)\*(?!\w)", re.DOTALL)
_CODE_RE = re.compile(r"`([^`]+)`")


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _inline(text: str) -> str:
    # Style <strong> with the newspaper-blue accent so bullet titles in the
    # email match the way they look in the local viewer.
    text = _BOLD_RE.sub(
        r'<strong style="color:#1d4ed8; font-weight:600;">\1</strong>',
        text,
    )
    text = _EM_RE.sub(r"<em>\1</em>", text)
    text = _CODE_RE.sub(
        r'<code style="background:#f3eedf; padding:1px 5px; border-radius:3px; '
        r'font-family:ui-monospace,Menlo,monospace; font-size:0.9em;">\1</code>',
        text,
    )
    return text


# --- Week label → "Apr 26 to May 2, 2026" -----------------------------------


def _parse_week_label(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", label)
    if not m:
        raise ValueError(label)
    return int(m.group(1)), int(m.group(2))


def _week_bounds(label: str) -> tuple[_dt.date, _dt.date] | None:
    """Return the (Sunday, Saturday) bounds for a "YYYY-Www" label."""
    try:
        y, w = _parse_week_label(label)
    except ValueError:
        return None
    jan1 = _dt.date(y, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - _dt.timedelta(days=jan1_offset)
    sunday = w01_sunday + _dt.timedelta(days=(w - 1) * 7)
    return sunday, sunday + _dt.timedelta(days=6)


def week_range_from_label(label: str) -> str:
    """Convert "2026-W18" → "Apr 26 to May 02 2026" (Sunday-Saturday).

    Always shows both month names and zero-padded days, no comma before
    the year. Used in the email title.
    """
    bounds = _week_bounds(label)
    if bounds is None:
        return label
    sunday, saturday = bounds
    return (
        f"{sunday.strftime('%b')} {sunday.day:02d} to "
        f"{saturday.strftime('%b')} {saturday.day:02d} {saturday.year}"
    )


def week_short_from_label(label: str) -> str:
    """Convert "2026-W18" → "Apr 26 - May 02" (no year). Used in subject lines."""
    bounds = _week_bounds(label)
    if bounds is None:
        return label
    sunday, saturday = bounds
    return (
        f"{sunday.strftime('%b')} {sunday.day:02d} - "
        f"{saturday.strftime('%b')} {saturday.day:02d}"
    )


def render_html_body(md: str) -> str:
    """Render the digest markdown body as inline-styled HTML.

    Returns just the body content (no <html>/<body> wrapper) — callers wrap
    it in their own template envelope.
    """
    md = _VERTEX_PAREN_RE.sub("", md)
    lines = md.split("\n")
    out: list[str] = []
    in_ul = False
    in_blockquote = False
    in_para: list[str] = []

    def flush_para():
        nonlocal in_para
        if in_para:
            joined = " ".join(s.strip() for s in in_para if s.strip())
            if joined:
                out.append(f'<p style="margin:8px 0;">{_inline(_escape(joined))}</p>')
            in_para = []

    def close_lists():
        nonlocal in_ul, in_blockquote
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_blockquote:
            out.append("</blockquote>"); in_blockquote = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            close_lists()
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            flush_para(); close_lists()
            level = min(len(m.group(1)), 4)
            sizes   = {1: 28, 2: 20, 3: 16, 4: 14}
            margins = {1: "24px 0 12px", 2: "20px 0 10px", 3: "16px 0 8px", 4: "14px 0 6px"}
            out.append(
                f'<h{level} style="font-size:{sizes[level]}px;'
                f' font-weight:600; line-height:1.25; margin:{margins[level]};'
                f' border-bottom:{"1px solid #e6dfd0" if level == 2 else "none"};'
                f' padding-bottom:{"6px" if level == 2 else "0"};">'
                f'{_inline(_escape(m.group(2).strip()))}'
                f'</h{level}>'
            )
            continue

        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            flush_para()
            if in_blockquote:
                out.append("</blockquote>"); in_blockquote = False
            if not in_ul:
                out.append('<ul style="padding-left:22px; margin:8px 0;">')
                in_ul = True
            out.append(
                f'<li style="margin:6px 0;">{_inline(_escape(m.group(1).strip()))}</li>'
            )
            continue

        m = re.match(r"^>\s?(.*)$", line)
        if m:
            flush_para()
            if in_ul:
                out.append("</ul>"); in_ul = False
            if not in_blockquote:
                out.append(
                    '<blockquote style="border-left:3px solid #e6dfd0; '
                    'margin:12px 0; padding:0 12px; color:#756f63;">'
                )
                in_blockquote = True
            out.append(_inline(_escape(m.group(1))))
            continue

        if in_ul:        out.append("</ul>"); in_ul = False
        if in_blockquote: out.append("</blockquote>"); in_blockquote = False
        in_para.append(line)

    flush_para()
    close_lists()
    return "\n".join(out) or '<p style="margin:8px 0;">No content.</p>'


def render_plain(md: str) -> str:
    """Plain-text fallback for clients that strip HTML.

    Headings become uppercase, bold/italic markers are stripped, vertex
    anchors removed.
    """
    text = _VERTEX_PAREN_RE.sub("", md)
    text = re.sub(r" +$", "", text, flags=re.MULTILINE)
    text = re.sub(
        r"^#{1,6}\s+(.+?)\s*$",
        lambda m: m.group(1).upper(),
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text)
