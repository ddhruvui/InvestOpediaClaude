#!/usr/bin/env bash
# Push this repo to GitHub, and publish app/backend and app/frontend as their own
# repos (what Vercel and Render deploy from) via git subtree split.
#
#   scripts/publish_repos.sh            # push main + both subtrees
#   ONLY=be scripts/publish_repos.sh    # just the backend subtree (or fe / main)
#
# The main repo is the source of truth: edit app/ here, commit, run this. Never
# commit directly in the BE/FE repos — the next subtree push would be rejected as
# non-fast-forward.
set -euo pipefail
cd "$(dirname "$0")/.."

MAIN_URL="${MAIN_URL:-https://github.com/ddhruvui/InvestOpediaClaude.git}"
BE_URL="${BE_URL:-https://github.com/ddhruvui/InvestOpediaClaudeBE.git}"
FE_URL="${FE_URL:-https://github.com/ddhruvui/InvestOpediaClaudeFE.git}"
BRANCH="${BRANCH:-main}"
ONLY="${ONLY:-all}"

ensure_remote() { git remote get-url "$1" >/dev/null 2>&1 || git remote add "$1" "$2"; }
ensure_remote origin "$MAIN_URL"
ensure_remote be "$BE_URL"
ensure_remote fe "$FE_URL"

# subtree split works from HEAD, so uncommitted app/ edits would be silently left out
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit first" >&2; exit 1
fi
# belt and braces: no .env may ever be tracked (root .gitignore already blocks it)
if git ls-files | grep -qE '(^|/)\.env$'; then
  echo "FATAL: a .env file is tracked — remove it from the index before pushing" >&2; exit 1
fi

case "$ONLY" in
  all|main) echo ">> main -> $MAIN_URL"; git push origin "HEAD:$BRANCH" ;;
esac
case "$ONLY" in
  all|be) echo ">> app/backend -> $BE_URL"; git subtree push --prefix=app/backend be "$BRANCH" ;;
esac
case "$ONLY" in
  all|fe) echo ">> app/frontend -> $FE_URL"; git subtree push --prefix=app/frontend fe "$BRANCH" ;;
esac
echo "done"
