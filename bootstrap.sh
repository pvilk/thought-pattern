#!/bin/bash
# Wispr Thoughts interactive setup. Run once after `git clone`.
#
# Bootstrap is intentionally minimal: it gets you to a working local viewer.
# Everything beyond that — Fathom API key, auto-sync schedule, source toggles,
# backfilling old weeks — is configured by clicking the pill in the running
# viewer. No need to edit config.local.toml or re-run this script.
#
# What this does:
#   1. Verify Python 3.11+, claude CLI, claude-p auth
#   2. Copy config.example.toml → config.local.toml (if absent)
#   3. Install the privacy pre-commit hook (if .git/hooks exists)
#   4. Detect Wispr Flow / Granola and run the initial export
#   5. Build the local viewer + install `digest` and `viewer` shell commands
#   6. Start the local server and open the browser
#
# After step 6, finish setup in the panel (top-right pill in the viewer).

set -e

cd "$(dirname "$0")"
ROOT="$(pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
warn() { printf "\033[33m! %s\033[0m\n" "$1"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$1"; }

bold "Wispr Thoughts setup"
echo

# --- 1. Prerequisites -------------------------------------------------------

bold "[1/5] Checking prerequisites"

if ! command -v python3 >/dev/null; then
    warn "python3 not found. Install Python 3.11 or later first."
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"; then
    warn "Python 3.11+ required (tomllib). You have $(python3 --version)."
    exit 1
fi
ok "python3 $(python3 --version | cut -d' ' -f2)"

if ! command -v claude >/dev/null; then
    warn "claude CLI not found. Install Claude Code at https://claude.ai/code first."
    warn "All LLM analysis runs through 'claude -p', which is required."
    exit 1
fi

echo "Verifying claude -p auth (one second)..."
if claude_test=$(echo "Reply with the literal word OK." | claude -p --output-format text 2>&1); then
    if echo "$claude_test" | grep -iE "not logged in|please run /login" >/dev/null; then
        warn "claude -p says you're not logged in."
        warn "Run 'claude' interactively once, sign in, then re-run ./bootstrap.sh"
        exit 1
    fi
    ok "claude -p ready"
else
    warn "claude -p test call failed: $claude_test"
    warn "Run 'claude' interactively once, then re-run ./bootstrap.sh"
    exit 1
fi
echo

# --- 2. Config + privacy hook -----------------------------------------------

bold "[2/5] Configure"

if [ ! -f config.local.toml ]; then
    cp config.example.toml config.local.toml
    ok "Created config.local.toml from template"
else
    ok "config.local.toml already exists, leaving it alone"
fi

if [ -d .git/hooks ] && [ -f scripts/pre-commit ]; then
    cp scripts/pre-commit .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
    DENYLIST=".git/hooks/personal-denylist.txt"
    if [ ! -f "$DENYLIST" ]; then
        cp scripts/personal-denylist.example.txt "$DENYLIST"
    fi
    ok "Privacy pre-commit hook installed"
fi
echo

# --- 3. Source detection + initial export -----------------------------------

bold "[3/5] Pull initial data"

WISPR_DB="$HOME/Library/Application Support/Wispr Flow/flow.sqlite"
GRANOLA_DIR="$HOME/Library/Application Support/Granola"

if [ -f "$WISPR_DB" ]; then
    ok "Wispr Flow detected; running initial dictation export"
    python3 src/export_wispr.py --refresh-snapshot
else
    warn "Wispr Flow not installed (skipping dictation; install at https://wisprflow.ai)"
fi

if [ -d "$GRANOLA_DIR" ]; then
    ok "Granola detected; pulling cached meetings"
    python3 src/export_granola.py || warn "Granola export had issues; continuing"
else
    warn "Granola not installed (skipping; install at https://granola.ai)"
fi

# Fathom is deferred to the panel — pasting an API key is one click after
# the viewer opens. Trying to ask for it here means another secret prompt
# in the terminal, plus an awkward two-touch setup.
echo
echo "Fathom (meeting transcripts) is set up in the panel later."
echo "Email digests are off by default; toggle in panel if you want them."
echo

# --- 4. Build viewer + install shell commands -------------------------------

bold "[4/5] Build viewer"

python3 src/build_viewer.py

mkdir -p "$HOME/bin"
cat > "$HOME/bin/digest" <<EOF
#!/bin/bash
[ -f "\$HOME/.zprofile" ] && source "\$HOME/.zprofile" >/dev/null 2>&1
[ -f "\$HOME/.zshrc" ] && source "\$HOME/.zshrc" >/dev/null 2>&1
export PATH="\$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:\$PATH"
cd "$ROOT" || exit 1
exec python3 src/weekly_email.py "\$@"
EOF
chmod +x "$HOME/bin/digest"

cat > "$HOME/bin/viewer" <<EOF
#!/bin/bash
# Builds the latest viewer and starts the local server on 127.0.0.1.
# Ctrl-C to stop. The pill in the viewer talks to this server.
[ -f "\$HOME/.zprofile" ] && source "\$HOME/.zprofile" >/dev/null 2>&1
[ -f "\$HOME/.zshrc" ] && source "\$HOME/.zshrc" >/dev/null 2>&1
export PATH="\$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:\$PATH"
cd "$ROOT" || exit 1
python3 src/build_viewer.py >/dev/null
exec python3 src/serve.py "\$@"
EOF
chmod +x "$HOME/bin/viewer"

ok "Installed \$HOME/bin/{digest,viewer}"

if ! echo ":$PATH:" | grep -q ":$HOME/bin:"; then
    if ! grep -q '$HOME/bin' "$HOME/.zshrc" 2>/dev/null; then
        echo "" >> "$HOME/.zshrc"
        echo '# Wispr Thoughts' >> "$HOME/.zshrc"
        echo 'export PATH="$HOME/bin:$PATH"' >> "$HOME/.zshrc"
        ok "Added \$HOME/bin to PATH (open a new terminal to activate)"
    fi
fi
echo

# --- 5. Launch the viewer ---------------------------------------------------

bold "[5/5] Open the viewer"
echo
cat <<'EOF'
What happens next:

  - Your browser will open http://127.0.0.1:8080
  - Click the pill in the top-right corner to finish setup:
      * Toggle "Run weekly automatically" on for Sunday 8am auto-sync
      * If you use Fathom, paste your API key in the Sources card
      * Backfill historical weeks (button appears when there are unthemed weeks)
  - From any new terminal, type 'viewer' to start the server again
  - 'digest' runs the pipeline once (same as auto-sync but on demand)

Setup is complete. Ctrl-C in this terminal stops the server.
EOF
echo

exec python3 src/serve.py 2>/dev/null || true
