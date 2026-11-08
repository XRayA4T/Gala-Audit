#!/usr/bin/env bash
# stop-portal.sh - stop the portal, the watcher, the web terminal and the Claude Code session.
# Your files in data/ and the questions (.portal/portal.db) are kept.   --keep-claude leaves Claude running.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$HERE"
for p in watch ttyd portal; do
  if [ -s ".portal/$p.pid" ]; then kill "$(cat ".portal/$p.pid")" 2>/dev/null && echo "stopped $p"; rm -f ".portal/$p.pid"; fi
done
if [ "${1:-}" != "--keep-claude" ] && tmux -L gala-portal has-session -t claude 2>/dev/null; then
  tmux -L gala-portal kill-server && echo "stopped Claude Code"
fi
