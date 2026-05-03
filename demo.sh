#!/bin/bash
# Wispr Thoughts: 30-second demo with synthetic Alice data.
#
# Stages a pre-themed sample week into data/weeks/ and builds the local viewer
# so you can see what the digest looks like without setting up Wispr Flow,
# Fathom, Granola, or even a Claude Code subscription. Run this first; run
# bootstrap.sh second when you're ready to point the pipeline at your own data.

set -e

cd "$(dirname "$0")"
ROOT="$(pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m%s\033[0m\n" "$1"; }
warn() { printf "\033[33m%s\033[0m\n" "$1"; }

bold "Wispr Thoughts demo"
echo

if ! command -v python3 >/dev/null; then
    warn "python3 not found. Install Python 3.11+ first."
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"; then
    warn "Python 3.11+ required (tomllib). You have $(python3 --version)."
    exit 1
fi

# Use a minimal config so build_viewer.py can resolve paths. If the user has
# already set up config.local.toml for real sources we leave it alone.
if [ ! -f config.local.toml ]; then
    cp config.example.toml config.local.toml
    ok "Created config.local.toml from template"
fi

# Stage the Alice fixture as a real Wispr-themed week the viewer will pick up.
mkdir -p data/weeks
cp tests/fixtures/2026-W04-alice.md data/weeks/2026-W04.md
ok "Staged synthetic week 2026-W04 (Alice fixture)"

python3 src/build_viewer.py
echo

bold "Done. Opening the viewer."
echo
echo "You should see one week (2026-W04, Jan 18 to Jan 24) with three themes"
echo "and two problems on Alice's mind. The chevrons in the header don't move"
echo "yet because there's only one week in the demo corpus."
echo
echo "Ready for your own data? Run ./bootstrap.sh to wire up Wispr Flow,"
echo "Fathom, and Granola."
echo

open data/viewer/index.html 2>/dev/null || true
