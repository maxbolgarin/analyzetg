# Generic `unread` CLI container image.
#
# No ENTRYPOINT — the image is a general-purpose `unread` runtime.
# Callers pick what to run:
#
#   # Run the bot (long-running service via compose):
#   docker compose -f docker-compose.bot.yml up -d
#
#   # Run the bot directly (no compose):
#   docker run -d \
#     --name unread-bot \
#     -e TELEGRAM_API_ID=... -e TELEGRAM_API_HASH=... \
#     -e OPENAI_API_KEY=... -e UNREAD_BOT_TOKEN=... \
#     -v unread_state:/root/.unread \
#     ghcr.io/maxbolgarin/unread:latest \
#     unread bot run
#
#   # Ad-hoc CLI invocation:
#   docker run --rm -v unread_state:/root/.unread \
#     ghcr.io/maxbolgarin/unread:latest \
#     unread doctor
#
# Bare `docker run …/unread` prints `unread --help` (see CMD below).

FROM python:3.11-slim AS base

# System deps:
# - ffmpeg: audio/video transcoding for Whisper. Without it, the bot
#   can still serve files/URLs/YouTube text but voice/video uploads
#   fail with a friendly "ffmpeg required" message at request time.
# - libpango / libpangoft2: backs `weasyprint` for markdown → PDF
#   report rendering. Since v1.x, weasyprint is a base dep (not a
#   `[bot]` extra) so phone Telegram clients get PDF reports out of
#   the box. Operators who want `.md` instead set
#   `UNREAD_BOT_REPORT_FORMAT=md`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Install the project into the system site-packages. We don't use a
# venv here because the container's whole filesystem IS the venv.
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY unread ./unread
COPY presets ./presets

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# The bot writes reports, cache, and (after /upload_session) the
# user session into ~/.unread. Mounting this as a named volume keeps
# the data across container restarts.
VOLUME ["/root/.unread"]

# Default to printing help. Compose / `docker run` callers override
# with `command: ["unread", "bot", "run"]` or a one-shot CLI call.
CMD ["unread", "--help"]
