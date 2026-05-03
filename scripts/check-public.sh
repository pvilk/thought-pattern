#!/bin/bash
# Wispr Thoughts pre-publication audit.
#
# Run this before flipping the GitHub repo from private to public, or before
# pushing to any new public remote. It scans both the current tree AND the
# entire git history for paths and content that should never be visible.
#
# Exits 0 if clean, 1 if findings. Findings are written to stderr.
#
# Usage:
#   ./scripts/check-public.sh
#   ./scripts/check-public.sh --quick    # current HEAD only, skip history scan

set -e

cd "$(dirname "$0")/.."

QUICK=0
[ "$1" = "--quick" ] && QUICK=1

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
red()  { printf "\033[31m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m%s\033[0m\n" "$1"; }

bold "▸ Wispr Thoughts pre-publication audit"
echo
findings=0

# ---- check 1: data/ in current tree ---------------------------------------

bold "[1/4] Current tree under data/"
data_now=$(git ls-tree -r HEAD --name-only | grep '^data/' || true)
if [ -n "$data_now" ]; then
    red  "  FOUND: tracked files under data/ at HEAD"
    echo "$data_now" | sed 's/^/    /' | head -20
    [ "$(echo "$data_now" | wc -l)" -gt 20 ] && echo "    ... ($(echo "$data_now" | wc -l) total)"
    findings=$((findings + 1))
else
    ok   "  clean: no tracked files under data/"
fi

# ---- check 2: data/ ever committed across full history --------------------

if [ "$QUICK" = "0" ]; then
    bold "[2/4] data/ across full history"
    data_hist=$(git log --all --pretty=format: --name-only --diff-filter=A | grep '^data/' | sort -u || true)
    if [ -n "$data_hist" ]; then
        red  "  FOUND: data/ paths in historical commits"
        echo "$data_hist" | sed 's/^/    /' | head -10
        [ "$(echo "$data_hist" | wc -l)" -gt 10 ] && echo "    ... ($(echo "$data_hist" | wc -l) total)"
        echo "    purge with: git-filter-repo --invert-paths --path data/ --force"
        findings=$((findings + 1))
    else
        ok   "  clean: no data/ paths in any commit, ever"
    fi
else
    echo "[2/4] history scan: skipped (--quick)"
fi

# ---- check 3: credential patterns in current tree -------------------------

bold "[3/4] Credential patterns in current tree"
CRED='sk-(proj-|ant-|live_|test_)?[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|gh[ps]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}'
cred_now=$(git grep -E -l "$CRED" -- ':!scripts/pre-commit' ':!scripts/check-public.sh' 2>/dev/null || true)
if [ -n "$cred_now" ]; then
    red  "  FOUND: files with credential-shaped strings"
    echo "$cred_now" | sed 's/^/    /'
    findings=$((findings + 1))
else
    ok   "  clean: no credential patterns in tracked files"
fi

# ---- check 4: denylist terms in current tree ------------------------------

bold "[4/4] Personal denylist matches in current tree"
DENYLIST=".git/hooks/personal-denylist.txt"
if [ -f "$DENYLIST" ] && [ -s "$DENYLIST" ]; then
    PATTERN=$(grep -v '^#' "$DENYLIST" | grep -v '^$' | paste -sd '|' -)
    if [ -n "$PATTERN" ]; then
        deny_now=$(git grep -i -l -E "($PATTERN)" -- ':!.gitignore' ':!scripts/personal-denylist.example.txt' 2>/dev/null || true)
        if [ -n "$deny_now" ]; then
            red  "  FOUND: tracked files matching personal-denylist terms"
            echo "$deny_now" | sed 's/^/    /' | head -10
            echo "    (Some matches are likely false positives like your GitHub username in URLs.)"
            findings=$((findings + 1))
        else
            ok   "  clean: no denylist matches in tracked files"
        fi
    else
        echo "  empty denylist; skipped"
    fi
else
    echo "  no .git/hooks/personal-denylist.txt; skipped"
fi

# ---- summary --------------------------------------------------------------

echo
if [ "$findings" -eq 0 ]; then
    ok "▸ Audit clean. Safe to publish."
    exit 0
fi
red "▸ Audit found $findings categories of concern. Resolve before publishing."
echo
echo "Quick remediation paths:"
echo "  data/ in tree:     git rm --cached -r data/ && git commit -m 'untrack data/'"
echo "  data/ in history:  git-filter-repo --invert-paths --path data/ --force"
echo "  credentials:       move to keychain or env var, edit the file, recommit"
echo "  denylist matches:  inspect each file; remove personal info or accept false positive"
echo
exit 1
