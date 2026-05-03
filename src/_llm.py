"""Wispr Thoughts LLM dispatcher.

Two backends, selected by config:
  cli  : `claude -p` via your Claude Code subscription. Free marginal cost.
         Works in interactive shells; not in launchd's stripped env.
  api  : Anthropic Messages API via stdlib urllib. Reads key from keychain.
         Required for cron / scheduled execution paths.
  auto : try cli first, fall back to api on auth failure.

Stdlib only. No SDK dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from _config import load as load_config

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 600
CLI_NOT_LOGGED_IN_MARKERS = ("not logged in", "please run /login")


def _cfg():
    cfg = load_config()
    llm = cfg.get("llm", {})
    return {
        "backend":  os.environ.get("WISPRTHOUGHTS_LLM_BACKEND", llm.get("backend", "auto")).lower(),
        "model":    llm.get("model", "claude-sonnet-4-6"),
        "service":  llm.get("api_keychain_service", "wispr-thoughts-anthropic-api"),
    }


def call_claude_cli(prompt: str, model: str | None = None) -> str:
    cfg = _cfg()
    model = model or cfg["model"]
    cmd = ["claude", "-p", "--model", model, "--output-format", "text"]
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"claude CLI not found: {e}") from e
    out, err = result.stdout, result.stderr
    if any(m in (out + err).lower() for m in CLI_NOT_LOGGED_IN_MARKERS):
        raise RuntimeError("claude -p reported 'Not logged in'.")
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}: "
            f"{(err.strip() or out.strip())[:500]}"
        )
    return out.strip()


def _get_api_key() -> str:
    cfg = _cfg()
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env.strip()
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", cfg["service"], "-w"],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit(
            "Anthropic API key not found.\n"
            f"  security add-generic-password -s {cfg['service']} -a <email> -w '<key>'\n"
            "Or set ANTHROPIC_API_KEY in the environment."
        )
    return result.stdout.strip()


def call_anthropic_api(prompt: str, model: str | None = None, max_tokens: int = 8000) -> str:
    cfg = _cfg()
    model = model or cfg["model"]
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {
        "x-api-key": _get_api_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_err: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(ANTHROPIC_ENDPOINT, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read())
            return payload["content"][0]["text"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            last_err = RuntimeError(f"HTTP {e.code}: {err_body}")
            if e.code < 500 and e.code != 429:
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        if attempt < RETRY_ATTEMPTS:
            print(f"  API attempt {attempt} failed; retrying in {RETRY_BACKOFF_SECONDS}s",
                  file=sys.stderr)
            time.sleep(RETRY_BACKOFF_SECONDS)
    raise RuntimeError(f"API call failed after {RETRY_ATTEMPTS} attempts: {last_err}")


def call_anthropic(prompt: str, model: str | None = None, max_tokens: int = 8000) -> str:
    """Top-level dispatch. Reads backend from config / env."""
    backend = _cfg()["backend"]
    if backend == "api":
        return call_anthropic_api(prompt, model, max_tokens)
    if backend == "cli":
        return call_claude_cli(prompt, model)
    # auto: try cli first, fall back to api
    try:
        return call_claude_cli(prompt, model)
    except RuntimeError as cli_err:
        print(f"  CLI failed ({cli_err}); falling back to API.", file=sys.stderr)
        return call_anthropic_api(prompt, model, max_tokens)
