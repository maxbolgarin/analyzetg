# Deploying `unread bot` to a VM

Two supported install paths on a fresh Linux VM:

| Path | Best for | One-line install |
|---|---|---|
| **A. Native (systemd)** | Single-VM hobbyist / small ops; no Docker needed | `curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh \| bash` |
| **B. Docker** | Already running Docker; want image-pinned versions; bigger deploys | See "Docker deploy" below |

---

## A. Native install (systemd, no Docker)

For when you want the simplest possible setup: PyPI install, a
`systemd --user` service that auto-restarts on crash and survives
logout. No Docker daemon, no GHCR auth, no compose files.

### The one-liner

On the VM (as a non-root user with sudo):

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh | bash
```

What it does:

1. **System deps** — installs Python 3.11+ if missing, plus `ffmpeg`
   (apt / dnf / pacman / brew autodetected).
2. **`unread[bot]` via pipx** — isolated venv, won't conflict with
   system Python. Includes weasyprint so the bot ships PDF reports
   (falls back to `.md` if weasyprint can't import).
3. **`unread init`** runs interactively — walks you through AI
   provider (OpenAI / Anthropic / Google / OpenRouter), Telegram
   credentials (`api_id`/`api_hash` from my.telegram.org), and the
   user-session login (you'll get a phone code from Telegram).
4. **Bot token prompt** — paste the token from [@BotFather](https://t.me/botfather);
   it's appended to `~/.unread/.env`.
5. **`systemd --user` unit** — written to
   `~/.config/systemd/user/unread-bot.service`, enabled + started.
   Enables linger (`loginctl enable-linger $USER`) so the service
   keeps running after SSH disconnect.

Re-run anytime — idempotent: skips Python/ffmpeg if already installed,
keeps existing `~/.unread/` config, only re-prompts for the bot token
when missing.

### Flags

```bash
# Wipe ~/.unread/ first (deletes session + reports + cache)
bash install-bot.sh --reset

# Skip the wizard (assume ~/.unread/.env is already populated via SCP / Ansible)
bash install-bot.sh --skip-init
```

### Day-to-day

```bash
# Status
systemctl --user status unread-bot

# Tail logs
journalctl --user -u unread-bot -f

# Restart after a config change
systemctl --user restart unread-bot

# Stop
systemctl --user stop unread-bot

# Upgrade to the latest release on PyPI
pipx upgrade 'unread[bot]'
systemctl --user restart unread-bot
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

**"Can't locate the 'unread' binary"** — pipx put it in
`~/.local/bin/`, which isn't on PATH for the systemd shell. The
script tries `command -v unread` at install time and bakes the full
path into the unit file, so this should only happen if you moved the
binary after running the script. Re-run the script — it'll re-create
the unit with the new path.

**Voice / video uploads fail** — `ffmpeg` not on PATH. The script
installs it; if it failed silently, install manually
(`sudo apt-get install ffmpeg` or distro equivalent), then
`systemctl --user restart unread-bot`.

---

## B. Docker deploy

For Docker fans, or anyone wanting **image-pinned versions** via GHCR.

End-to-end recipe with **no source checkout on the remote** — the
image is built by GitHub Actions, pulled from GHCR; the VM only needs
a compose file, an env file, and a Docker daemon.

The three moving pieces:

1. **GitHub Actions builds + pushes the image** to GHCR on every tagged
   release (`.github/workflows/bot-image.yml`).
2. **`scripts/deploy-bot.sh`** rsync's the compose + env file from your
   laptop to the VM and triggers `docker compose pull && up -d`
   remotely. No git clone happens on the VM.
3. **`docker-compose.bot.prod.yml`** points at `ghcr.io/<you>/unread-bot`
   and only pulls — never builds — so the VM doesn't need the source.

---

## One-time setup

### 1. Publish the image (GitHub Actions)

The workflow at `.github/workflows/bot-image.yml` is already wired up.
First push to GHCR happens automatically the next time you:

- Push a `v*` tag (semantic-release does this on every shipped release), OR
- Push to `main`, OR
- Trigger it manually: **Actions → Bot Docker Image → Run workflow**.

Image tags produced:

| Trigger          | Tags                                             |
| ---------------- | ------------------------------------------------ |
| `v1.4.2` tag     | `:1.4.2`, `:1.4`, `:latest`                      |
| push to `main`   | `:main`                                          |
| `workflow_dispatch` from a tag | same as the tag                    |

### 2. Make the GHCR package public

GitHub publishes new packages as **private** by default. Until you flip
this once, any `docker pull` on the VM has to authenticate.

1. Go to <https://github.com/users/maxbolgarin/packages/container/unread-bot/settings>
   (change the username if you forked).
2. Scroll to **Danger zone → Change package visibility → Public**.
3. Confirm the package name.

After this, the VM's `docker compose pull` works with no `docker login`.

### 3. Prep the VM

Bare minimum on the VM:

```bash
# Docker Engine + Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # log out + back in to pick up the group

# Verify
docker --version
docker compose version
```

That's it. **No Python, no git, no source clone.** The deploy script
puts everything else in place.

### 4. Prep `.env.bot` on your laptop

```bash
cp .env.bot.example .env.bot
$EDITOR .env.bot
```

Fill in `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `OPENAI_API_KEY`,
`UNREAD_BOT_TOKEN`. The other env vars have sensible defaults — see
the comments in `.env.bot.example`.

---

## First deploy

From the repo root on your laptop:

```bash
scripts/deploy-bot.sh deploy@bot.example.com
```

That single command:

1. Creates `/srv/unread-bot/` on the VM (mode `0700`).
2. rsyncs `docker-compose.bot.prod.yml` → `/srv/unread-bot/docker-compose.yml`.
3. rsyncs `.env.bot` → `/srv/unread-bot/.env.bot` atomically (temp +
   rename), mode `0600`.
4. SSHes in and runs `docker compose pull && docker compose up -d &&
   docker compose ps` against the new files.

You should see the container come up. Tail logs to confirm:

```bash
ssh deploy@bot.example.com 'cd /srv/unread-bot && docker compose logs -f'
```

### Custom paths / ports

```bash
# Non-standard SSH port + custom remote dir
scripts/deploy-bot.sh deploy@bot.example.com:2222 /opt/unread-bot

# Use a different env file (e.g. .env.bot.staging)
ENV_FILE=.env.bot.staging scripts/deploy-bot.sh deploy@staging.example.com

# Push files only, don't restart (e.g. you want to edit before going live)
SKIP_RESTART=1 scripts/deploy-bot.sh deploy@bot.example.com
```

### Pinning to a specific version

Track `:latest` by default. To pin a known-good version, set in `.env.bot`:

```
UNREAD_BOT_IMAGE=ghcr.io/maxbolgarin/unread-bot:1.4.2
```

Then re-run the deploy script — `docker compose` will pull the pinned
tag instead of `:latest`.

---

## Updating

When a new release lands:

```bash
scripts/deploy-bot.sh deploy@bot.example.com
```

That's it. The same script is idempotent — it always re-pulls the image
(`pull_policy: always` is set in the compose file) and recreates the
container if the image hash changed. The `unread_state` named volume
preserves your reports, cache, secrets DB, and Telegram session across
the rolling update.

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
ssh deploy@bot.example.com '
  docker compose -f /srv/unread-bot/docker-compose.yml stop
  VOL=$(docker volume inspect unread-bot_unread_state --format "{{.Mountpoint}}")
  sudo mkdir -p "$VOL/storage"
  sudo chmod 700 "$VOL/storage"
'
scp ~/.unread/storage/session.sqlite \
    deploy@bot.example.com:/tmp/session.sqlite
ssh deploy@bot.example.com '
  VOL=$(docker volume inspect unread-bot_unread_state --format "{{.Mountpoint}}")
  sudo mv /tmp/session.sqlite "$VOL/storage/session.sqlite"
  sudo chmod 600 "$VOL/storage/session.sqlite"
  cd /srv/unread-bot && docker compose start
'
```

---

## Operations cheat sheet

All commands run on the VM after `cd /srv/unread-bot`.

| Action               | Command                                           |
| -------------------- | ------------------------------------------------- |
| Start                | `docker compose up -d`                            |
| Stop                 | `docker compose down`                             |
| Restart              | `docker compose restart`                          |
| Tail logs            | `docker compose logs -f`                          |
| Pull new image       | `docker compose pull && docker compose up -d`     |
| Show running state   | `docker compose ps`                               |
| Shell into container | `docker compose exec unread-bot bash`             |
| Inspect named volume | `docker volume inspect unread-bot_unread_state`   |
| Run `unread doctor`  | `docker compose exec unread-bot unread doctor`    |

---

## Troubleshooting

**`unauthorized: authentication required` on `docker compose pull`**

The GHCR package is still private. Either flip it public (see step 2
above) or `docker login ghcr.io -u <user> -p <PAT_with_read:packages>`
on the VM once.

**Bot exits with "no owner allowlist"**

There's no `UNREAD_BOT_OWNER_ID` set AND no authorized user session
mounted yet. Either fill in `UNREAD_BOT_OWNER_ID` in `.env.bot` (find
your numeric ID via [@userinfobot](https://t.me/userinfobot)) and
redeploy, or message the bot `/upload_session` and drop the file.

**"Pre-run confirm panel" never appears**

Someone set `/confirm off` in this chat. Send `/confirm on` to
re-enable.

**Image build is slow on first push to GHCR**

The Buildx GHA cache is empty on the first run. Subsequent builds
share the cache and only rebuild the layers that actually changed.
