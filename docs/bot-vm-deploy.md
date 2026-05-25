# Installing `unread` and deploying the bot

Three supported install paths. All three share the same `unread` PyPI
package — only the runtime differs.

| # | Path | Best for | One-line install |
|---|---|---|---|
| **1** | **Local (uv)** | Laptop / dev / running the bot manually for testing | `uv tool install unread` |
| **2** | **Native VM (script + systemd)** | Always-on bot on a Linux VM, no Docker | `curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh \| bash` |
| **3** | **Docker** | Containerized bot OR ad-hoc CLI in a container | `docker compose -f docker-compose.bot.yml up -d` |

`unread` is a single PyPI package. Weasyprint (PDF report rendering)
is part of the base install since v1.x — no `[bot]` extras dance.
Reports default to PDF; set `UNREAD_BOT_REPORT_FORMAT=md` to skip the
render entirely.

---

## 1. Local install (uv)

For local development, your laptop, or running the bot in the
foreground for testing.

```bash
# One-time: install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install unread itself
uv tool install unread

# First-time setup wizard: AI key + Telegram creds + user session
unread init

# Run the bot in the foreground (Ctrl-C to stop)
export UNREAD_BOT_TOKEN=123:abc...       # from @BotFather
unread bot run
```

**Optional system deps** (CLI works without them, bot degrades
gracefully):

- `ffmpeg` — Whisper voice/video transcription. Without it, voice
  uploads / YouTube audio fallbacks fail at request time.
- `libpango` (Linux) or `pango` (macOS) — runtime dep for weasyprint
  PDF rendering. Without it, bot uploads `.md` instead of PDF.

```bash
# Linux (Debian/Ubuntu)
sudo apt-get install -y ffmpeg libpango-1.0-0 libpangoft2-1.0-0
# macOS
brew install ffmpeg pango
```

This path is also the right one for using the bare CLI (`unread analyze`,
`unread ask`, `unread dump`) — same install, no extra steps.

---

## 2. Native VM (script + systemd)

For a long-running bot on a Linux VM without Docker. The one-line
installer is idempotent and handles everything:

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh | bash
```

What it does:

1. **uv** — installs the single-binary uv (manages its own Python 3.11+).
2. **System deps** — `ffmpeg` + libpango via apt / dnf / pacman / brew.
3. **`uv tool install unread`** — isolated tool venv; `unread` on PATH.
4. **`unread init`** — interactive wizard for AI provider + Telegram
   credentials + user-session login (you'll get a phone code from
   Telegram). Stdin is reassigned to `/dev/tty` at the top of the
   script so this works under `curl | bash`.
5. **Bot token prompt** — paste the token from
   [@BotFather](https://t.me/botfather); appended to `~/.unread/.env`
   with mode `0600`.
6. **`systemd --user` service** — written to
   `~/.config/systemd/user/unread-bot.service`, enabled + started.
   Also runs `loginctl enable-linger $USER` so the service survives
   SSH disconnect (and auto-restarts on crash in 5s).

Re-run anytime — idempotent: skips uv/ffmpeg if present, keeps existing
`~/.unread/` config, only re-prompts for the bot token when missing.

**If `unread init` exits non-zero** (older PyPI releases had a wizard
that bailed in some env states), the script prints a clear hint and
exits. Run `unread init` directly in your terminal — that always works
— then re-run the installer to pick up at the bot-token step.

### Flags

```bash
# Wipe ~/.unread/ first (deletes session + reports + cache)
bash install-bot.sh --reset

# Skip the wizard (assume ~/.unread/.env is pre-provisioned via Ansible / SCP)
bash install-bot.sh --skip-init
```

### Day-to-day

```bash
systemctl --user status unread-bot
journalctl --user -u unread-bot -f      # tail logs
systemctl --user restart unread-bot     # after a config change
uv tool upgrade unread                  # pull the latest PyPI release
systemctl --user restart unread-bot     # ...and reload
```

The bot's data — reports, cache, secrets DB, Telegram session — lives
in `~/.unread/`. Back that directory up, you've backed up everything.

### Troubleshooting

**"Couldn't enable linger"** — the script needed sudo for the
`loginctl enable-linger $USER` step. Run it manually:
```bash
sudo loginctl enable-linger $USER
```
Without linger, the systemd service stops when you log out.

**Voice / video uploads fail** — `ffmpeg` not on PATH. The script
installs it; if it failed silently, install manually
(`sudo apt-get install ffmpeg` or distro equivalent), then
`systemctl --user restart unread-bot`.

**Reports come as `.md` instead of PDF** — libpango isn't installed
(weasyprint's native dep). Install it
(`sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0` on Debian)
and restart the service. The bot auto-detects and switches to PDF.

---

## 3. Docker

Pull a pre-built image from GHCR — no source checkout on the remote.
The image is generic: no `ENTRYPOINT`, the `CMD` defaults to
`unread --help`. Pass `command:` (compose) or `unread bot run`
(`docker run`) to launch the bot. The same image works for one-off
CLI commands too.

### Bot service via compose

The shipped `docker-compose.bot.yml` is what you want. From your
laptop, the `scripts/deploy-bot.sh` helper rsyncs it + your env file
to the VM and triggers `docker compose pull && up -d`:

```bash
cp .env.bot.example .env.bot
$EDITOR .env.bot                       # fill in API ids / openai / bot token
scripts/deploy-bot.sh deploy@my-vm     # ssh user@host, then docker compose up
```

Or directly on the VM, no laptop helper:

```bash
cp .env.bot.example .env.bot && $EDITOR .env.bot
docker compose -f docker-compose.bot.yml --env-file .env.bot pull
docker compose -f docker-compose.bot.yml --env-file .env.bot up -d
docker compose -f docker-compose.bot.yml logs -f
```

The compose file specifies `command: ["unread", "bot", "run"]` since
the image has no entrypoint.

### Bot service without compose

```bash
docker run -d --name unread-bot \
  -e TELEGRAM_API_ID=... -e TELEGRAM_API_HASH=... \
  -e OPENAI_API_KEY=... -e UNREAD_BOT_TOKEN=... \
  -v unread_state:/root/.unread \
  --restart unless-stopped \
  ghcr.io/maxbolgarin/unread:latest \
  unread bot run
```

### Ad-hoc CLI inside the container

The same image is a full `unread` CLI runtime — useful when you don't
want a local Python install but need a one-off `unread doctor` or
`unread analyze <url>`:

```bash
docker run --rm -v unread_state:/root/.unread \
  ghcr.io/maxbolgarin/unread:latest \
  unread doctor

docker run --rm -v unread_state:/root/.unread \
  ghcr.io/maxbolgarin/unread:latest \
  unread analyze https://example.com/article
```

### One-time GHCR setup

GitHub publishes new packages as **private** by default. Until you
flip this once, any `docker pull` has to authenticate.

1. Go to <https://github.com/users/maxbolgarin/packages/container/unread/settings>
   (change the username if you forked).
2. Scroll to **Danger zone → Change package visibility → Public**.
3. Confirm the package name.

After this, `docker compose pull` works with no `docker login`.

### Image tags

`.github/workflows/image.yml` publishes on every tag and `main` push:

| Trigger | Tags |
|---|---|
| `v1.4.2` tag | `:1.4.2`, `:1.4`, `:latest` |
| push to `main` | `:main` |
| `workflow_dispatch` from a tag | same as the tag |

Pin a version in `.env.bot`:

```
UNREAD_BOT_IMAGE=ghcr.io/maxbolgarin/unread:1.4.2
```

---

## Bootstrapping the Telegram user session

The first time a user-mode session is needed (to read `t.me/<chat>/<msg>`
links), the bot will reply with:

> I don't have your Telegram user session — send /upload_session

Two ways to provide it:

### Option A — `/upload_session` (no shell access required)

1. In Telegram, message the bot `/upload_session`.
2. Drag-and-drop your local `~/.unread/storage/session.sqlite` into the
   chat as a file (NOT a photo).

The bot validates the file, atomically replaces the session on the
volume, and re-derives the owner allowlist from it.

### Option B — SCP the session ahead of time

If you have one already, skip the in-chat upload by dropping the file
straight into the volume's mountpoint:

```bash
# Docker
ssh deploy@my-vm '
  docker compose -f docker-compose.bot.yml stop
  VOL=$(docker volume inspect unread-bot_unread_state --format "{{.Mountpoint}}")
  sudo mkdir -p "$VOL/storage"
  sudo chmod 700 "$VOL/storage"
'
scp ~/.unread/storage/session.sqlite \
    deploy@my-vm:/tmp/session.sqlite
ssh deploy@my-vm '
  VOL=$(docker volume inspect unread-bot_unread_state --format "{{.Mountpoint}}")
  sudo mv /tmp/session.sqlite "$VOL/storage/session.sqlite"
  sudo chmod 600 "$VOL/storage/session.sqlite"
  docker compose -f docker-compose.bot.yml start
'

# Native (systemd)
scp ~/.unread/storage/session.sqlite \
    deploy@my-vm:~/.unread/storage/session.sqlite
ssh deploy@my-vm 'systemctl --user restart unread-bot'
```
