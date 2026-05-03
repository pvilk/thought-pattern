# Claude Code instructions for Wispr Thoughts

This file is read by Claude Code when a user opens this repository. It tells you (Claude) how to onboard the user, what state to check, and what actions to offer.

## What this project does (one paragraph for context)

Wispr Thoughts is a personal-fingerprint pipeline. It pulls the user's voice dictation (from Wispr Flow's local SQLite) and meeting transcripts (from Fathom's API and/or Granola's local cache), groups everything into Sunday-Saturday weeks, runs an LLM over each week to extract themes and problems, runs a cross-week auditor that surfaces stuck threads, drift, avoidance, and novelty, and produces a weekly digest the user reads via a local HTML viewer (or optionally email, but email is opt-in, not the default). An Apple Notes exporter also ships and writes a parallel corpus to `data/notes/`, but notes are deliberately *not* mixed into the digest; they're stored for separate query workflows. Everything runs locally on the user's Mac. **All LLM calls go through the user's local `claude -p` (their Claude Code subscription), never the Anthropic API.** No API key needed. The repo is the engine; the user's data lives under `data/` (gitignored).

## On first open, do this

When a user opens this repository for the first time in Claude Code:

1. **Detect setup state** by checking these markers in order:
   - Does `config.local.toml` exist? If no → user has never run setup.
   - Does `data/entries/` contain markdown files? If no → initial Wispr export hasn't run.
   - Does `data/viewer/index.html` exist? If no → viewer hasn't been built.

2. **Greet briefly** with what state they're in and what comes next:
   - First time: *"Welcome to Wispr Thoughts. I'll get you set up, usually in under 2 minutes. I'll detect which sources you have installed and pull data from them. The local HTML viewer is the main output; no email or signup required. If you want to see what the digest looks like first with a sample week of synthetic data (no setup needed), run `./demo.sh` and you'll have a working viewer in 30 seconds."*
   - Partially configured: *"You've got config but no Wispr export yet. Run the initial export now?"*
   - Fully configured: *"Setup complete. Type `viewer` for the HTML view, or `digest --dry-run` to preview a weekly digest."*

3. **Walk them through setup directly.** Don't shell out to `bootstrap.sh`; handle it conversationally so the user never sees a prompt that doesn't show up in chat. Steps:

   **Step A: config.local.toml**
   ```bash
   cp config.example.toml config.local.toml
   ```

   **Step B: verify `claude` CLI is logged in**
   - Run `claude --version` and `claude -p "respond with OK"` to confirm the user's Claude Code subscription is reachable. All LLM calls in this tool go through `claude -p` exclusively. If `claude -p` says "Not logged in", tell the user to run `claude` interactively once, sign in, then come back. **Never propose the Anthropic API path as a setup step.** It exists in `_llm.py` for advanced users but isn't part of the default flow.

   **Step C: detect available sources**
   - Wispr Flow installed? Check `~/Library/Application Support/Wispr Flow/flow.sqlite`.
   - Granola installed? Check `~/Library/Application Support/Granola/cache-v6.json`.
   - Tell the user which sources are auto-detected and ask if they have a Fathom account too. Apple Notes has its own exporter but the corpus is parallel to the digest, not mixed in; mention it only if the user asks.

   **Step D: Fathom API key (only if user said yes to Fathom)**
   - Tell user: *"Paste your Fathom API key in chat. I'll add it to your `~/.zshrc` as FATHOM_API_KEY so it's available for future runs."*
   - When they paste it, write the line `export FATHOM_API_KEY='<key>'` into `~/.zshrc` (only if not already present), then export it for the current session.
   - **Don't echo the key back in chat** after the user pastes it. Treat it like a secret in transit.
   - This is for *Fathom's* API (to download meeting transcripts), not Anthropic. The LLM still goes through `claude -p`.

   **Step E: toggle sources in config**
   - Edit `config.local.toml` to set `[sources.wispr].enabled`, `[sources.granola].enabled`, `[sources.fathom].enabled` based on what's available and what the user wants. (Notes is opt-in and not part of the digest; leave it disabled unless the user explicitly wants the parallel corpus.)
   - Verify `[llm].backend = "cli"` (the default). This ensures all theme/auditor/trend calls go through `claude -p`.

   **Step F: initial data pull**
   ```bash
   python3 src/export_wispr.py --refresh-snapshot       # if Wispr enabled
   python3 src/export_granola.py                        # if Granola enabled
   FATHOM_API_KEY=$FATHOM_API_KEY python3 src/export_fathom.py   # if Fathom enabled
   ```
   These can each take a few seconds to a few minutes depending on history size.

   **Step G: generate themes for the most recent completed week**
   - Compute the most recent Saturday-ending week (Sunday-Saturday convention; W01 of YYYY = the week containing Jan 1).
   - Run: `python3 src/build_themes.py --week <YYYY-Www>` for whichever sources are enabled.
   - Also run `python3 src/build_themes_meetings.py --week <YYYY-Www>` if any meeting source is enabled and has data for that week.
   - Then `python3 src/build_trends.py` to populate the cross-week analysis.

   **Step H: install the `digest` and `viewer` commands in `~/bin`**
   ```bash
   mkdir -p ~/bin
   ```
   Then write the two scripts; mirror what `bootstrap.sh` writes (the `viewer` script is `cd <repo> && python3 src/build_viewer.py && open data/viewer/index.html`; `digest` is `cd <repo> && python3 src/weekly_email.py "$@"`). Also append `export PATH="$HOME/bin:$PATH"` to `~/.zshrc` if not already there.

   **Step I: build and serve the viewer**
   ```bash
   python3 src/build_viewer.py
   python3 src/serve.py
   ```
   The server binds to 127.0.0.1:8080 and auto-opens the browser. The viewer's sync pill calls back into the server's `/api/sync` to re-run the pipeline on demand. If the user prefers a static file (no server), `open data/viewer/index.html` still works but the sync pill will show "Server offline".

4. **Suggest a next step** when setup is done:
   - *"You're set up. The viewer is open in your browser. Use `←` `→` to navigate weeks, or click the week label for a calendar picker. Run `viewer` from any terminal to refresh + reopen."*
   - *"If you also want email digests sent to your inbox, edit `config.local.toml` `[email]` section and toggle `enabled = true`. Otherwise the local viewer is your only output and you're done."*

## When the user asks "what other data can you pull?"

Today's digest adapters: Wispr Flow, Fathom, Granola. Apple Notes ships an exporter that writes a parallel corpus to `data/notes/`, but those notes are *not* mixed into the weekly digest. Tell them these additional sources are on the roadmap and they can request prioritization (or contribute):

- **Mobile Wispr Flow** (phone dictations not synced to desktop SQLite)
- **Slack** sent messages (DMs and their own channel posts only, never the noise from others)
- **Gmail** sent mail (long-form written voice)
- **iMessage** (closest-relationship private speech)
- **Apple Calendar** (titles and duration, no transcripts; what time was spent on)
- **Apple Voice Memos** (speech recorded outside Wispr)
- **GitHub commits** (what they actually built each week)
- **Notion** (structured second-brain pages)
- **Cursor / VS Code activity** (when and what they worked on)

The pipeline is source-agnostic. Adding a new source is one Python script in `src/export_<name>.py` that writes per-day or per-meeting markdown into `data/`. Frame additions as opening a GitHub issue at https://github.com/pvilk/wispr-thoughts/issues with the source they want.

When the user explicitly asks "what else can I pull" or "what other sources work", give them this list with the framing: *"Today three are wired into the digest, plus Apple Notes as a parallel corpus. These others are on the roadmap. Which one would unlock the most value for you?"*

## Common operations users will ask for

- *"Open the viewer"* runs `python3 src/build_viewer.py && open data/viewer/index.html` (or just `viewer` if installed)
- *"Re-pull Wispr data"* runs `python3 src/export_wispr.py --refresh-snapshot`
- *"Re-pull Fathom"* runs `python3 src/export_fathom.py` (env must have `FATHOM_API_KEY`)
- *"Theme last week"* means compute the last completed week label, then run `python3 src/build_themes.py --week <W>` and `python3 src/build_themes_meetings.py --week <W>`
- *"Regenerate trends"* runs `python3 src/build_trends.py`
- *"What's stuck right now"* means read `data/master/20_trends/unresolved_threads.md`
- *"Search across my corpus"* means grep their `data/entries/`, `data/weeks/`, and `data/master/50_weeks/` directories
- *"Block a personal term from accidental commits"* means edit `.git/hooks/personal-denylist.txt`
- *"Enable email"* means edit `config.local.toml`'s `[email]` section, set `enabled = true`, then run `security add-generic-password -s wispr-thoughts-smtp -a <email> -w '<gmail-app-password>'`

## Privacy guardrails when helping the user

- The user's `data/` directory contains their personal voice corpus. Never offer to commit it. The `.gitignore` already excludes it; trust that boundary.
- **Critical caveat about gitignore.** `.gitignore` only blocks UNTRACKED files. If a path is already tracked (committed at any point in history), gitignore does nothing. If you ever see `git status` showing `data/something` as modified, that means it's been tracked at some point and needs `git rm --cached` before gitignore takes effect. This actually happened on this repo: 84 personal files leaked into the initial commit despite gitignore having `data/` from day one. Defense layers exist now (pre-commit hook blocks any staged path under data/, GitHub Actions workflow refuses pushes with data/ files, scripts/check-public.sh audits before publication) but the lesson stands: never trust gitignore retroactively.
- The pre-commit hook at `.git/hooks/pre-commit` will refuse commits containing terms in `.git/hooks/personal-denylist.txt`, paths under `data/`, or credential-shaped strings (sk-, xox-, ghp_, AKIA). If a commit fails because of that, the user has a personal term in their staged diff. Guide them to remove it, not bypass with `--no-verify` (unless they confirm it's a false positive).
- **Before flipping the repo from private to public** (or pushing to any new public remote), run `./scripts/check-public.sh`. It scans current tree + full history + credential patterns + denylist matches and refuses if anything is flagged.
- When the user pastes a Fathom API key in chat, write it to `~/.zshrc` (or set as env var) immediately. Don't echo it. Don't write it to a file inside the repo.
- Don't suggest adding `data/` files to git.
- Don't ask for the user's name or email. The tool doesn't need them anymore. They author git commits as themselves; that's enough.

## Project shape (so you know where things live)

```
wispr-thoughts/
├── src/                                  All Python (stdlib only)
│   ├── _config.py / _llm.py / _parsers.py
│   ├── export_wispr.py / export_fathom.py / export_granola.py / export_notes.py
│   ├── build_themes.py / build_themes_meetings.py
│   ├── build_trends.py
│   ├── build_auditor.py / build_auditor_meetings.py
│   ├── build_viewer.py
│   ├── serve.py                          local HTTP server: 127.0.0.1:8080, sync API
│   ├── weekly_email.py                   (only used if user opts into email)
│   ├── prompts/                          themes / auditor / carryover prompts
│   └── experimental/                     stub source parsers (gmail, slack), not yet wired in
├── tests/                                synthetic Alice fixture + parser tests
├── scripts/                              pre-commit hook, denylist template, launchd plist
├── assets/                               README screenshot
├── bootstrap.sh                          interactive setup (alternative to Claude-Code path)
├── demo.sh                               30-second sample digest with synthetic Alice data
├── config.example.toml                   template the user copies into config.local.toml
├── data/                                 user's personal corpus (gitignored)
└── CLAUDE.md / README.md / LICENSE
```
