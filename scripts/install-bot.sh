#!/usr/bin/env bash
# Bootstrap `unread bot` on a fresh Linux VM.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh | bash
#
# Or, with the script downloaded locally:
#   bash scripts/install-bot.sh
#
# What this does, in order:
#   1. Verifies Python 3.11+ is present (installs via apt/dnf if missing).
#   2. Installs system deps (ffmpeg).
#   3. Installs pipx if absent, then `unread[bot]` into an isolated venv.
#   4. Runs `unread init` interactively (AI key + Telegram creds + session).
#   5. Prompts for the bot's `@BotFather` token, writes it to `~/.unread/.env`.
#   6. Drops a `systemd --user` unit that auto-restarts on crash + survives
#      logout (enables linger for the current user).
#
# Idempotent: re-running skips steps that already succeeded. Pass
# `--reset` to wipe `~/.unread/` first (warning: deletes reports + session).

set -euo pipefail

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_RST=""
fi
ok()   { printf "%s✓%s %s\n" "$C_GREEN" "$C_RST" "$*"; }
warn() { printf "%s!%s %s\n" "$C_YELLOW" "$C_RST" "$*"; }
err()  { printf "%s✖%s %s\n" "$C_RED" "$C_RST" "$*" >&2; }
step() { printf "\n%s→%s %s\n" "$C_DIM" "$C_RST" "$*"; }

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
RESET=0
SKIP_INIT=0
for arg in "$@"; do
  case "$arg" in
    --reset)      RESET=1 ;;
    --skip-init)  SKIP_INIT=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      err "unknown argument: $arg"
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# OS / package-manager detection
# ---------------------------------------------------------------------------
OS=""
PKG=""
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS="${ID:-unknown}"
  case "$OS" in
    ubuntu|debian) PKG="apt" ;;
    fedora|rhel|centos|rocky|almalinux) PKG="dnf" ;;
    arch|manjaro) PKG="pacman" ;;
  esac
fi
if [[ -z "$PKG" && "$(uname -s)" == "Darwin" ]]; then
  PKG="brew"
fi
if [[ -z "$PKG" ]]; then
  err "Unsupported OS — install ffmpeg + Python 3.11+ manually, then 'pipx install unread[bot]'."
  exit 1
fi

step "Detected OS: ${OS:-macOS} (pkg manager: $PKG)"

# ---------------------------------------------------------------------------
# Sudo discovery — install steps that touch system packages may need it.
# ---------------------------------------------------------------------------
SUDO=""
if [[ "$PKG" != "brew" ]] && [[ "$EUID" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    warn "No sudo and not root — system package installs will fail. Re-run as root or pre-install ffmpeg + python."
  fi
fi

# Suppress every interactive prompt apt might raise. Without these, a
# `curl … | bash` run gets stuck on whiptail dialogs like "Daemons using
# outdated libraries: which services should be restarted?" — whiptail
# needs a real TTY, which the pipe doesn't provide, and the user can't
# interact with arrow keys.
#
# - DEBIAN_FRONTEND=noninteractive   silences debconf prompts (postfix
#                                    config wizard, MySQL root password, …)
# - NEEDRESTART_MODE=a               needrestart auto-restarts daemons
#                                    instead of asking which to restart
# - NEEDRESTART_SUSPEND=1            also covers the "restart now?" dialog
# - APT_LISTCHANGES_FRONTEND=none    suppresses the changelog viewer
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1
export APT_LISTCHANGES_FRONTEND=none

pkg_install() {
  case "$PKG" in
    apt)
      # `-o Dpkg::Options::=--force-confnew` — keep the package's new
      # config without prompting on conffile conflicts. Combined with
      # the env vars above, every interactive dialog is suppressed.
      $SUDO -E apt-get update -y -qq \
        && $SUDO -E apt-get install -y -qq \
             -o Dpkg::Options::="--force-confnew" \
             -o Dpkg::Options::="--force-confdef" \
             "$@"
      ;;
    dnf)    $SUDO dnf install -y "$@" ;;
    pacman) $SUDO pacman -Sy --noconfirm "$@" ;;
    brew)   brew install "$@" ;;
  esac
}

# ---------------------------------------------------------------------------
# Optional reset
# ---------------------------------------------------------------------------
if [[ "$RESET" == "1" ]]; then
  warn "Reset requested — wiping ~/.unread/ in 5 seconds (Ctrl-C to cancel)…"
  sleep 5
  rm -rf "$HOME/.unread"
  ok "~/.unread/ removed."
fi

# ---------------------------------------------------------------------------
# 1. Python 3.11+
# ---------------------------------------------------------------------------
step "Checking Python 3.11+"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
      PYTHON="$candidate"
      break
    fi
  fi
done
if [[ -z "$PYTHON" ]]; then
  warn "No Python 3.11+ found — installing"
  case "$PKG" in
    apt)    pkg_install python3.11 python3.11-venv python3-pip ;;
    dnf)    pkg_install python3.11 ;;
    pacman) pkg_install python ;;
    brew)   pkg_install python@3.11 ;;
  esac
  PYTHON="$(command -v python3.11 || command -v python3)"
fi
ok "Python: $PYTHON ($($PYTHON --version))"

# ---------------------------------------------------------------------------
# 2. System deps: ffmpeg
# ---------------------------------------------------------------------------
step "Checking ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg already present: $(ffmpeg -version | head -n1)"
else
  pkg_install ffmpeg
  ok "ffmpeg installed."
fi

# ---------------------------------------------------------------------------
# 3. pipx + unread[bot]
# ---------------------------------------------------------------------------
step "Installing unread[bot] via pipx"
if ! command -v pipx >/dev/null 2>&1; then
  $PYTHON -m pip install --user --quiet pipx
  $PYTHON -m pipx ensurepath
  # Make pipx available in THIS shell without re-login.
  export PATH="$HOME/.local/bin:$PATH"
fi
if pipx list 2>/dev/null | grep -q '^   package unread '; then
  warn "unread already installed via pipx — upgrading"
  pipx upgrade 'unread[bot]'
else
  pipx install --force 'unread[bot]'
fi
ok "unread installed: $(unread --version 2>/dev/null || echo 'installed')"

# ---------------------------------------------------------------------------
# 4. unread init
# ---------------------------------------------------------------------------
if [[ "$SKIP_INIT" == "0" ]] && [[ ! -f "$HOME/.unread/install.toml" ]]; then
  step "Running 'unread init' — set up AI provider + Telegram credentials + user session"
  unread init
else
  ok "Skipping init (~/.unread/install.toml exists or --skip-init)"
fi

# ---------------------------------------------------------------------------
# 5. Bot token
# ---------------------------------------------------------------------------
ENV_PATH="$HOME/.unread/.env"
mkdir -p "$HOME/.unread"
touch "$ENV_PATH"
chmod 600 "$ENV_PATH"

if grep -qE '^UNREAD_BOT_TOKEN=.+' "$ENV_PATH"; then
  ok "UNREAD_BOT_TOKEN already set in ~/.unread/.env"
else
  step "Bot token (from @BotFather)"
  printf "Paste your bot token: "
  read -r BOT_TOKEN < /dev/tty
  if [[ -z "$BOT_TOKEN" ]]; then
    err "Empty token — aborting. Re-run when you have one from @BotFather."
    exit 1
  fi
  # Append (don't dedupe — the user can clean stale lines later).
  printf '\nUNREAD_BOT_TOKEN=%s\n' "$BOT_TOKEN" >> "$ENV_PATH"
  ok "Bot token saved to $ENV_PATH"
fi

# ---------------------------------------------------------------------------
# 6. systemd --user unit
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
  warn "systemd not detected — start the bot manually: 'unread bot run'"
  warn "Or use the Docker setup in docker-compose.bot.prod.yml."
  exit 0
fi

step "Installing systemd --user service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/unread-bot.service"
mkdir -p "$UNIT_DIR"

# Resolve `unread` to a full path — systemd doesn't read your shell rc.
UNREAD_BIN="$(command -v unread)"
if [[ -z "$UNREAD_BIN" ]]; then
  err "Can't locate the 'unread' binary on PATH — check that pipx finished."
  exit 1
fi

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=unread Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$UNREAD_BIN bot run
Restart=on-failure
RestartSec=5
# Keep stdout/stderr in the journal (journalctl --user -u unread-bot -f).
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
ok "Wrote $UNIT_FILE"

# Linger keeps user-services running after logout. Needs root.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  if [[ -n "$SUDO" ]] || [[ "$EUID" -eq 0 ]]; then
    $SUDO loginctl enable-linger "$USER"
    ok "Enabled linger for $USER (service survives logout)"
  else
    warn "Couldn't enable linger (no sudo). Run 'sudo loginctl enable-linger $USER' manually so the bot keeps running after SSH disconnect."
  fi
fi

systemctl --user daemon-reload
systemctl --user enable --now unread-bot.service
ok "unread-bot.service started"

cat <<EOF

${C_GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_GREEN}✓ Setup complete.${C_RST}

Useful commands:
  Status:  systemctl --user status unread-bot
  Logs:    journalctl --user -u unread-bot -f
  Restart: systemctl --user restart unread-bot
  Stop:    systemctl --user stop unread-bot

Open Telegram and message your bot — it should reply.
${C_GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
EOF
