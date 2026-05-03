#!/usr/bin/env python3
"""Parse a Gmail Takeout mbox into one Markdown file per thread (sent-only voice corpus).

Input:  ./inbox.mbox   (Gmail Takeout export, "Sent" label only)
Output: ./threads/*.md (one per email thread, vertex-tagged g<thread_hash>.m<NNN>.me)
        ./INDEX.md     (year-grouped index)

Usage:
    python3 parse_mbox.py
    python3 parse_mbox.py --in /path/to/Sent.mbox --out ./threads --me me@example.com
"""

from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import mailbox
import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

ME_DEFAULT = "me@example.com"

# --- Cleaning ----------------------------------------------------------------

QUOTED_LINE = re.compile(r"^\s*>")
ON_WROTE = re.compile(
    r"^On (Mon|Tue|Wed|Thu|Fri|Sat|Sun|[A-Z][a-z]+,? )?.*?wrote:\s*$",
    re.MULTILINE,
)
SIG_DELIM = re.compile(r"^-- ?$", re.MULTILINE)
FORWARDED = re.compile(r"^-+ ?Forwarded message ?-+", re.MULTILINE)
ORIGINAL = re.compile(r"^-+ ?Original Message ?-+", re.MULTILINE)
FROM_LINE = re.compile(r"^From: .+$", re.MULTILINE)


def html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;|&apos;", "'", text)
    return text


def get_body(msg: email.message.Message) -> str:
    """Return the best plain-text body. Prefer text/plain; fall back to text/html stripped."""
    if msg.is_multipart():
        plain, html = None, None
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype == "text/plain" and plain is None:
                plain = safe_get_payload(part)
            elif ctype == "text/html" and html is None:
                html = safe_get_payload(part)
        return plain if plain else html_to_text(html or "")
    payload = safe_get_payload(msg)
    if msg.get_content_type() == "text/html":
        return html_to_text(payload)
    return payload


def safe_get_payload(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return part.get_payload() if isinstance(part.get_payload(), str) else ""


def strip_quoted_history(body: str) -> str:
    """Remove quoted reply text, sig, forwarded headers; keep only what you wrote."""
    # Cut at signature delimiter
    parts = SIG_DELIM.split(body, maxsplit=1)
    body = parts[0]
    # Cut at "On ... wrote:" markers
    body = ON_WROTE.split(body, maxsplit=1)[0]
    # Cut at forwarded/original message markers
    body = FORWARDED.split(body, maxsplit=1)[0]
    body = ORIGINAL.split(body, maxsplit=1)[0]
    # Drop quoted lines
    lines = []
    for line in body.splitlines():
        if QUOTED_LINE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    # Drop standalone "From: x" header lines that sometimes leak
    text = FROM_LINE.sub("", text)
    return text.strip()


# --- Threading ---------------------------------------------------------------

def thread_key(msg) -> str:
    """Group messages into threads via References / In-Reply-To, fall back to Subject."""
    refs = msg.get("References", "") or ""
    irt = msg.get("In-Reply-To", "") or ""
    msgids = re.findall(r"<[^>]+>", refs + " " + irt)
    if msgids:
        return msgids[0].strip()
    subj = (msg.get("Subject") or "").strip()
    subj = re.sub(r"^(re|fwd|fw):\s*", "", subj, flags=re.IGNORECASE)
    subj = re.sub(r"^(re|fwd|fw):\s*", "", subj, flags=re.IGNORECASE)  # twice for nested
    return f"subject:{subj.lower()}"


def short_hash(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


# --- Parsing -----------------------------------------------------------------

def parse_addr(raw: str) -> str:
    addrs = getaddresses([raw or ""])
    if not addrs:
        return ""
    name, addr = addrs[0]
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name


def parse_addr_list(raw: str) -> list[str]:
    return [parse_addr(f"{n} <{a}>" if n else a) for n, a in getaddresses([raw or ""]) if a]


def msg_date(msg) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"^(re|fwd|fw):\s*", "", s or "no-subject", flags=re.IGNORECASE)
    s = re.sub(r"^(re|fwd|fw):\s*", "", s, flags=re.IGNORECASE)
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return (s[:maxlen] or "no-subject").rstrip("-")


# --- Rendering ---------------------------------------------------------------

def render_thread(thash: str, messages: list[dict], me: str) -> str:
    messages.sort(key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc))
    first = messages[0]
    title = first["subject"] or "(no subject)"
    title = re.sub(r"^(re|fwd|fw):\s*", "", title, flags=re.IGNORECASE).strip() or "(no subject)"

    # Frontmatter
    fm = ["---", f'title: "{title.replace(chr(34), chr(39))}"',
          f'thread_id: g{thash}',
          f'first_date: {first["date"].isoformat() if first["date"] else ""}',
          f'message_count: {len(messages)}']
    participants = set()
    for m in messages:
        participants.add(m["from_addr"])
        for r in m["to_addrs"]:
            participants.add(r)
    participants.discard("")
    if participants:
        fm.append("participants:")
        for p in sorted(participants):
            fm.append(f'  - "{p}"')
    fm.append("---")

    body = ["", f"# {title}", ""]
    for i, m in enumerate(messages):
        is_me = me in (m["from_addr"] or "").lower()
        spk = "me" if is_me else "other"
        v = f"g{thash}.m{i:03d}.{spk}"
        date_str = m["date"].strftime("%Y-%m-%d %H:%M") if m["date"] else "?"
        body.append(f"### `{v}` · {date_str} · _{m['from_addr'] or '?'}_")
        body.append("")
        text = m["clean_body"] if is_me else m["clean_body"]
        # Indent all lines as quote
        for line in text.splitlines():
            body.append(f"> {line}" if line.strip() else ">")
        body.append("")
    return "\n".join(fm + body)


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="inbox.mbox")
    ap.add_argument("--out", default="threads")
    ap.add_argument("--me", default=ME_DEFAULT, help=f"Your email (default: {ME_DEFAULT})")
    args = ap.parse_args()

    inpath = Path(args.infile)
    outpath = Path(args.out)
    if not inpath.exists():
        raise SystemExit(f"mbox not found: {inpath}\n\nGmail Takeout: https://takeout.google.com/ → Mail (mbox), Sent label only.")

    outpath.mkdir(parents=True, exist_ok=True)
    me = args.me.lower()

    print(f"reading {inpath}…")
    box = mailbox.mbox(str(inpath), factory=lambda f: email.message_from_binary_file(f, policy=email.policy.default))

    threads: dict[str, list[dict]] = defaultdict(list)
    n_msgs = n_me = 0

    for msg in box:
        n_msgs += 1
        from_addr = parse_addr(msg.get("From", "")).lower()
        if me not in from_addr:
            # Sent-label export should be all you, but be defensive
            continue
        n_me += 1
        body = get_body(msg)
        clean = strip_quoted_history(body)
        if not clean:
            continue
        tk = thread_key(msg)
        thash = short_hash(tk)
        threads[thash].append({
            "subject": msg.get("Subject", ""),
            "from_addr": parse_addr(msg.get("From", "")),
            "to_addrs": parse_addr_list(msg.get("To", "")),
            "date": msg_date(msg),
            "clean_body": clean,
        })

    print(f"  parsed {n_msgs} messages, kept {n_me} from {me}, grouped into {len(threads)} threads")

    manifest = []
    for thash, msgs in threads.items():
        # Use the latest date in the thread for filename
        latest = max((m["date"] for m in msgs if m["date"]), default=None)
        date_part = latest.strftime("%Y-%m-%d") if latest else "0000-00-00"
        title = msgs[0]["subject"] or "no-subject"
        slug = slugify(title)
        fname = f"{date_part}_{slug}_{thash}.md"
        (outpath / fname).write_text(render_thread(thash, msgs, me), encoding="utf-8")
        manifest.append({"date": date_part, "title": re.sub(r"^(re|fwd|fw):\s*", "", title, flags=re.IGNORECASE).strip() or "(no subject)", "file": fname})

    # Index
    manifest.sort(key=lambda x: x["date"], reverse=True)
    by_year: dict[str, list[dict]] = defaultdict(list)
    for m in manifest:
        by_year[m["date"][:4]].append(m)
    lines = ["# Gmail Sent Archive", "", f"Total threads: {len(manifest)}", ""]
    for year in sorted(by_year, reverse=True):
        lines += [f"## {year}", ""]
        for m in by_year[year]:
            lines.append(f"- {m['date']}: [{m['title']}]({m['file']})")
        lines.append("")
    (outpath.parent / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\ndone: {len(manifest)} threads written to {outpath}/")


if __name__ == "__main__":
    main()
