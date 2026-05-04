# Claude Code instructions for Wispr Thoughts

This file is read by Claude Code when a user opens this repository. It tells you (Claude) how to onboard the user, what state to check, and what actions to offer.

## What this project does (one paragraph for context)

Wispr Thoughts is a personal-fingerprint pipeline. It pulls the user's voice dictation (from Wispr Flow's local SQLite) and meeting transcripts (from Fathom's API and/or Granola's local cache), groups everything into Sunday-Saturday weeks, runs an LLM over each week to extract themes and problems, runs a cross-week auditor that surfaces stuck threads, drift, avoidance, and novelty, and produces a weekly digest the user reads via a local HTML viewer at `http://127.0.0.1:8080`. An Apple Notes exporter also ships and writes a parallel corpus to `data/notes/`, but notes are deliberately *not* mixed into the digest. Everything runs locally on the user's Mac. **All LLM calls go through the user's local `claude -p` (their Claude Code subscription), never the Anthropic API.** No API key needed. The repo is the engine; the user's data lives under `data/` (gitignored).

## The settings panel (most setup happens here, not in chat)

The viewer has a clickable pill in the top-right corner that opens a Settings drawer. This is where the user manages:

- **Auto-sync**: a toggle that installs a launchd job (`~/Library/LaunchAgents/io.wisprthoughts.weekly.plist`) running every Sunday 8:00 AM. The schedule is fixed; only the on/off toggle is user-controlled.
- **Sources**: per-source toggles for Wispr Flow / Fathom / Granola. Each row shows DETECTED / not found and writes back to `config.local.toml`.
- **Fathom API key**: when Fathom is enabled but no key is set, an inline input appears in the Sources card. Pasting writes `export FATHOM_API_KEY='...'` to `~/.zshrc` (which the cron sources via `zsh -lc`) and updates the live server's env.
- **Backfill**: a row showing the count of unthemed past weeks with a button to theme them all (`build_themes.py` per week, then `build_trends.py`, then `build_viewer.py`).

**Always defer to the panel for these settings rather than editing `config.local.toml` manually.** The TOML editor path still works, but the panel is the canonical UX for new users.

## On first open, do this

When a user opens this repository for the first time in Claude Code:

1. **Detect setup state** by checking these markers in order:
   - Does `config.local.toml` exist? If no → user has never run setup.
   - Does `data/entries/` contain markdown files? If no → initial Wispr export hasn't run.
   - Does `data/viewer/index.html` exist? If no → viewer hasn't been built.
   - Is `serve.py` running on 127.0.0.1:8080? Check with `lsof -nP -iTCP:8080 -sTCP:LISTEN` or by curling `http://127.0.0.1:8080/api/status`.

2. **Greet briefly** with what state they're in and what comes next:
   - First time: *"Welcome to Wispr Thoughts. Two minutes to a working local viewer; everything else is configured by clicking the pill once it's open. If you want a sample first (no setup), run `./demo.sh`."*
   - Partially configured: *"You've got config but no Wispr export yet. Run the initial export now?"*
   - Fully configured: *"Setup complete. Type `viewer` to start the local server, then click the pill in the top-right to manage auto-sync, Fathom key, sources, or backfill."*

3. **Walk through setup directly.** Don't shell out to `bootstrap.sh`; do each step in chat. Steps:

   **Step A: config.local.toml**
   ```bash
   cp config.example.toml config.local.toml
   ```

   **Step B: verify `claude` CLI is logged in**
   - Run `claude --version` and `claude -p "respond with OK"` to confirm the user's Claude Code subscription is reachable. All LLM calls go through `claude -p` exclusively. If `claude -p` says "Not logged in", tell the user to run `claude` interactively once, sign in, then come back. **Never propose the Anthropic API path as a setup step.**

   **Step C: detect available sources**
   - Wispr Flow installed? Check `~/Library/Application Support/Wispr Flow/flow.sqlite`.
   - Granola installed? Check `~/Library/Application Support/Granola/cache-v6.json`.
   - Tell the user which sources are auto-detected. Don't ask about Fathom here — they'll paste the key in the panel later. Apple Notes is a parallel corpus and not surfaced in the panel; mention it only if the user asks.

   **Step D: initial Wispr/Granola export**
   ```bash
   python3 src/export_wispr.py --refresh-snapshot       # if Wispr installed
   python3 src/export_granola.py                        # if Granola installed
   ```
   Skip Fathom — it's configured in the panel via key paste, then the next sync pulls it.

   **Step E: install the `digest` and `viewer` commands in `~/bin`**
   ```bash
   mkdir -p ~/bin
   ```
   Mirror what `bootstrap.sh` writes:
   - `~/bin/viewer`: `cd <repo> && python3 src/build_viewer.py >/dev/null && exec python3 src/serve.py`
   - `~/bin/digest`: `cd <repo> && exec python3 src/weekly_email.py "$@"`
   Both should source `~/.zshrc` first so they inherit `FATHOM_API_KEY`. Append `export PATH="$HOME/bin:$PATH"` to `~/.zshrc` if not already there.

   **Step F: build viewer + start the server**
   ```bash
   python3 src/build_viewer.py
   python3 src/serve.py
   ```
   The server binds to 127.0.0.1:8080 and auto-opens the browser. The pill in the corner is the entry point to all remaining setup.

4. **Hand off to the panel** when the server is running:
   - *"You're set up. The viewer is open at http://127.0.0.1:8080. Click the pill in the top-right corner to:*
     - *Toggle 'Run weekly automatically' on (Sunday 8 AM auto-sync)*
     - *Paste your Fathom API key if you use Fathom*
     - *Toggle Wispr / Granola sources on or off*
     - *Backfill themes for any past weeks captured but not yet themed*"*

## When the user asks about setup mid-flow

- *"How do I add my Fathom key?"* → Click the pill, scroll to Sources, toggle Fathom on, paste the key in the inline input that appears, click Save. It writes to `~/.zshrc` and the cron will pick it up.
- *"How do I turn on auto-sync?"* → Click the pill, flip the "Run weekly automatically" toggle. Schedule is fixed at Sunday 8 AM.
- *"How do I theme my old weeks?"* → Click the pill. If there are unthemed past weeks, the Auto-sync card shows a "Backfill N" button. Click it; modal streams the log.
- *"How do I send myself email?"* → Email delivery is intentionally not in the panel. If the user really wants it, they edit `config.local.toml` `[email]` section directly and run `security add-generic-password -s <service> -a <email> -w '<gmail-app-password>'`. The local viewer is the primary output.

## When the user asks "what other data can you pull?"

Today's digest adapters: Wispr Flow, Fathom, Granola. Apple Notes ships an exporter that writes a parallel corpus to `data/notes/`, but those notes are *not* mixed into the weekly digest. These are on the roadmap:

- **Mobile Wispr Flow** (phone dictations not synced to desktop SQLite)
- **Slack** sent messages (DMs and their own channel posts only)
- **Gmail** sent mail (long-form written voice)
- **iMessage** (closest-relationship private speech)
- **Apple Calendar** (titles and durations only)
- **Apple Voice Memos** (speech recorded outside Wispr)
- **GitHub commits** (what they actually built each week)
- **Notion** (structured second-brain pages)
- **Cursor / VS Code activity** (when and what they worked on)

The pipeline is source-agnostic. Adding a new source is one Python script in `src/export_<name>.py` that writes per-day or per-meeting markdown into `data/`. Frame additions as opening a GitHub issue at https://github.com/pvilk/wispr-thoughts/issues with the source they want.

## Common operations users will ask for

- *"Open the viewer"* — start the server: `python3 src/serve.py` (or `viewer` if installed). The viewer rebuilds first then auto-opens the browser.
- *"Sync now"* — auto-sync runs Sunday 8 AM via launchd; for an immediate run, the user can run `digest` from a terminal or wait for the cron. There's no manual "sync now" button in the panel anymore — auto-sync is the canonical path.
- *"Re-pull Wispr data"* — `python3 src/export_wispr.py --refresh-snapshot`
- *"Re-pull Fathom"* — `python3 src/export_fathom.py` (env must have `FATHOM_API_KEY`; loaded from `~/.zshrc` automatically when the server starts)
- *"Theme last week"* — `python3 src/build_themes.py --week <YYYY-Www>` (and `build_themes_meetings.py` if any meeting source is enabled)
- *"Regenerate trends"* — `python3 src/build_trends.py`
- *"What's stuck right now"* — read `data/master/20_trends/unresolved_threads.md`
- *"Search across my corpus"* — grep `data/entries/`, `data/weeks/`, and `data/master/50_weeks/`
- *"Block a personal term from accidental commits"* — edit `.git/hooks/personal-denylist.txt`

## Privacy guardrails when helping the user

- The user's `data/` directory contains their personal voice corpus. Never offer to commit it. The `.gitignore` already excludes it; trust that boundary.
- **Critical caveat about gitignore.** `.gitignore` only blocks UNTRACKED files. If a path is already tracked (committed at any point in history), gitignore does nothing. If you ever see `git status` showing `data/something` as modified, that means it's been tracked at some point and needs `git rm --cached` before gitignore takes effect. This actually happened on this repo: 84 personal files leaked into the initial commit despite gitignore having `data/` from day one. Defense layers exist now (pre-commit hook blocks any staged path under data/, GitHub Actions workflow refuses pushes with data/ files, scripts/check-public.sh audits before publication) but the lesson stands: never trust gitignore retroactively.
- The pre-commit hook at `.git/hooks/pre-commit` will refuse commits containing terms in `.git/hooks/personal-denylist.txt`, paths under `data/`, or credential-shaped strings (sk-, xox-, ghp_, AKIA). If a commit fails, the user has a personal term in their staged diff. Guide them to remove it, not bypass with `--no-verify` (unless they confirm it's a false positive).
- **Before flipping the repo from private to public** (or pushing to any new public remote), run `./scripts/check-public.sh`. It scans current tree + full history + credential patterns + denylist matches and refuses if anything is flagged.
- When the user pastes a Fathom API key in chat (instead of in the panel), write it to `~/.zshrc` immediately. Don't echo it. Don't write it to a file inside the repo. If the server is running, prefer directing them to the panel instead — it does the same write and updates the live process env.
- Don't suggest adding `data/` files to git.
- Don't ask for the user's name or email. The tool doesn't need them.

## Project shape (so you know where things live)

```
wispr-thoughts/
├── src/                                  All Python (stdlib only)
│   ├── _config.py / _llm.py / _parsers.py
│   ├── _settings.py                      TOML write helper for the panel
│   ├── _schedule.py                      launchd plist render + install/remove
│   ├── export_wispr.py / export_fathom.py / export_granola.py / export_notes.py
│   ├── build_themes.py / build_themes_meetings.py
│   ├── build_trends.py
│   ├── build_auditor.py / build_auditor_meetings.py
│   ├── build_viewer.py
│   ├── serve.py                          local HTTP server: 127.0.0.1:8080, settings + sync APIs
│   ├── weekly_email.py                   pipeline orchestrator (run by cron + manual)
│   ├── prompts/                          themes / auditor / carryover prompts
│   └── experimental/                     stub source parsers (gmail, slack), not yet wired in
├── tests/                                synthetic Alice fixture + parser tests
├── scripts/                              pre-commit hook, denylist template, launchd plist template
├── assets/                               README screenshot
├── bootstrap.sh                          minimal setup: install + first export + start server
├── demo.sh                               30-second sample digest with synthetic Alice data
├── config.example.toml                   template the user copies into config.local.toml
├── data/                                 user's personal corpus (gitignored)
└── CLAUDE.md / README.md / LICENSE
```
