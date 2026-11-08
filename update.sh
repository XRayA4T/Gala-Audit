#!/usr/bin/env bash
# update.sh - update the CGA Gala Audit Portal from GitHub, and the gala-ocr plugin.
#
#   bash update.sh            update if there is anything new; restart the portal if it was running
#   bash update.sh --check    only say whether an update is available (exit 0 = up to date, 10 = update available)
#   bash update.sh --restart  restart the portal afterwards even if it wasn't running
#   bash update.sh --restart-claude   restart Claude Code too (it otherwise keeps running, mid-event work and all;
#                             it picks up new instructions and plugin versions the next time it starts)
#
# The gala's files (data/), the key and the portal's database are never touched (they're not in git).
# Files of the portal changed by hand on this computer stop the update, so nothing is lost; it says which.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
CHECK=0; RESTART=0; KEEP=--keep-claude
for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    --restart) RESTART=1 ;;
    --restart-claude) KEEP="" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

[ -d .git ] || { warn "This copy of the portal isn't a git clone, so it can't update itself. Install it with install.sh."; exit 1; }
# Making a script runnable (chmod +x start-portal.sh, to run it as ./start-portal.sh) isn't a change to the portal.
git config core.fileMode false
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
[ "$BRANCH" = HEAD ] && BRANCH=main

echo "[$(stamp)] checking for updates"
if ! timeout 30 git fetch -q origin "$BRANCH" 2>/dev/null; then
  warn "Couldn't reach GitHub (no internet?). Carrying on with the version installed."
  exit 2
fi
BEHIND="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"
AHEAD="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
if [ "$BEHIND" = 0 ]; then
  say "Up to date ($(git log -1 --format='%h, %cd' --date=short))"
  [ "$CHECK" = 1 ] && exit 0
  # still make sure the plugin is current
  claude plugin marketplace update gala-ai-plugins >/dev/null 2>&1 || true
  claude plugin update gala-ocr@gala-ai-plugins >/dev/null 2>&1 || true
  exit 0
fi
say "$BEHIND update(s) available:"
git log --format='   %h %cd  %s' --date=short "HEAD..origin/$BRANCH" | head -20
[ "$CHECK" = 1 ] && exit 10

if [ "$AHEAD" != 0 ]; then
  warn "This copy has $AHEAD change(s) of its own that aren't on GitHub; not updating. (This is the development copy? Push instead.)"
  exit 3
fi
# .claude/settings.json is rewritten by start-portal.sh (and Claude Code) each start; it's put back and remade.
SETTINGS=.claude/settings.json
CHANGED="$(git status --porcelain --untracked-files=no | grep -v " $SETTINGS\$" || true)"
if [ -n "$CHANGED" ]; then
  warn "These portal files were changed on this computer, so the update would overwrite them; not updating:"
  echo "$CHANGED" | sed 's/^/     /'
  warn "Put them back with:  git -C \"$HERE\" checkout -- <file>   (or ask for the change to be made in the portal itself)"
  exit 4
fi

RUNNING=0
if [ -s .portal/portal.pid ] && kill -0 "$(cat .portal/portal.pid)" 2>/dev/null; then RUNNING=1; fi
if [ "$RUNNING" = 1 ]; then
  say "Stopping the portal"
  bash stop-portal.sh $KEEP >/dev/null 2>&1 || true
  sleep 1
fi

say "Updating"
git diff --quiet -- "$SETTINGS" 2>/dev/null || { mkdir -p .portal; cp "$SETTINGS" .portal/settings.json.before-update 2>/dev/null; git checkout -q -- "$SETTINGS"; }
if ! git merge -q --ff-only "origin/$BRANCH"; then
  warn "The update couldn't be applied cleanly; nothing was changed."
  if [ "$RUNNING" = 1 ]; then ARGS=(); [ -s .portal/start-args ] && eval "ARGS=($(cat .portal/start-args))"
    bash start-portal.sh --no-browser --no-update-check "${ARGS[@]}" >/dev/null 2>&1; fi
  exit 5
fi
chmod +x start-portal.sh stop-portal.sh update.sh install.sh 2>/dev/null || true
say "Now at $(git log -1 --format='%h, %cd: %s' --date=short)"

say "Updating the gala-ocr plugin"
claude plugin marketplace update gala-ai-plugins >/dev/null 2>&1 || true
claude plugin update gala-ocr@gala-ai-plugins >/dev/null 2>&1 || true

if [ "$RUNNING" = 1 ] || [ "$RESTART" = 1 ]; then
  say "Starting the portal again"
  ARGS=(); [ -s .portal/start-args ] && eval "ARGS=($(cat .portal/start-args))"   # the port, inbox and name it had
  bash start-portal.sh --no-browser --no-update-check "${ARGS[@]}"
fi
echo "[$(stamp)] updated"
