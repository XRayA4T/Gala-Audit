#!/usr/bin/env bash
# push-gala-audit-repo.sh - commit this folder and push it to github.com/XRayA4T/Gala-Audit as XRayA4T,
# then switch gh back to the work account (RaymondBarbourFFF), even if something fails.
# The gala's files (data/), the key and the portal's database are never committed (see .gitignore);
# the script stops if any of them would be.
#
#     bash push-gala-audit-repo.sh ["commit message"]
set -euo pipefail

PUBLIC=XRayA4T
WORK=RaymondBarbourFFF
URL="https://github.com/XRayA4T/Gala-Audit.git"
cd "$(dirname "${BASH_SOURCE[0]}")"
MSG="${1:-Update the Gala Audit Portal}"

gh auth status 2>&1 | grep -q "account $PUBLIC" || {
  echo "gh has no $PUBLIC login yet. Run once (sign in as $PUBLIC in the browser):"
  echo "  gh auth login --hostname github.com --git-protocol https --web"
  exit 1
}

FIRST=0
if [ ! -d .git ]; then
  git init -q -b main
  FIRST=1
fi
git remote get-url origin >/dev/null 2>&1 && git remote set-url origin "$URL" || git remote add origin "$URL"

git add -A
# Safety: nothing from the gala, the key or local settings may be committed.
BAD="$(git diff --cached --name-only | grep -E '^(data/(.+/)?[^/]+|simulation/|plugin-update/|.*\.(hy3|zip|pdf|cl2|ev3|hyv)$|\.portal/|\.mcp\.json$|\.claude/settings\.local\.json$)' | grep -vx 'data/README.txt' || true)"
if [ -n "$BAD" ]; then
  echo "Refusing to commit these files (gala data or private settings):"; echo "$BAD"
  git reset -q; exit 1
fi
if ! git diff --cached --quiet; then
  [ "$FIRST" = 1 ] && [ $# -eq 0 ] && MSG="Gala Audit Portal: first pass"
  git commit -q -m "$MSG" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  echo "Committed: $(git log --oneline -1)"
else
  echo "Nothing new to commit."
fi

back_to_work() { gh auth switch --hostname github.com --user "$WORK" >/dev/null 2>&1 && echo "gh is back on $WORK."; }
trap back_to_work EXIT

echo "Switching gh to $PUBLIC ..."
gh auth switch --hostname github.com --user "$PUBLIC"
gh auth setup-git

if git ls-remote --exit-code --heads origin main >/dev/null 2>&1; then
  git pull -q --rebase origin main     # e.g. a README or licence made on GitHub
fi
git push -u origin main
echo "Pushed: $(git log --oneline -1)"
