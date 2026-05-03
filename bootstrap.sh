#!/bin/bash
# Wispr Thoughts interactive setup. Run once after `git clone`.
#
# This script:
#   1. Checks prerequisites (Python 3.11+, claude CLI, macOS keychain)
#   2. Copies config.example.toml to config.local.toml
#   3. Detects which sources you have installed and asks which to enable
#   4. (Optional) Asks for a Fathom API key and runs an initial export
#   5. Runs the initial Wispr / Granola exports
#   6. Generates themes for the most recent completed week
#   7. Installs `digest` and `viewer` commands in ~/bin
#   8. Opens the local HTML viewer in your browser
#
# Email is OFF by default; the local viewer is your primary output.
# Re-running is safe; existing config + data are preserved.

set -e

cd "$(dirname "$0")"
ROOT="$(pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
warn() { printf "\033[33m! %s\033[0m\n" "$1"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$1"; }
ask()  { printf "\033[36m? %s\033[0m " "$1"; }
yesno() {
    ask "$1 (y/N)"
    read -r ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

bold "Wispr Thoughts setup"
echo

# --- 1. Prerequisites ---------------------------------------------------------

bold "[1/6] Checking prerequisites"

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
    warn "All LLM analysis in this tool runs through 'claude -p', which is required."
    exit 1
fi

# Pre-flight: verify claude is authenticated with a simple no-op prompt.
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

# --- 2. Config file -----------------------------------------------------------

bold "[2/6] Local configuration"

if [ ! -f config.local.toml ]; then
    cp config.example.toml config.local.toml
    ok "Created config.local.toml from template"
fi
echo

# --- 3. Source detection + selection ------------------------------------------

bold "[3/6] Detect data sources"

WISPR_DB="$HOME/Library/Application Support/Wispr Flow/flow.sqlite"
GRANOLA_DIR="$HOME/Library/Application Support/Granola"

if [ -f "$WISPR_DB" ]; then
    ok "Wispr Flow detected (will pull dictation history)"
    WISPR_OK=1
else
    warn "Wispr Flow not installed; skipping (install at https://wisprflow.ai if you want voice dictation)"
    WISPR_OK=0
fi

if [ -d "$GRANOLA_DIR" ]; then
    ok "Granola detected (will pull cached meetings)"
    GRANOLA_OK=1
else
    warn "Granola not installed; skipping (install at https://granola.ai)"
    GRANOLA_OK=0
fi

NOTES_OK=0

FATHOM_KEY=""
if yesno "Do you have a Fathom account and API key?"; then
    ask "Paste your Fathom API key (it'll be added to ~/.zshrc as FATHOM_API_KEY):"
    read -rs FATHOM_KEY
    echo
    if [ -n "$FATHOM_KEY" ]; then
        if ! grep -q "FATHOM_API_KEY" "$HOME/.zshrc" 2>/dev/null; then
            echo "" >> "$HOME/.zshrc"
            echo "# Wispr Thoughts Fathom API key" >> "$HOME/.zshrc"
            echo "export FATHOM_API_KEY='$FATHOM_KEY'" >> "$HOME/.zshrc"
            ok "FATHOM_API_KEY added to ~/.zshrc"
        else
            warn "~/.zshrc already has a FATHOM_API_KEY entry; not overwriting"
        fi
        export FATHOM_API_KEY="$FATHOM_KEY"
    fi
fi
echo

# --- 4. Pre-commit privacy hook -----------------------------------------------

if [ -d .git/hooks ] && [ -f scripts/pre-commit ]; then
    cp scripts/pre-commit .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
    DENYLIST=".git/hooks/personal-denylist.txt"
    if [ ! -f "$DENYLIST" ]; then
        cp scripts/personal-denylist.example.txt "$DENYLIST"
        ok "Privacy hook installed. Edit $DENYLIST with terms you want blocked from commits."
    fi
fi

# --- 5. Initial data pull -----------------------------------------------------

bold "[4/6] Pull initial data"

if [ "$WISPR_OK" = "1" ]; then
    python3 src/export_wispr.py --refresh-snapshot
    ok "Wispr export complete"
fi

if [ "$GRANOLA_OK" = "1" ]; then
    python3 src/export_granola.py || warn "Granola export had issues; continuing"
fi

if [ -n "$FATHOM_KEY" ]; then
    python3 src/export_fathom.py || warn "Fathom export had issues; continuing"
fi
echo

# --- 5. Generate themes for the most recent completed week --------------------

bold "[5/6] Generate first themes (so the viewer isn't empty)"

# Compute most recent Saturday-ending week label
LAST_WEEK=$(python3 -c "
import datetime as dt
d = dt.date.today()
days_back = (d.weekday() + 2) % 7
if days_back == 0: days_back = 7
sat = d - dt.timedelta(days=days_back)
days_since_sun = (sat.weekday() + 1) % 7
sun = sat - dt.timedelta(days=days_since_sun)
saturday = sun + dt.timedelta(days=6)
y = saturday.year
jan1 = dt.date(y, 1, 1)
offset = (jan1.weekday() + 1) % 7
w01_sun = jan1 - dt.timedelta(days=offset)
w = ((sun - w01_sun).days // 7) + 1
print(f'{y}-W{w:02d}')
")
echo "  target: $LAST_WEEK"

if [ "$WISPR_OK" = "1" ]; then
    python3 src/build_themes.py --week "$LAST_WEEK" 2>&1 | tail -2 || warn "Wispr theming had issues; continuing"
fi
if [ "$GRANOLA_OK" = "1" ] || [ -n "$FATHOM_KEY" ]; then
    python3 src/build_themes_meetings.py --week "$LAST_WEEK" 2>&1 | tail -2 || warn "Meeting theming had issues; continuing"
fi
echo

# Optional: backfill ALL historical weeks
if yesno "Backfill themes for ALL your historical weeks too? (~10-25 min, free with Claude Code subscription)"; then
    bold "Running full backfill; this takes a while"
    if [ "$WISPR_OK" = "1" ]; then
        for week_file in data/weeks/*.md; do
            [ -f "$week_file" ] || continue
            week=$(basename "$week_file" .md)
            python3 src/build_themes.py --week "$week" 2>&1 | tail -1
        done
    fi
    if [ "$GRANOLA_OK" = "1" ] || [ -n "$FATHOM_KEY" ]; then
        python3 src/build_themes_meetings.py --all-missing 2>&1 | while read -r week; do
            python3 src/build_themes_meetings.py --week "$week" 2>&1 | tail -1
        done
    fi
fi
echo

# --- 6. Install shell commands + open viewer ----------------------------------

bold "[5/6] Install 'digest' and 'viewer' commands"

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
# Ctrl-C to stop. The sync pill in the viewer talks to this server.
[ -f "\$HOME/.zprofile" ] && source "\$HOME/.zprofile" >/dev/null 2>&1
[ -f "\$HOME/.zshrc" ] && source "\$HOME/.zshrc" >/dev/null 2>&1
export PATH="\$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:\$PATH"
cd "$ROOT" || exit 1
python3 src/build_viewer.py >/dev/null
exec python3 src/serve.py "\$@"
EOF
chmod +x "$HOME/bin/viewer"

ok "Installed $HOME/bin/{digest,viewer}"

if ! echo ":$PATH:" | grep -q ":$HOME/bin:"; then
    SHELL_RC="$HOME/.zshrc"
    if ! grep -q '$HOME/bin' "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo '# Wispr Thoughts' >> "$SHELL_RC"
        echo 'export PATH="$HOME/bin:$PATH"' >> "$SHELL_RC"
        ok "Added \$HOME/bin to PATH (open a new terminal to activate)"
    fi
fi
echo

bold "[6/6] Build the local viewer"

python3 src/build_viewer.py
echo
bold "Setup complete. Starting the local viewer server."
echo
echo "From any new terminal type 'viewer' to start the server again."
echo "It serves at http://127.0.0.1:8080 and the sync pill in the page"
echo "calls back to it when you click. Ctrl-C in this terminal stops it."
echo
echo "Want email digests too? Edit config.local.toml ([email] section) later."
echo

exec python3 src/serve.py 2>/dev/null || true
