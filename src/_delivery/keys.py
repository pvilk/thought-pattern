"""Persist secret env vars to ~/.zshrc and the current process.

The launchd cron runs weekly_email.py via `zsh -lc`, which sources ~/.zshrc.
Writing keys there means both interactive shells and the cron see them.

This generalizes the FATHOM_API_KEY pattern to any secret. New providers
(Resend, future Discord webhook tokens, etc.) just call persist_env_key().
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ZSHRC = Path.home() / ".zshrc"


def persist_env_key(name: str, value: str) -> None:
    """Write `export NAME='value'` to ~/.zshrc and set in current process.

    Replaces any existing line with the same name. Single-quotes the value
    (with proper escape for embedded quotes) so shell parsing is safe.
    """
    if not name or not name.strip():
        raise ValueError("env var name must be non-empty")
    safe = value.replace("'", "'\\''")
    new_line = f"export {name}='{safe}'"
    pat = re.compile(rf"^\s*export\s+{re.escape(name)}=.*$", re.MULTILINE)

    if ZSHRC.exists():
        text = ZSHRC.read_text()
        if pat.search(text):
            text = pat.sub(new_line, text)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += new_line + "\n"
    else:
        text = new_line + "\n"
    ZSHRC.write_text(text)
    os.environ[name] = value


def env_key_present(name: str) -> bool:
    """True if the var is set in the current env or persisted in ~/.zshrc."""
    if os.environ.get(name, "").strip():
        return True
    if not ZSHRC.exists():
        return False
    try:
        for line in ZSHRC.read_text().splitlines():
            if line.strip().startswith(f"export {name}=") and len(line.strip()) > len(f"export {name}="):
                return True
    except OSError:
        pass
    return False
