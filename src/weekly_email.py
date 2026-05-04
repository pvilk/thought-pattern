#!/usr/bin/env python3
"""Wispr Thoughts: unified Sunday-morning weekly digest.

Pipeline:
  1. Refresh enabled sources (Wispr / Fathom / Granola)
  2. Compute last completed week
  3. Theme that week in voice + meeting corpora
  4. Run trends + both auditor passes
  5. Assemble unified digest, write to disk
  6. Email it (unless --dry-run or email disabled)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

from _config import load as load_config, resolve_path

CFG = load_config()
SRC = Path(__file__).resolve().parent
ROOT = SRC.parent

WEEKS_DIR = resolve_path(CFG, "weeks_dir")
MEETING_WEEKS_DIR = resolve_path(CFG, "master_dir") / "50_weeks"
DIGESTS_DIR = resolve_path(CFG, "digests_dir")
LOGS_DIR = resolve_path(CFG, "logs_dir")
LOCK_FILE = ROOT / ".weekly-email.lock"
DESKTOP = Path.home() / "Desktop"

EMAIL_CFG = CFG.get("email", {})
SOURCES_CFG = CFG.get("sources", {})


# ----- logging ---------------------------------------------------------------


def log(msg: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with (LOGS_DIR / "wispr-thoughts.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ----- week math -------------------------------------------------------------


def week_label_for(d: dt.date) -> str:
    days_since_sunday = (d.weekday() + 1) % 7
    sunday = d - dt.timedelta(days=days_since_sunday)
    saturday = sunday + dt.timedelta(days=6)
    week_year = saturday.year
    jan1 = dt.date(week_year, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - dt.timedelta(days=jan1_offset)
    return f"{week_year}-W{((sunday - w01_sunday).days // 7) + 1:02d}"


def last_completed_week(today: dt.date) -> str:
    days_back = (today.weekday() + 2) % 7
    if days_back == 0:
        days_back = 7
    return week_label_for(today - dt.timedelta(days=days_back))


def week_range(label: str) -> tuple[dt.date, dt.date]:
    y, w = re.match(r"(\d{4})-W(\d{1,2})", label).groups()
    y, w = int(y), int(w)
    jan1 = dt.date(y, 1, 1)
    jan1_offset = (jan1.weekday() + 1) % 7
    w01_sunday = jan1 - dt.timedelta(days=jan1_offset)
    sunday = w01_sunday + dt.timedelta(days=(w - 1) * 7)
    return sunday, sunday + dt.timedelta(days=6)


# ----- subprocess wrapper ----------------------------------------------------


def run_step(name: str, cmd: list[str], capture: bool = False, optional: bool = True):
    log(f"step: {name}: {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=capture, cwd=SRC)
    except subprocess.CalledProcessError as e:
        log(f"  {name} failed (exit {e.returncode})")
        if optional:
            return None
        raise


# ----- digest assembly -------------------------------------------------------


def extract_section(content: str, header: str, end: str) -> str:
    pat = re.compile(rf"##\s*{header}\s*\n+(.*?)(?=\n##\s*{end}|\Z)", flags=re.DOTALL)
    m = pat.search(content)
    return m.group(1).strip() if m else ""


def read_week(corpus_dir: Path, label: str) -> dict | None:
    p = corpus_dir / f"{label}.md"
    if not p.exists():
        return None
    c = p.read_text()
    return {
        "stats":    extract_section(c, re.escape("Stats"), r"On my mind|Daily files|Meetings this week|$").strip(),
        "on_mind":  extract_section(c, re.escape("On my mind"), r"Problems\s*I[\'’]m solving").strip(),
        "problems": extract_section(c, r"Problems\s*I[\'’]m solving", r"Daily files|Meetings this week|$").strip(),
    }


def build_digest(label: str, voice_auditor: str | None, meeting_auditor: str | None) -> str:
    sun, sat = week_range(label)
    voice = read_week(WEEKS_DIR, label)
    meeting = read_week(MEETING_WEEKS_DIR, label)

    parts = [f"# Weekly digest, {label} ({sun.strftime('%b %d')} to {sat.strftime('%b %d %Y')})", ""]
    if voice:
        parts += ["## Voice stats (solo)", "", voice["stats"], ""]
    if meeting:
        parts += ["## Meeting stats (in conversation)", "", meeting["stats"], ""]

    if voice and voice["on_mind"]:
        parts += ["## On my mind (solo)", "", voice["on_mind"], ""]
    if meeting and meeting["on_mind"]:
        parts += ["## On my mind (in conversation)", "", meeting["on_mind"], ""]
    if voice and voice["problems"]:
        parts += ["## Problems I'm solving (solo)", "", voice["problems"], ""]
    if meeting and meeting["problems"]:
        parts += ["## Problems I'm solving (in conversation)", "", meeting["problems"], ""]

    if voice_auditor:
        parts += [voice_auditor.strip(), ""]
    if meeting_auditor:
        parts += [meeting_auditor.strip(), ""]
    if not voice and not meeting:
        parts.append("_No themed content for this week._")
    return "\n".join(parts)


# ----- email -----------------------------------------------------------------


VERTEX_PAREN_RE = re.compile(
    r"\s*\((?:(?:[a-zA-Z][\w-]+\.[\w-]+(?:\.[\w-]+)?)(?:[,;]\s*)?)+\)"
)


def format_for_email(md: str) -> str:
    """Plain-text version of the digest for the multipart email fallback.

    Strips vertex anchors and removes markdown bolding markers so plain-text
    clients don't render literal asterisks. Headings become uppercase + a
    blank line under them so they read as section breaks even without
    formatting.
    """
    text = VERTEX_PAREN_RE.sub("", md)
    text = re.sub(r" +$", "", text, flags=re.MULTILINE)

    def upcase_heading(m):
        return m.group(1).upper()

    text = re.sub(r"^#{1,6}\s+(.+?)\s*$", upcase_heading, text, flags=re.MULTILINE)
    # Strip markdown emphasis markers (**bold** / *italic*) from plain text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text)


# --- Markdown -> HTML (stdlib only) -----------------------------------------


_HTML_INLINE_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HTML_INLINE_RE_EM   = re.compile(r"(?<!\w)\*(.+?)\*(?!\w)", re.DOTALL)
_HTML_INLINE_RE_CODE = re.compile(r"`([^`]+)`")


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def _inline_md_to_html(text: str) -> str:
    """Convert inline markdown (bold/italic/code) on already-escaped text."""
    text = _HTML_INLINE_RE_BOLD.sub(r"<strong>\1</strong>", text)
    text = _HTML_INLINE_RE_EM.sub(r"<em>\1</em>", text)
    text = _HTML_INLINE_RE_CODE.sub(r"<code>\1</code>", text)
    return text


def format_for_email_html(md: str) -> str:
    """Render the digest as a self-contained HTML body. No external CSS.

    Stdlib-only mini-renderer. Handles `# H1` through `### H3`, `- bullets`,
    `> quotes`, blank-line paragraph breaks, plus inline `**bold**` and
    `*italic*`. Anything more exotic falls back to a plain `<p>`.
    """
    md = VERTEX_PAREN_RE.sub("", md)
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
                out.append(f'<p style="margin:8px 0;">{_inline_md_to_html(_html_escape(joined))}</p>')
            in_para = []

    def close_lists():
        nonlocal in_ul, in_blockquote
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_blockquote:
            out.append("</blockquote>")
            in_blockquote = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            close_lists()
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            flush_para(); close_lists()
            level = min(len(m.group(1)), 4)  # collapse h5/h6 to h4
            sizes = {1: 28, 2: 20, 3: 16, 4: 14}
            margins = {1: "24px 0 12px", 2: "20px 0 10px", 3: "16px 0 8px", 4: "14px 0 6px"}
            out.append(
                f'<h{level} style="font-size:{sizes[level]}px;'
                f' font-weight:600; line-height:1.25; margin:{margins[level]};'
                f' border-bottom:{"1px solid #e5e5e7" if level == 2 else "none"};'
                f' padding-bottom:{"6px" if level == 2 else "0"};">'
                f'{_inline_md_to_html(_html_escape(m.group(2).strip()))}'
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
                f'<li style="margin:6px 0;">{_inline_md_to_html(_html_escape(m.group(1).strip()))}</li>'
            )
            continue

        m = re.match(r"^>\s?(.*)$", line)
        if m:
            flush_para()
            if in_ul:
                out.append("</ul>"); in_ul = False
            if not in_blockquote:
                out.append(
                    '<blockquote style="border-left:3px solid #e5e5e7; '
                    'margin:12px 0; padding:0 12px; color:#6b6b6b;">'
                )
                in_blockquote = True
            out.append(_inline_md_to_html(_html_escape(m.group(1))))
            continue

        # Default: prose paragraph
        if in_ul:    out.append("</ul>"); in_ul = False
        if in_blockquote: out.append("</blockquote>"); in_blockquote = False
        in_para.append(line)

    flush_para()
    close_lists()

    body = "\n".join(out) or '<p>No content.</p>'
    return (
        '<!doctype html><html><body style="'
        'font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;'
        'color:#1a1a1a; max-width:680px; margin:0 auto; padding:24px; line-height:1.55;">'
        f'{body}'
        '</body></html>'
    )


def get_smtp_password() -> str:
    env = os.environ.get("WISPRTHOUGHTS_SMTP_PASSWORD")
    if env:
        return env.strip()
    svc = EMAIL_CFG.get("keychain_service", "wispr-thoughts-smtp")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", svc, "-w"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"SMTP password not in keychain. Run:\n"
            f"  security add-generic-password -s {svc} -a <email> -w '<gmail-app-password>'"
        )
    return result.stdout.strip()


def send_email(label: str, md: str, partial: bool) -> None:
    msg = EmailMessage()
    on_mind = sum(
        len(re.findall(r"^- \*\*", body, re.MULTILINE))
        for body in re.findall(r"##\s*On my mind[^\n]*\n+(.*?)(?=\n##|\Z)", md, flags=re.DOTALL)
    )
    problems = sum(
        len(re.findall(r"^- \*\*", body, re.MULTILINE))
        for body in re.findall(r"##\s*Problems\s*I[\'’]m solving[^\n]*\n+(.*?)(?=\n##|\Z)", md, flags=re.DOTALL)
    )
    prefix = "[partial] " if partial else ""
    msg["Subject"] = f"{prefix}Wispr Thoughts {label}: {on_mind} themes, {problems} problems"
    msg["From"] = EMAIL_CFG["smtp_user"]
    msg["To"] = EMAIL_CFG["smtp_to"]
    # Multipart: plain-text fallback for clients that strip HTML, rich body
    # otherwise. Most modern clients (Gmail, Apple Mail, Outlook) prefer the
    # HTML alternative when both are present.
    msg.set_content(format_for_email(md))
    msg.add_alternative(format_for_email_html(md), subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(EMAIL_CFG["smtp_host"], EMAIL_CFG["smtp_port"], context=ctx) as s:
        s.login(EMAIL_CFG["smtp_user"], get_smtp_password())
        s.send_message(msg)
    log(f"  emailed: {msg['Subject']!r}")


def update_desktop_copy(label: str, digest_path: Path) -> Path:
    """Mirror the assembled digest to ~/Desktop/wispr-thoughts-digest-<W>.md.

    Never deletes existing digests on disk. The user is the only thing
    allowed to remove these — once a weekly digest lands, it stays.
    """
    DESKTOP.mkdir(parents=True, exist_ok=True)
    target = DESKTOP / f"wispr-thoughts-digest-{label}.md"
    target.write_text(digest_path.read_text())
    return target


# ----- locking ---------------------------------------------------------------


def acquire_lock() -> None:
    """Refuse to run if another sync is genuinely active; clear stale locks.

    A "stale" lock is one whose recorded PID isn't alive anymore (e.g., a
    previous run was killed mid-flight or crashed). os.kill(pid, 0) raises
    ProcessLookupError if no such process exists, PermissionError if the
    process exists but is owned by another user, and otherwise returns
    silently meaning the process is alive.
    """
    if LOCK_FILE.exists():
        try:
            pid_str = LOCK_FILE.read_text().strip().split()[0]
            pid = int(pid_str)
        except (OSError, ValueError, IndexError):
            pid = None
        alive = False
        if pid is not None:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
        if alive:
            sys.exit(f"Run already in progress (PID {pid}, lock: {LOCK_FILE}).")
        # Lock is orphaned; reclaim it
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    LOCK_FILE.write_text(f"{os.getpid()} {dt.datetime.now().isoformat()}\n")


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ----- main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week")
    ap.add_argument("--no-refresh", action="store_true")
    args = ap.parse_args()

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    acquire_lock()
    partial = False
    try:
        target = args.week or last_completed_week(dt.date.today())
        log(f"=== wispr-thoughts {target} ===")

        if not args.no_refresh:
            if SOURCES_CFG.get("wispr", {}).get("enabled", True):
                if not run_step("export-wispr", ["python3", "export_wispr.py", "--refresh-snapshot"]):
                    partial = True
            if SOURCES_CFG.get("fathom", {}).get("enabled", False):
                if not run_step("export-fathom", ["python3", "export_fathom.py"]):
                    partial = True
            if SOURCES_CFG.get("granola", {}).get("enabled", False):
                if not run_step("export-granola", ["python3", "export_granola.py"]):
                    partial = True
            if SOURCES_CFG.get("notes", {}).get("enabled", False):
                if not run_step("export-notes", ["python3", "export_notes.py"]):
                    partial = True

        # Theme target week in both corpora
        if SOURCES_CFG.get("wispr", {}).get("enabled", True):
            if not run_step("themes-voice", ["python3", "build_themes.py", "--week", target]):
                partial = True
        if any(SOURCES_CFG.get(k, {}).get("enabled", False) for k in ("fathom", "granola")):
            if not run_step("themes-meetings", ["python3", "build_themes_meetings.py", "--week", target]):
                partial = True

        # Trends (Wispr-side only for now)
        if not run_step("trends", ["python3", "build_trends.py"]):
            partial = True

        voice_auditor = None
        if SOURCES_CFG.get("wispr", {}).get("enabled", True):
            r = run_step("auditor-voice", ["python3", "build_auditor.py", "--week", target], capture=True)
            if r and r.stdout:
                voice_auditor = r.stdout.strip()

        meeting_auditor = None
        if any(SOURCES_CFG.get(k, {}).get("enabled", False) for k in ("fathom", "granola")):
            r = run_step("auditor-meetings", ["python3", "build_auditor_meetings.py", "--week", target], capture=True)
            if r and r.stdout:
                meeting_auditor = r.stdout.strip()

        digest_md = build_digest(target, voice_auditor, meeting_auditor)
        digest_path = DIGESTS_DIR / f"{target}.md"
        digest_path.write_text(digest_md)
        log(f"  wrote {digest_path}")

        if args.dry_run:
            log("  --dry-run")
            print("\n--- DIGEST PREVIEW ---\n")
            print(digest_md)
            return 1 if partial else 0

        update_desktop_copy(target, digest_path)
        if EMAIL_CFG.get("enabled", True):
            try:
                send_email(target, digest_md, partial)
            except Exception as e:
                log(f"  email failed: {e}")
                partial = True
        return 1 if partial else 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
