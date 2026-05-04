"""Pluggable email/notification delivery for the weekly digest.

Today there's exactly one provider: Resend. The dispatcher reads the
`[delivery]` block from config.local.toml and delegates. No-ops silently
when delivery is disabled or the API key is missing — the local viewer is
always the canonical output, email is purely additive.

Adding a new provider (Discord webhook, ntfy, Apple Notes) is a new module
under this package + a new branch in send_digest().
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as a top-level package from src/ without weird imports.
_THIS = Path(__file__).resolve().parent
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from _config import load as load_config

from . import keys, resend  # noqa: F401
from .resend import ResendError

from pathlib import Path  # noqa: E402  (kept here so it's available in send_test)


DEFAULT_FROM = "onboarding@resend.dev"


def is_configured() -> dict:
    """Return {enabled, key_set, from_email, to_email} for the panel UI."""
    cfg = (load_config().get("delivery") or {})
    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "from_email": cfg.get("from_email") or DEFAULT_FROM,
        "to_email":   cfg.get("to_email", ""),
        "key_set":    keys.env_key_present("RESEND_API_KEY"),
    }


def send_digest(subject: str, body_md: str, week_label: str) -> None:
    """Dispatch the digest to whatever provider is configured.

    Silently skips when delivery is off or unconfigured. Raises ResendError
    for actionable failures (bad key, unverified domain, network) so the
    caller can log and surface the message.
    """
    cfg = (load_config().get("delivery") or {})
    if not cfg.get("enabled", False):
        return
    if not os.environ.get("RESEND_API_KEY", "").strip():
        return
    if not cfg.get("to_email"):
        return
    cfg = {**cfg, "from_email": cfg.get("from_email") or DEFAULT_FROM}
    resend.send(subject, body_md, week_label, cfg)


def send_test(subject: str | None = None, note: str | None = None) -> dict:
    """Send a test email through the configured provider.

    Uses the most recent themed week's digest as the body so the user sees
    what their real Sunday email will look like. Falls back to a small
    sample if no digest has been generated yet. Bypasses the `enabled`
    toggle since the user is explicitly testing.
    """
    cfg = (load_config().get("delivery") or {})
    if not cfg.get("to_email"):
        raise ResendError(0, "Add a 'To' email before testing.")
    cfg = {**cfg, "from_email": cfg.get("from_email") or DEFAULT_FROM}

    # Pick the most recent themed week and reuse the viewer's assembly so
    # the email shows the same clean "On my mind" + "Problems I'm solving"
    # layout — no voice stats, no auditor "What I noticed".
    body_md, label = _latest_week_body()
    if note:
        body_md += f"\n_{note}_\n"
    if subject is None:
        from .render import week_short_from_label
        subject = f"Weekly Life Recap ({week_short_from_label(label)})"
    return resend.send(subject, body_md, label, cfg)


def _latest_week_body() -> tuple[str, str]:
    """Return (clean markdown body, week label) using the viewer's renderer.

    Falls back to a minimal sample when no themed weeks exist yet so a
    first-run user still sees a representative test email.
    """
    import sys
    src = Path(__file__).resolve().parent.parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import build_viewer
        labels = build_viewer.discover_weeks()
        themed = [w for w in labels if build_viewer.has_themed_content(w)]
        if themed:
            label = themed[-1]
            return build_viewer.assemble_week_markdown(label), label
    except Exception:
        pass
    sample = (
        "## On my mind\n"
        "\n"
        "- **Sample theme**: This is what your real digest will look like once the "
        "Sunday auto-sync has themed at least one week. The format mirrors the local viewer.\n"
        "\n"
        "## Problems I'm solving\n"
        "\n"
        "- **Sample problem**: Real items will appear here drawn from your dictation "
        "and meeting transcripts.\n"
    )
    return sample, "test"
