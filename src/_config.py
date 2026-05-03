"""Wispr Thoughts config loader. Reads config.local.toml; falls back to env vars."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_LOCAL = ROOT / "config.local.toml"
CONFIG_EXAMPLE = ROOT / "config.example.toml"


def _expand(value):
    if isinstance(value, str) and value.startswith("~/"):
        return str(Path(value).expanduser())
    return value


def _walk_expand(d):
    if isinstance(d, dict):
        return {k: _walk_expand(v) for k, v in d.items()}
    return _expand(d)


def load() -> dict:
    """Load merged config. Local overrides example; env overrides both for secrets."""
    if not CONFIG_LOCAL.exists():
        if not CONFIG_EXAMPLE.exists():
            sys.exit("Missing config.example.toml; clone the repo fresh.")
        sys.exit(
            f"Missing {CONFIG_LOCAL}.\n"
            f"\n"
            f"Quickest path: ./demo.sh to see a sample digest in 30 seconds.\n"
            f"Real data path: ./bootstrap.sh to wire up Wispr Flow / Fathom / Granola.\n"
            f"Manual path:\n"
            f"  cp {CONFIG_EXAMPLE} {CONFIG_LOCAL}\n"
            f"  $EDITOR {CONFIG_LOCAL}"
        )
    with open(CONFIG_LOCAL, "rb") as fh:
        cfg = tomllib.load(fh)
    return _walk_expand(cfg)


def resolve_path(cfg: dict, key: str) -> Path:
    """Resolve a [paths] entry relative to ROOT (so users can keep data in-repo or anywhere)."""
    raw = cfg.get("paths", {}).get(key)
    if not raw:
        sys.exit(f"Missing paths.{key} in config.local.toml")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    return p
