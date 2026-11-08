#!/usr/bin/env bash
# install.sh - install the CGA Gala Audit Portal on this computer, from GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/XRayA4T/Gala-Audit/main/install.sh | bash
#   (or, from a copy of the repository:  bash install.sh)
#
# It checks the tools the portal needs (offering to install the missing ones), clones the portal into
# ~/CGA Gala Audit (set GALA_DIR to put it elsewhere), installs the gala-ocr plugin for Claude Code, and can add a
# launcher to the desktop menu. Run it again at any time: an existing install is updated instead (update.sh).
#
# Options:  --dir DIR   --yes (install missing tools without asking)   --no-launcher
set -euo pipefail

REPO="${GALA_REPO:-https://github.com/XRayA4T/Gala-Audit.git}"
DIR="${GALA_DIR:-$HOME/CGA Gala Audit}"
YES=0; LAUNCHER=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --no-launcher) LAUNCHER=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*"; exit 1; }
# Questions read from the terminal, so they work when the script is piped from curl.
ask() {
  [ "$YES" = 1 ] && return 0
  [ -r /dev/tty ] || return 1
  local a; printf '%s [Y/n] ' "$1" > /dev/tty; read -r a < /dev/tty || return 1
  [ -z "$a" ] || [[ "$a" =~ ^[Yy] ]]
}

# ---- 1. tools
OS="$(uname -s)"
PKG=""
if [ "$OS" = Darwin ]; then
  command -v brew >/dev/null && PKG="brew install"
elif command -v apt-get >/dev/null; then PKG="sudo apt-get install -y"
elif command -v dnf >/dev/null; then PKG="sudo dnf install -y"
elif command -v pacman >/dev/null; then PKG="sudo pacman -S --noconfirm"
fi
need=()
command -v git >/dev/null      || need+=(git)
command -v python3 >/dev/null  || need+=(python3)
command -v tmux >/dev/null     || need+=(tmux)
command -v curl >/dev/null     || need+=(curl)
command -v pdftoppm >/dev/null || need+=("$([ "$OS" = Darwin ] && echo poppler || echo poppler-utils)")
if [ ${#need[@]} -gt 0 ]; then
  say "Missing: ${need[*]}"
  if [ -n "$PKG" ] && ask "Install them now with: $PKG ${need[*]} ?"; then
    [[ "$PKG" == "sudo apt-get"* ]] && sudo apt-get update -qq
    $PKG "${need[@]}"
  else
    die "Install ${need[*]} and run this again."
  fi
fi
if ! command -v claude >/dev/null; then
  say "Claude Code isn't installed."
  if ask "Install it now (Anthropic's installer: curl -fsSL https://claude.ai/install.sh | bash)?"; then
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v claude >/dev/null || die "Install Claude Code (https://docs.claude.com/en/docs/claude-code) and run this again."
fi
command -v ttyd >/dev/null || warn "Optional: install ttyd for the portal's web terminal ($PKG ttyd)."

# ---- 2. the portal
if [ -d "$DIR/.git" ]; then
  say "Already installed in $DIR - updating it"
  exec bash "$DIR/update.sh"
fi
if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR exists and isn't empty. Use --dir to install somewhere else."
fi
say "Installing into $DIR"
git clone -q "$REPO" "$DIR"
cd "$DIR"

# ---- 3. the gala-ocr plugin for Claude Code
say "Installing the gala-ocr plugin for Claude Code"
claude plugin marketplace add XRayA4T/Gala-AI-Plugins >/dev/null 2>&1 || true
claude plugin install gala-ocr@gala-ai-plugins >/dev/null 2>&1 || warn "Couldn't install the plugin now; start-portal.sh tries again."

# ---- 4. a launcher in the desktop menu (Linux)
if [ "$LAUNCHER" = 1 ] && [ "$OS" = Linux ] && [ -d "$HOME/.local/share" ]; then
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/cga-gala-audit.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CGA Gala Audit Portal
Comment=Start the gala audit portal (checks for updates first)
Exec=bash -c 'cd "$DIR" && bash start-portal.sh --update; echo; read -rp "Press Enter to close this window "'
Icon=$DIR/portal/brand/cga-logo.png
Terminal=true
Categories=Office;
EOF
  say "Added \"CGA Gala Audit Portal\" to the applications menu"
fi

say "Installed $(git -C "$DIR" log -1 --format='%h (%cd)' --date=short)"
cat <<EOF

Next:
  1. If Claude Code isn't signed in on this computer yet, run:   claude
  2. The first time, start the portal in this terminal and answer Claude Code's questions
     (trust the folder, use the portal MCP server, the plugin):
        cd "$DIR" && bash start-portal.sh --attach        (then Ctrl-b d to leave Claude running)
  3. After that:   cd "$DIR" && bash start-portal.sh      (add --update to update first)
     Updates:      bash "$DIR/update.sh"   (or the Update button on the portal's Restart tab)

The gala's files stay in $DIR/data on this computer and are never put in git.
EOF
