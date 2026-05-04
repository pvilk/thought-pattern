"""Resend delivery adapter.

Sends the weekly digest as a multipart HTML+plain email via the Resend API.
Reads RESEND_API_KEY from os.environ (loaded from ~/.zshrc by serve.py on
boot or by the launchd job's `zsh -lc` shell).

Default newspaper-style template is hardcoded here. No user-customization
yet — see the Plan v2 doc for Phase B's editor + override path.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request

from .render import render_html_body, render_plain, week_range_from_label

API_URL = "https://api.resend.com/emails"
TIMEOUT_S = 30


# --- Newspaper-style outer template ----------------------------------------
#
# Mirrors the local viewer (data/viewer/index.html) as closely as inline-CSS
# email allows: cream paper, large serif "Weekly Digest" title centered above
# a muted date range, then the digest content as section headings + bullets.

_DEFAULT_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{{subject}}</title></head>
<body style="margin:0; padding:0; background:#faf6ec;">
  <div style="font-family: 'New York', Charter, Georgia, 'Times New Roman', serif;
              background:#faf6ec; color:#1c1916;
              max-width:680px; margin:0 auto; padding:48px 24px 32px;
              line-height:1.55;">

    <h1 style="font-family: 'New York', Charter, Georgia, 'Times New Roman', serif;
               font-size:32px; font-weight:500; letter-spacing:-0.02em;
               line-height:1.15; margin:0 0 36px; text-align:center;">
      Weekly Digest: {{week_range}}
    </h1>

    <div style="font-family:-apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
                font-size:15px; line-height:1.55;">
      {{body_html}}
    </div>

    <hr style="border:none; border-top:1px solid #e6dfd0; margin:40px 0 16px;">
    <p style="color:#756f63; font-size:12px; text-align:center;
              font-family:-apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;">
      Generated locally by Wispr Thoughts.
    </p>
  </div>
</body>
</html>
"""


def _wrap(subject: str, week_label: str, body_md: str) -> str:
    body_html = render_html_body(body_md)
    week_range = week_range_from_label(week_label)
    return (
        _DEFAULT_TEMPLATE
        .replace("{{subject}}", _escape(subject))
        .replace("{{week_range}}", _escape(week_range))
        .replace("{{body_html}}", body_html)
    )


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- HTTP --------------------------------------------------------------------


class ResendError(Exception):
    """Resend API returned a non-2xx response. The .message attribute holds
    the human-readable error from the JSON body when present."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Resend {status}: {message}")
        self.status = status
        self.message = message


def send(subject: str, body_md: str, week_label: str, cfg: dict) -> dict:
    """POST to /emails. Returns the parsed JSON response on success.

    cfg is the [delivery] block from config.local.toml, expected keys:
        from_email, to_email
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise ResendError(0, "RESEND_API_KEY not set")
    if not cfg.get("from_email") or not cfg.get("to_email"):
        raise ResendError(0, "from_email + to_email both required in config")

    payload = {
        "from": f"Wispr Thoughts <{cfg['from_email']}>",
        "to":   [cfg["to_email"]],
        "subject": subject,
        "html": _wrap(subject, week_label, body_md),
        "text": render_plain(body_md),
    }
    # Avoid <mailto:...> for List-Unsubscribe — most clients want an HTTPS
    # endpoint or no header at all. For solo personal use, omit.

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Cloudflare in front of api.resend.com 403s Python's default
            # `Python-urllib/3.x` UA. A real-looking UA gets through.
            "User-Agent": "wispr-thoughts/1.0 (https://github.com/pvilk/wispr-thoughts)",
            "Accept": "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Resend returns JSON like {"name": "validation_error", "message": "..."}
        # Surface the full message verbatim so the user sees the actual reason
        # ("You can only send testing emails to your own email address").
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = body or e.reason or "unknown error"
        try:
            parsed = json.loads(body) if body else {}
            msg = parsed.get("message") or parsed.get("name") or body or e.reason or "unknown error"
        except Exception:
            pass
        raise ResendError(e.code, msg) from e
    except urllib.error.URLError as e:
        raise ResendError(0, f"network error: {e.reason}") from e
