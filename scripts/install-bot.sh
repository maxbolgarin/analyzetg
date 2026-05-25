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
#   1. Installs uv (single static binary; manages its own Python 3.11+).
#   2. Installs system deps (ffmpeg, libpango — the latter is needed by
#      weasyprint for PDF report rendering, which is now a base feature).
#   3. `uv tool install unread` — isolated tool venv, `unread` on PATH.
#   4. Runs `unread init` interactively — AI provider menu, Telegram
#      credentials prompt, Telethon phone-code login. Stdin is
#      reassigned to /dev/tty up front so this works under `curl | bash`.
#      If `unread init` exits non-zero (older CLI versions had a bug
#      where this step bailed out instead of prompting), the script
#      prints a clear recovery hint and exits — re-running after
#      `unread init` is done picks up from there.
#   5. Prompts for the bot's `@BotFather` token, writes it to `~/.unread/.env`.
#   6. Drops a `systemd --user` unit that auto-restarts on crash + survives
#      logout (enables linger for the current user).
#
# Idempotent: re-running skips steps that already succeeded. Pass
# `--reset` to wipe `~/.unread/` first (warning: deletes reports + session).
# Pass `--skip-init` to bypass the `unread init` step when
# `~/.unread/.env` is pre-provisioned via Ansible / SCP.

set -euo pipefail

# ---------------------------------------------------------------------------
# Reassign stdin to the user's terminal.
#
# `curl … | bash` pipes the script's body in on stdin, leaving no TTY
# for interactive prompts. `unread init` and our own bot-token read
# both need keyboard input, so reattach stdin to /dev/tty up front.
# Without this, the init wizard bails with
#     "Missing: TELEGRAM_API_ID / TELEGRAM_API_HASH, OPENAI_API_KEY"
# instead of asking for them.
#
# When run as plain `bash scripts/install-bot.sh`, stdin is already
# the TTY and this is a no-op.
# ---------------------------------------------------------------------------
if [[ ! -t 0 ]]; then
  if [[ -r /dev/tty ]]; then
    exec </dev/tty
  else
    echo "✖ No TTY available — interactive prompts won't work." >&2
    echo "  Re-run as 'bash scripts/install-bot.sh' (downloaded first) or run on a real shell." >&2
    exit 1
  fi
fi

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
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
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
  err "Unsupported OS — install ffmpeg + libpango manually, then 'uv tool install unread'."
  exit 1
fi

step "Detected OS: ${OS:-macOS} (pkg manager: $PKG)"

# On macOS we lean on Homebrew for ffmpeg + pango. Confirm it's
# reachable from this shell — Apple Silicon puts brew at
# /opt/homebrew/bin/brew which isn't always on PATH for non-login
# shells, so probe known locations and prepend to PATH if found.
# Bail early with the install URL if still missing — beats dying
# mid-pipe with a confusing `brew: command not found`.
if [[ "$PKG" == "brew" ]] && ! command -v brew >/dev/null 2>&1; then
  for brew_candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
    if [[ -x "$brew_candidate" ]]; then
      eval "$("$brew_candidate" shellenv)"
      break
    fi
  done
fi
if [[ "$PKG" == "brew" ]] && ! command -v brew >/dev/null 2>&1; then
  err "Homebrew not installed (or not on PATH)."
  err "Install it from https://brew.sh first:"
  err '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  err "Or, if brew IS installed but not on PATH, add it:"
  err '  eval "$(/opt/homebrew/bin/brew shellenv)"   # Apple Silicon'
  err '  eval "$(/usr/local/bin/brew shellenv)"      # Intel'
  err "Then re-run this script."
  exit 1
fi

# ---------------------------------------------------------------------------
# Sudo discovery — install steps that touch system packages may need it.
# ---------------------------------------------------------------------------
SUDO=""
if [[ "$PKG" != "brew" ]] && [[ "$EUID" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    warn "No sudo and not root — system package installs will fail. Re-run as root or pre-install ffmpeg + libpango."
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

# Per-OS package name set for the runtime stack we need:
# - ffmpeg            — Whisper voice/video transcription
# - libpango / cairo  — weasyprint PDF rendering (base dep since v1.x;
#                      missing libs → bot still works but falls back to
#                      .md upload via the runtime guard)
runtime_packages() {
  case "$PKG" in
    apt)    echo "ffmpeg libpango-1.0-0 libpangoft2-1.0-0" ;;
    dnf)    echo "ffmpeg pango" ;;
    pacman) echo "ffmpeg pango" ;;
    brew)   echo "ffmpeg pango" ;;
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
# 1. uv (manages its own Python — no system Python install needed)
# ---------------------------------------------------------------------------
step "Installing uv"
if command -v uv >/dev/null 2>&1; then
  ok "uv already present: $(uv --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installer drops the binary at ~/.local/bin/uv; make it visible
  # in THIS shell without a re-login.
  export PATH="$HOME/.local/bin:$PATH"
  ok "uv installed: $(uv --version 2>/dev/null || echo 'installed')"
fi

# ---------------------------------------------------------------------------
# 2. System deps (ffmpeg + libpango)
# ---------------------------------------------------------------------------
step "Checking system deps (ffmpeg + libpango)"
# `ffmpeg` we can probe via PATH. `libpango` is a shared lib so we
# can't `command -v` it — assume present once it's been installed
# (idempotent re-runs just no-op on already-installed packages anyway).
NEEDS_INSTALL=0
if ! command -v ffmpeg >/dev/null 2>&1; then
  NEEDS_INSTALL=1
fi
# On a fresh re-run, also assume libpango is missing if `ffmpeg` is
# missing (they install together). When ffmpeg is present but libpango
# isn't, weasyprint imports fail at runtime — the bot's PDF helper
# catches that and falls back to .md, so it's a soft failure.
if [[ "$NEEDS_INSTALL" == "1" ]]; then
  # shellcheck disable=SC2086
  pkg_install $(runtime_packages)
  ok "System deps installed."
else
  ok "ffmpeg already present: $(ffmpeg -version 2>/dev/null | head -n1)"
fi

# ---------------------------------------------------------------------------
# 3. unread via uv tool
# ---------------------------------------------------------------------------
step "Installing unread via 'uv tool'"
if uv tool list 2>/dev/null | grep -qE '^unread\s'; then
  warn "unread already installed via uv — upgrading"
  uv tool upgrade unread
else
  # --force lets us re-run after a partial earlier install.
  uv tool install --force unread
fi
# uv tool puts the entry point in ~/.local/bin which we already added
# to PATH above. Re-export defensively in case the shell missed it.
export PATH="$HOME/.local/bin:$PATH"
ok "unread installed: $(unread --version 2>/dev/null || echo 'installed')"

# ---------------------------------------------------------------------------
# 4. unread init
#
# Interactive wizard — AI provider menu, Telegram credentials,
# Telethon phone-code login. Stdin is already reassigned to /dev/tty
# at the top of this script so `curl | bash` runs work too.
#
# Older PyPI versions (≤0.1.0) had a bug where this exited non-zero
# with "Missing: TELEGRAM_API_ID / TELEGRAM_API_HASH" instead of
# prompting. We detect that, print a clear recovery hint, and exit
# cleanly so the user can run `unread init` manually then re-run us.
# ---------------------------------------------------------------------------
if [[ "$SKIP_INIT" == "0" ]] && [[ ! -f "$HOME/.unread/install.toml" ]]; then
  step "Running 'unread init' — set up AI provider + Telegram credentials + user session"
  # Disable -e for this single call so we can recover from a non-zero
  # exit instead of dropping the user back at the shell with no hint.
  set +e
  unread init
  init_rc=$?
  set -e
  if [[ "$init_rc" -ne 0 ]] || [[ ! -f "$HOME/.unread/install.toml" ]]; then
    cat <<EOF

${C_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_YELLOW}\`unread init\` didn't complete (exit ${init_rc}, no install.toml written).${C_RST}

This usually means you're on an older PyPI release that bails out of
the wizard early. Run it directly in this terminal — that always works:

  ${C_DIM}\$${C_RST} unread init

When it finishes (you'll see ${C_GREEN}~/.unread/install.toml${C_RST} written), re-run
this installer to finish the bot-token + systemd-service setup:

  ${C_DIM}\$${C_RST} ./scripts/install-bot.sh

The re-run will skip everything that's already done.
${C_YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
EOF
    exit 0
  fi
  ok "unread init completed."
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
  printf "%sIf Enter does nothing (you see ^M), press Ctrl-J to submit.%s\n" "$C_DIM" "$C_RST"
  printf "Paste your bot token: "
  read -r BOT_TOKEN
  # Strip stray CR / surrounding whitespace a paste over SSH can leave behind.
  BOT_TOKEN="${BOT_TOKEN//$'\r'/}"
  BOT_TOKEN="${BOT_TOKEN#"${BOT_TOKEN%%[![:space:]]*}"}"
  BOT_TOKEN="${BOT_TOKEN%"${BOT_TOKEN##*[![:space:]]}"}"
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
  warn "Or use the Docker setup in docker-compose.bot.yml."
  exit 0
fi

step "Installing systemd --user service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/unread-bot.service"
mkdir -p "$UNIT_DIR"

# Resolve `unread` to a full path — systemd doesn't read your shell rc.
UNREAD_BIN="$(command -v unread)"
if [[ -z "$UNREAD_BIN" ]]; then
  err "Can't locate the 'unread' binary on PATH — check that 'uv tool install' finished."
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
  Upgrade: uv tool upgrade unread && systemctl --user restart unread-bot

Open Telegram and message your bot — it should reply.
${C_GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
EOF
