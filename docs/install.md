# Installation, setup, and paths

← Back to [README](../README.md)

## One line, all platforms

```bash
uv tool install unread
```

`uv` provisions a private Python ≥ 3.11 environment for `unread`,
isolated from your system Python and any other tools. Don't have `uv`?

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify with `unread --version`. Upgrade later with `uv tool upgrade unread`.

## What works without extra dependencies

The default install handles **text, PDF, DOCX, images, web pages, and
YouTube captions** out of the box (extractors are bundled). The only
optional system dependency is `ffmpeg` — and only if you want **audio
or video transcription** (voice messages, video notes, podcasts,
recordings):

| OS | Install ffmpeg |
|---|---|
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Fedora / RHEL | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| Windows (Scoop) | `scoop install ffmpeg` |
| Windows (Chocolatey) | `choco install ffmpeg` |

Without `ffmpeg`, audio/video paths skip with a clear warning instead
of crashing — the rest of `unread` keeps working.

## Other install methods

```bash
# Bleeding-edge unreleased commits from GitHub:
uv tool install git+https://github.com/maxbolgarin/unread.git

# Editable / development install (source-linked, edits picked up live):
git clone https://github.com/maxbolgarin/unread.git
cd unread
uv sync --extra dev
uv tool install --editable .

# No global install — run from a cloned dir without putting anything on PATH:
uv run unread @somegroup
```

After install, `unread doctor` verifies the binary, dependencies, and
the install layout.

> **Tested on** macOS and Linux. Windows works for the non-Telegram
> paths (files, YouTube, websites) — Telegram itself is supported via
> Telethon but signal handling and a few file-path edge cases may
> differ. Please file issues at
> <https://github.com/maxbolgarin/unread/issues>.

## First-run setup

```bash
unread init
```

Four-step interactive wizard:

1. **Install folder** — `~/.unread/` (default), current directory, or
   a custom path. Recorded at `~/.unread/install.toml`.
2. **AI provider** — pick one and paste its key:
   - **openai** (default) — also backs Whisper, embeddings, and vision
     used for media enrichment (`--enrich=voice`/`videonote`/`video`/`image`)
     and `ask --semantic`.
   - **anthropic** (Claude), **google** (Gemini), **openrouter** (many
     models, one key), or **local** (Ollama / LM Studio / vLLM).

   Press Enter to skip — `dump`, `describe`, and `sync` work without
   any key; only `analyze` and `ask` need one.

   > **Capability gaps.** Whisper / embeddings / vision are OpenAI-only.
   > Pick Anthropic / Google / OpenRouter / local as your chat provider
   > and you can still add an OpenAI key alongside for those features.
   > Without one, they skip with a clear warning.
3. **Telegram** (optional) — `api_id` / `api_hash` from
   <https://my.telegram.org> → *API development tools*, then phone +
   code login. Skip if you only want YouTube / web / file analysis.
4. **Done.** Credentials persist in
   `~/.unread/storage/data.sqlite::secrets`. Re-run `unread init` later
   to fill in any step you skipped; only unsatisfied steps re-prompt.

**Non-interactive setup** (CI, scripts) — pre-populate `~/.unread/.env`:

```
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
OPENAI_API_KEY=sk-...
```

Then `unread init` skips wizard prompts and runs Telethon auth only.
`.env` values always win over anything in the secrets DB, so key
rotation is a one-line edit.

`unread doctor` verifies the install at any time. `unread tg login --force`
re-runs Telethon auth without touching keys.

> **Migrating from a cwd-relative install** (older versions wrote into
> the working directory)? Move `./.env`, `./config.toml`, `./storage/`,
> and `./reports/` into `~/.unread/` manually — or set
> `UNREAD_HOME=$(pwd)` to keep the cwd-relative layout.

## Where does `unread` read config and write data?

Everything lives under `~/.unread/`:

```
~/.unread/.env                          ← credentials (filled in by you)
~/.unread/config.toml                   ← models, pricing, tuning
~/.unread/storage/session.sqlite        ← Telegram session
~/.unread/storage/data.sqlite           ← chats, messages, cache, embeddings
~/.unread/storage/backups/              ← snapshots from `unread backup`
~/.unread/reports/{chat}[/{topic}]/analyze/{preset}-{stamp}.md   ← default report path
~/.unread/reports/{chat}/dump/dump-{stamp}.md                    ← default dump path
```

You can run `unread` from any directory — paths don't depend on cwd.
Override the install root for tests / multi-profile setups via the
`UNREAD_HOME` env var (e.g. `UNREAD_HOME=/srv/unread-shared`).
