#!/usr/bin/env bash
# claude-loop.sh - keep Claude Code running in the portal's tmux session.
#   - starts it with -c (continue the last conversation) and the restart prompt, so after any restart
#     Claude redoes its start-up tasks and carries on; with no conversation to continue, starts a new one
#   - when Claude exits: starts it again after 5 seconds; press Ctrl-C during the countdown for a shell
#   - gives up (shell) if Claude exits within 30 s three times in a row
DELAY=5
DIR="$(cd "$(dirname "$0")" && pwd)"
prompt() {
  cat "$DIR/resume-prompt.txt"
}
stop=0; quick=0
trap 'stop=1' INT
cd "$DIR/.." || exit 1
export GALA_PORTAL=1
while true; do
  clear
  echo "Claude Code in $PWD  (keeps running if you close the browser tab)"
  t0=$(date +%s)
  claude -c "$@" "$(prompt)"; rc=$?
  if [ "$rc" -ne 0 ] && [ $(( $(date +%s) - t0 )) -lt 15 ]; then
    echo "Couldn't continue a conversation (exit $rc) - starting a new one."
    sleep 1; t0=$(date +%s)
    claude "$@" "$(prompt)"; rc=$?
  fi
  if [ $(( $(date +%s) - t0 )) -lt 30 ]; then quick=$((quick + 1)); else quick=0; fi
  stop=0
  echo
  if [ "$quick" -ge 3 ]; then
    echo "Claude exited straight away 3 times in a row (last exit code $rc) - not restarting."
    echo "Fix the problem, then run:  $0 $*"
    exec bash -l
  fi
  echo "Claude has exited (code $rc). Restarting in ${DELAY}s, continuing the conversation."
  echo "Press Ctrl-C to stay at the shell instead."
  for ((i = DELAY; i > 0; i--)); do printf '\r  %d ' "$i"; sleep 1; [ "$stop" = 1 ] && break; done
  echo
  if [ "$stop" = 1 ]; then
    echo "Staying at the shell. Start Claude again (with the restart loop) with:  $0 $*"
    trap - INT
    exec bash -l
  fi
done
