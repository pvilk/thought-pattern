"""Wispr Thoughts settings read/write.

The viewer's settings drawer needs to mutate a small slice of config.local.toml
(email enabled/recipient, schedule weekday/hour/minute, source toggles).
Python's stdlib reads TOML via tomllib but doesn't write it, so we do
surgical line-based edits for the keys the UI manages and leave the rest of
the file untouched.

Only six top-level scalar paths are writable here; everything else stays in
the user's hands. This keeps the surface tiny and the diff tool-friendly.

Email passwords go to the macOS Keychain via `security`, never to disk.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_LOCAL = ROOT / "config.local.toml"

# Path -> default. Anything not listed here is read-only from the UI.
WRITABLE: dict[tuple[str, str], object] = {
    ("email", "enabled"):  False,
    ("email", "smtp_to"):  "you@example.com",
    ("email", "smtp_user"): "you@example.com",
    ("schedule", "weekday"): 0,
    ("schedule", "hour"):    9,
    ("schedule", "minute"):  0,
    # Per-source enable toggles. The UI flips these without manual TOML edits.
    ("sources.wispr",   "enabled"): True,
    ("sources.fathom",  "enabled"): False,
    ("sources.granola", "enabled"): True,
    ("sources.notes",   "enabled"): False,
}


def _detect_section(text: str, section: str) -> tuple[int, int] | None:
    """Return (start_line_idx, end_line_idx_exclusive) for a [section] block.

    Section is the literal table header text, e.g. "email" or "sources.fathom"
    for `[email]` and `[sources.fathom]` respectively. The block ends at the
    next `[...]` header or end of file. Comment-only lines after the last
    key/value still belong to the section.
    """
    lines = text.splitlines()
    open_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$")
    next_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    start = None
    for i, line in enumerate(lines):
        if open_re.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if next_re.match(lines[j]):
            end = j
            break
    return start, end


def _format_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return '""'
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write_keys(updates: dict[tuple[str, str], object]) -> None:
    """Surgically update config.local.toml. Creates sections if needed."""
    if not CONFIG_LOCAL.exists():
        raise FileNotFoundError(f"{CONFIG_LOCAL} missing; run bootstrap first.")
    text = CONFIG_LOCAL.read_text()
    lines = text.splitlines()

    # Group updates by section
    by_section: dict[str, list[tuple[str, object]]] = {}
    for (section, key), value in updates.items():
        if (section, key) not in WRITABLE:
            raise ValueError(f"Refusing to write read-only key: [{section}].{key}")
        by_section.setdefault(section, []).append((key, value))

    for section, kvs in by_section.items():
        bounds = _detect_section(text, section)
        if bounds is None:
            # Append fresh section at end
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append(f"[{section}]")
            for k, v in kvs:
                lines.append(f"{k} = {_format_value(v)}")
            text = "\n".join(lines)
            continue

        start, end = bounds
        section_lines = lines[start:end]
        for k, v in kvs:
            key_re = re.compile(rf"^(\s*){re.escape(k)}\s*=.*$")
            replaced = False
            for i, line in enumerate(section_lines):
                if key_re.match(line):
                    indent = key_re.match(line).group(1)
                    section_lines[i] = f"{indent}{k} = {_format_value(v)}"
                    replaced = True
                    break
            if not replaced:
                # Insert after the [section] header
                section_lines.insert(1, f"{k} = {_format_value(v)}")

        lines[start:end] = section_lines
        text = "\n".join(lines)

    if not text.endswith("\n"):
        text += "\n"
    CONFIG_LOCAL.write_text(text)


# --- Keychain ops -----------------------------------------------------------


def keychain_set(service: str, account: str, password: str) -> None:
    """Add/update a generic password in the macOS Keychain."""
    # Delete first if it exists; security add-generic-password without -U
    # rejects duplicates. We avoid -U because `security` man page warns about
    # ACL inheritance differences across macOS versions.
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True, text=True,
    )
    r = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", account, "-w", password],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "security add-generic-password failed")


def keychain_has(service: str, account: str) -> bool:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.returncode == 0
