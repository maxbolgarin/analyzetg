#!/usr/bin/env bash
# Copy the bot's compose file + .env to a VM, then pull the image and
# restart the container. No git clone on the remote side — only docker
# + ssh need to exist there.
#
# Usage:
#   scripts/deploy-bot.sh user@host[:port] [/remote/path]
#
# Examples:
#   scripts/deploy-bot.sh deploy@bot.example.com
#   scripts/deploy-bot.sh deploy@bot.example.com:2222 /srv/unread-bot
#   ENV_FILE=.env.bot.prod scripts/deploy-bot.sh deploy@bot.example.com
#
# Environment overrides:
#   ENV_FILE         — local env file to copy as .env.bot (default: .env.bot)
#   COMPOSE_FILE     — local compose file (default: docker-compose.bot.prod.yml)
#   REMOTE_DIR       — remote target dir (default: /srv/unread-bot or the
#                      positional arg if given)
#   SKIP_RESTART=1   — copy files only; don't run docker compose remotely

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/.env.bot}"
COMPOSE_FILE="${COMPOSE_FILE:-${HERE}/docker-compose.bot.yml}"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 user@host[:port] [/remote/path]" >&2
  exit 2
fi

REMOTE_TARGET="$1"
REMOTE_DIR="${2:-${REMOTE_DIR:-/srv/unread-bot}}"

# Split host:port into rsync/ssh flags. Default port is 22.
if [[ "$REMOTE_TARGET" == *:* ]]; then
  REMOTE_HOST="${REMOTE_TARGET%:*}"
  REMOTE_PORT="${REMOTE_TARGET##*:}"
else
  REMOTE_HOST="$REMOTE_TARGET"
  REMOTE_PORT="22"
fi

for f in "$ENV_FILE" "$COMPOSE_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "✖ missing local file: $f" >&2
    echo "  (copy .env.bot.example → .env.bot first, or set ENV_FILE)" >&2
    exit 1
  fi
done

# Hard-fail on a placeholder env so we don't silently push an empty
# config to the VM and stand up a bot that immediately crash-loops.
if grep -qE '^(TELEGRAM_API_ID|TELEGRAM_API_HASH|UNREAD_BOT_TOKEN)=\s*$' "$ENV_FILE"; then
  echo "✖ $ENV_FILE has unfilled required values (TELEGRAM_*, UNREAD_BOT_TOKEN)." >&2
  exit 1
fi

SSH=(ssh -p "$REMOTE_PORT" -o ConnectTimeout=10)
RSYNC_SSH="ssh -p ${REMOTE_PORT}"

echo "→ ensuring $REMOTE_DIR exists on $REMOTE_HOST"
"${SSH[@]}" "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR' && chmod 700 '$REMOTE_DIR'"

echo "→ copying compose file + env"
rsync -avz --no-perms --omit-dir-times \
  -e "$RSYNC_SSH" \
  "$COMPOSE_FILE" \
  "${REMOTE_HOST}:${REMOTE_DIR}/docker-compose.yml"

# Use a temp upload + atomic rename so a half-written .env can never be
# read by a concurrent `docker compose` on the remote side. 0600 keeps
# the API keys readable by the deploy user only.
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
cp "$ENV_FILE" "$TMP_ENV"
chmod 600 "$TMP_ENV"
rsync -avz --no-perms --omit-dir-times \
  -e "$RSYNC_SSH" \
  "$TMP_ENV" \
  "${REMOTE_HOST}:${REMOTE_DIR}/.env.bot.new"
"${SSH[@]}" "$REMOTE_HOST" \
  "mv '${REMOTE_DIR}/.env.bot.new' '${REMOTE_DIR}/.env.bot' && chmod 600 '${REMOTE_DIR}/.env.bot'"

if [[ "${SKIP_RESTART:-0}" == "1" ]]; then
  echo "✓ files in place at ${REMOTE_HOST}:${REMOTE_DIR} (SKIP_RESTART=1; not touching the container)"
  exit 0
fi

echo "→ pulling image + restarting on $REMOTE_HOST"
"${SSH[@]}" "$REMOTE_HOST" bash -se <<EOF
set -euo pipefail
cd '${REMOTE_DIR}'
docker compose --env-file .env.bot pull
docker compose --env-file .env.bot up -d
docker compose --env-file .env.bot ps
EOF

echo "✓ deploy complete"
echo "  tail logs:   ${SSH[*]} $REMOTE_HOST 'cd $REMOTE_DIR && docker compose logs -f'"
