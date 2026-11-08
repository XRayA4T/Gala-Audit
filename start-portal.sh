#!/usr/bin/env bash
# start-portal.sh - start the CGA Gala Audit Portal on this computer.
#
#   1. checks the tools (python3, poppler, tmux, claude)
#   2. makes the data/ folders and a private key (.portal/key)
#   3. sets up the project for Claude Code: the gala-ocr plugin, the portal MCP server, permissions, the inbox hook
#   4. starts the portal web page (http://127.0.0.1:PORT), Claude Code in tmux, and the watcher
#   5. opens the portal in your web browser; Claude keeps running in the background (watch it on the Live tab)
#
# Options:  --port N (default 9000)   --host ADDR (default 0.0.0.0: every network; 127.0.0.1: this computer only)
#             --inbox DIR (default data/inbox)   --name "Gala name"
#           --no-browser   --attach (put this terminal into Claude's session; leave it with Ctrl-b d)
#           --update (update from GitHub first, with update.sh)   --no-update-check
# Stop everything with:  bash stop-portal.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ORIG_ARGS=("$@")
PORT="${PORTAL_PORT:-9000}"; HOST="${PORTAL_HOST:-0.0.0.0}"; INBOX="${GALA_INBOX:-data/inbox}"; TERM_PORT=""; BROWSER=1; ATTACH=0; UPDATE=0; NAME="CGA Gala Audit"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --inbox) INBOX="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --no-browser) BROWSER=0; shift ;;
    --attach) ATTACH=1; shift ;;
    --no-attach) ATTACH=0; shift ;;   # the default now; kept so older commands still work
    --update) UPDATE=1; shift ;;
    --no-update-check) UPDATE=-1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
SOCK=gala-portal; SESSION=claude; WHO=gala-claude
ME="$(id -un 2>/dev/null | tr 'A-Z ' 'a-z-' || echo auditor)"
BOARD="http://127.0.0.1:$PORT"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*"; exit 1; }

# ---- 0. updates from GitHub (a git clone only)
if [ -d .git ] && [ -f update.sh ]; then
  if [ "$UPDATE" = 1 ]; then
    before="$(git rev-parse HEAD)"
    bash update.sh || warn "Not updated; starting the version installed."
    if [ "$(git rev-parse HEAD)" != "$before" ]; then   # run the new start-portal.sh
      args=(); for a in "${ORIG_ARGS[@]}"; do [ "$a" = --update ] || args+=("$a"); done
      exec bash "$0" --no-update-check "${args[@]}"
    fi
  elif [ "$UPDATE" = 0 ]; then
    rc=0; timeout 20 bash update.sh --check >/dev/null 2>&1 || rc=$?
    [ "$rc" = 10 ] && warn "An update is available: bash update.sh  (or start with --update, or the Update button on the Restart tab)"
  fi
fi

# ---- 1. tools
command -v python3 >/dev/null || die "python3 is needed."
command -v tmux    >/dev/null || die "tmux is needed:  sudo apt install tmux   (Mac: brew install tmux)"
command -v claude  >/dev/null || die "Claude Code is needed:  npm install -g @anthropic-ai/claude-code   (then run 'claude' once to sign in)"
command -v curl    >/dev/null || die "curl is needed."
for t in pdfinfo pdftoppm pdftotext; do
  command -v "$t" >/dev/null || warn "$t is missing - the gala-ocr skill needs poppler:  sudo apt install poppler-utils   (Mac: brew install poppler)"
done

# ---- 2. folders and key
mkdir -p .portal data/sessions data/unsorted "$INBOX"
chmod 700 .portal
# remember how it was started, so update.sh starts it again the same way
printf '%q ' --port "$PORT" --host "$HOST" --inbox "$INBOX" --name "$NAME" > .portal/start-args
[ -s .portal/key ] || python3 -c 'import secrets;print(secrets.token_urlsafe(18))' > .portal/key
chmod 600 .portal/key
KEY="$(cat .portal/key)"
# the admin key: lets a browser type to Claude in the web terminal (everyone else with the portal link can watch)
[ -s .portal/admin-key ] || python3 -c 'import secrets;print(secrets.token_urlsafe(12))' > .portal/admin-key
chmod 600 .portal/admin-key
ADMIN="$(cat .portal/admin-key)"

# ---- 3. Claude Code project settings
say "Setting up Claude Code for this folder"
python3 - "$BOARD" "$KEY" "$WHO" <<'PY'
import json, os, sys
board, key, who = sys.argv[1:4]
def load(p):
    try:
        with open(p) as f: return json.load(f)
    except (OSError, ValueError): return {}
def save(p, d):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f: json.dump(d, f, indent=2); f.write("\n")

# The portal as an MCP server (project scope, .mcp.json). Contains the key: kept out of git (.gitignore).
m = load(".mcp.json"); m.setdefault("mcpServers", {})
m["mcpServers"]["portal"] = {"type": "http", "url": "%s/mcp?who=%s&key=%s" % (board, who, key)}
save(".mcp.json", m)

# Shared project settings: the gala-ocr plugin from the Gala-AI-Plugins marketplace.
s = load(".claude/settings.json")
s.setdefault("extraKnownMarketplaces", {})["gala-ai-plugins"] = {
    "source": {"source": "github", "repo": "XRayA4T/Gala-AI-Plugins"}}
s.setdefault("enabledPlugins", {})["gala-ocr@gala-ai-plugins"] = True
save(".claude/settings.json", s)

# Local settings: trust the portal server, allow the routine tools, show the inbox with every prompt.
l = load(".claude/settings.local.json")
l["enableAllProjectMcpServers"] = True
l.setdefault("enabledMcpjsonServers", [])
if "portal" not in l["enabledMcpjsonServers"]: l["enabledMcpjsonServers"].append("portal")
allow = l.setdefault("permissions", {}).setdefault("allow", [])
for rule in ["mcp__portal", "Bash(pdfinfo:*)", "Bash(pdftoppm:*)", "Bash(pdftotext:*)", "Bash(pdftocairo:*)",
             "Bash(python3:*)", "Bash(ls:*)", "Bash(mkdir:*)", "Read(./data/**)",
             "Edit(./data/sessions/*/ocr/**)", "Write(./data/sessions/*/ocr/**)",
             "Edit(./data/sessions/*/reports/**)", "Write(./data/sessions/*/reports/**)"]:
    if rule not in allow: allow.append(rule)
hook = "curl -s --max-time 5 '%s/api/digest?to=%s&key=%s' || true" % (board, who, key)
l.setdefault("hooks", {})["UserPromptSubmit"] = [{"hooks": [{"type": "command", "command": hook}]}]
save(".claude/settings.local.json", l)
PY

mkdir -p .claude
[ -e .claude/startup.md ] || echo '# Things to redo after Claude Code restarts (read on every restart). Usually nothing.' > .claude/startup.md

# Install the plugin for this user too (harmless if it's already there; the project settings also offer it).
if ! claude plugin list 2>/dev/null | grep -q 'gala-ocr'; then
  say "Installing the gala-ocr plugin"
  claude plugin marketplace add XRayA4T/Gala-AI-Plugins >/dev/null 2>&1 || true
  claude plugin install gala-ocr@gala-ai-plugins >/dev/null 2>&1 || warn "Couldn't install gala-ocr automatically. In Claude, run:  /plugin install gala-ocr@gala-ai-plugins"
else
  claude plugin marketplace update gala-ai-plugins >/dev/null 2>&1 || true
  claude plugin update gala-ocr@gala-ai-plugins >/dev/null 2>&1 || true   # needs 1.6.0+ (Gen7 race PDFs, --events)
fi

# ---- 4a. the portal (it serves the web terminal too, if ttyd is installed: /terminal/ro/ to watch, /terminal/rw/ to type)
TERM_ARGS=()
if command -v ttyd >/dev/null; then
  TERM_PORT=$((PORT + 1)); TERM_RO_PORT=$((PORT + 2))
  TERM_ARGS=(--term-rw-port "$TERM_PORT" --term-ro-port "$TERM_RO_PORT" --admin-key "$ADMIN")
fi
if curl -s --max-time 2 "$BOARD/health?key=$KEY" | grep -q '"ok"'; then
  say "The portal is already running on $BOARD"
else
  say "Starting the portal on $BOARD"
  nohup python3 portal/portal.py serve --host "$HOST" --port "$PORT" --db .portal/portal.db --key "$KEY" \
        --max-doc-mb 200 --name "$NAME" --brand-dir portal/brand --gala-data data --inbox "$INBOX" ${TERM_ARGS[@]+"${TERM_ARGS[@]}"} > .portal/portal.log 2>&1 &
  echo $! > .portal/portal.pid
  for _ in $(seq 1 40); do curl -s --max-time 1 "$BOARD/health?key=$KEY" | grep -q '"ok"' && break; sleep 0.25; done
  curl -s --max-time 2 "$BOARD/health?key=$KEY" | grep -q '"ok"' || die "The portal didn't start - see .portal/portal.log"
fi
# The people on the board: Claude, and you.
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "{\"name\":\"$WHO\",\"kind\":\"claude\"}" "$BOARD/api/users?key=$KEY" || true
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -d "{\"name\":\"$ME\",\"kind\":\"person\"}" "$BOARD/api/users?key=$KEY" || true

# ---- 4b. Claude Code in tmux (restarts itself, continuing the conversation)
if tmux -L "$SOCK" has-session -t "$SESSION" 2>/dev/null; then
  say "Claude Code is already running (tmux -L $SOCK attach -t $SESSION)"
else
  say "Starting Claude Code"
  tmux -L "$SOCK" -f portal/tmux.conf new-session -d -s "$SESSION" -x 200 -y 50 -c "$HERE" "bash portal/claude-loop.sh"
fi

# ---- 4c. the watcher: announces new questions to Claude, mirrors its screen on the Live tab
mkdir -p .portal/watch
cp portal/board-watch.py .portal/watch/board-watch.py
python3 - "$BOARD" "$KEY" "$WHO" "$SOCK" "$SESSION" > .portal/watch/config.json <<'PY'
import json, sys
b, k, w, sock, sess = sys.argv[1:6]
print(json.dumps({"board": b, "key": k, "who": w, "tmux_socket": sock, "tmux_session": sess,
                  "poll": 5, "idle": 8, "live": True, "live_interval": 2, "title": "Claude Code - gala audit"}, indent=2))
PY
if [ -s .portal/watch.pid ] && kill -0 "$(cat .portal/watch.pid)" 2>/dev/null; then :; else
  nohup python3 .portal/watch/board-watch.py > .portal/watch.log 2>&1 &
  echo $! > .portal/watch.pid
fi

# ---- 4d. optional web terminal (ttyd), on this computer only; the portal passes it through with its keys.
# One to type to Claude (needs the admin key), one to watch it (a read-only tmux client, so it can't type either way).
if [ -n "$TERM_PORT" ]; then
  for p in ttyd ttyd-ro; do
    [ -s ".portal/$p.pid" ] && kill "$(cat ".portal/$p.pid")" 2>/dev/null; rm -f ".portal/$p.pid"
  done
  W=""; ttyd --help 2>&1 | grep -q -- '--writable' && W="-W"          # ttyd 1.7+ is read-only unless -W
  RO=(-r); tmux -V | awk '{v=$2+0; exit !(v>=3.2)}' && RO=(-f read-only,ignore-size)   # a watcher doesn't resize Claude's screen
  nohup ttyd -i 127.0.0.1 -p "$TERM_PORT" -b /terminal/rw $W tmux -L "$SOCK" attach -t "$SESSION" > .portal/ttyd.log 2>&1 &
  echo $! > .portal/ttyd.pid
  nohup ttyd -i 127.0.0.1 -p "$TERM_RO_PORT" -b /terminal/ro tmux -L "$SOCK" attach "${RO[@]}" -t "$SESSION" > .portal/ttyd-ro.log 2>&1 &
  echo $! > .portal/ttyd-ro.pid
fi

URL="$BOARD/?key=$KEY&me=$ME"
OPEN_URL="$URL&admin=$ADMIN"   # this computer's browser can type in the web terminal
echo
say "Portal:  $URL"
if [ "$HOST" != "127.0.0.1" ]; then
  LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$LAN" ] || LAN="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [ -n "$LAN" ] && say "On the network:  http://$LAN:$PORT/?key=$KEY   (anyone with this link can open it)"
fi
if [ -n "$TERM_PORT" ]; then
  say "Web terminal:  anyone with the portal link can watch Claude (Live tab, Open terminal); to type to it,"
  say "         a browser needs the admin key: $ADMIN   (keep it to yourself; this computer's browser has it already)"
else
  say "Web terminal:  install ttyd for one in the portal (sudo apt install ttyd)"
fi
say "Inbox:   $(cd "$INBOX" && pwd)   (the scanner / Gen7 computer save here, or drop files on the Gala tab)"
say "Data:    $HERE/data/sessions"
say "Claude:  running in the background - watch it on the portal's Live tab"
say "         to type to it here:  tmux -L $SOCK attach -t $SESSION   (leave with Ctrl-b d)"
say "Stop:    bash stop-portal.sh"
echo

# ---- 5. browser and terminal
if [ "$BROWSER" = 1 ]; then
  if command -v xdg-open >/dev/null; then xdg-open "$OPEN_URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null; then open "$OPEN_URL"
  elif command -v wslview >/dev/null; then wslview "$OPEN_URL"
  else warn "Open the address above in your browser."; fi
fi
if [ "$ATTACH" = 1 ] && [ -t 1 ]; then
  sleep 1
  exec tmux -L "$SOCK" attach -t "$SESSION"
fi
# Claude Code may be waiting for an answer the first time (trust this folder, the portal MCP server, the plugin):
# say so, since nobody is looking at its terminal.
sleep 4
if tmux -L "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null | grep -qiE "do you trust|trust the files|enter to confirm|\\(y/n\\)|❯ 1\\. yes"; then
  warn "Claude Code is asking a question (first start?). Answer it with:  tmux -L $SOCK attach -t $SESSION"
fi
