# Telegram bot

← Back to [README](../README.md)

`unread bot run` is the same analysis pipeline as the CLI, exposed as
a self-hosted Telegram bot. You message your bot with something; the
bot replies with a Markdown report. The bot is **single-user** — only
one Telegram ID (yours) gets answered, everyone else is silently
dropped.

This page covers the user-facing surface. For end-to-end VM
deployment (GHCR image, docker-compose, session bootstrapping) see
[`bot-vm-deploy.md`](bot-vm-deploy.md).

## What you can send

| Input | Result |
|---|---|
| **File** — PDF, DOCX, audio (`.mp3` / `.m4a` / `.ogg` / `.opus` / `.flac`), video (`.mp4` / `.mov` / `.mkv`), images (`.png` / `.jpg`), Markdown / text / source code | Summarized via the same `unread <path>` pipeline. Audio + video go through Whisper, images through vision. |
| **Web URL** (any HTTP/HTTPS link) | Page is fetched + extracted (trafilatura), summarized with the `website` preset. |
| **YouTube URL** | Captions if the video has them, audio + Whisper if it doesn't. Citations become `t=SECONDS` deep links. |
| **Forwarded Telegram message** | Analyzes the forwarded content; if forwarded from a channel, offers a picker to also pull a day/week/month of that channel. |
| **`t.me/<chat>/<msg>` link** | Pulls the chat and analyzes. **Requires a Telegram user session installed via `/upload_session`** — the bot token alone can't read user chats. |
| **`@channel` ref** | Same as the `t.me/` form. Needs `/upload_session`. |

## Common use cases

A few of the moments where forwarding to the bot beats anything else:

- **Long voice message** — a friend sends a 12-minute voice. Forward it; the bot replies with a TL;DR in roughly the time it takes to put your phone down.
- **Podcast / lecture video** — drop a `.mp4` or YouTube URL, get the talk's main points without watching.
- **Recorded meeting** — `.mp4` from Zoom / Meet / Teams. The audio track is extracted, transcribed, summarized.
- **Suspicious link** — that "you have to see this" URL from a stranger. Forward it; the bot fetches, summarizes, tells you what it actually says without you clicking.
- **PDF you'd rather not read** — contract, paper, manual. Same drop-in.
- **Channel preview** — paste a `t.me/<channel>/<msg>` link and the bot summarizes the channel's last day / week / month before you decide whether to subscribe.

## Reply format

For each input, the bot:

1. Sends an inline **TL;DR** as a text message (the report's first section, lifted out for at-a-glance reading).
2. Attaches the **full Markdown report** as a `.md` document with a one-line caption:
   ```
   ✓ 23.4s | 1842↓ + 612↑ tok (1280 cached) | $0.0041
   ```
   Format: `✓ <elapsed>s | <prompt>↓ + <completion>↑ tok [(<cached>)] | $<cost>`. Cached-token count is shown only if the provider returned one; cost line is dropped when it's zero (e.g. a fully cache-hit re-run).

Citations in the report follow the same shape as the CLI: `[#42](t.me/…)` for Telegram, `[#754](youtube.com/watch?v=…&t=754s)` for YouTube, `[#7](file://…)` for local files, paragraph indices for web pages. See [`sources.md`](sources.md) for the full citation matrix.

## The confirm panel

When the bot receives something to analyze, it doesn't run immediately — it sends a small inline panel with a **▶ Run** button:

```
🎬 YouTube: https://youtu.be/dQw4w9WgXcQ
Preset: `video`
Mode: `auto`
[▶ Run]
```

Tap **Run** and the analysis starts. The panel is there so an accidentally-tapped Telegram link doesn't silently spend money. Per-run tuning happens through slash commands (`/preset`, `/lang`, `/enrich`, `/window`) and is **sticky** — set once, applies to every subsequent run in the same chat.

**Forwarded messages get a richer picker.** If you forward a message from a channel, the panel asks what to analyze:

- *This message / image / caption* — just the forwarded content
- *From this msg in channel* — open the source channel from the forwarded message as the start anchor
- *Channel · day* / *week* / *month* — pull a time-window of the source channel and summarize

**To skip the panel**, send `/confirm off` once. The bot will then run analyses immediately on receipt (the default before this gate was added). `/confirm on` puts it back.

## Slash commands

| Command | Effect |
|---|---|
| `/help` | Show the input list + this command list. |
| `/ping` | Health check — reply `pong`. |
| `/settings` | Show current sticky settings (preset, language, enrich, window) + their defaults. |
| `/preset <name>` | Sticky preset for this chat (e.g. `/preset digest`). Bare `/preset` clears the override. Names match the CLI: `summary`, `tldr`, `digest`, `highlights`, `quotes`, `links`, `action_items`, `decisions`, `questions`, `reactions`, `video`, `website`. |
| `/lang <code>` | Sticky report language (e.g. `/lang en`, `/lang ru`). Bare clears. |
| `/enrich <list\|all\|none>` | Sticky extra enrichments for Telegram chat analyses. `/enrich image,link` turns those two on; `/enrich all` enables every kind; `/enrich none` strips even the defaults. |
| `/window <day\|week\|month\|msg\|from_msg\|none>` | Sticky default time window for TG-chat analyses. |
| `/confirm on\|off` | Toggle the pre-run confirm panel (default: on). |
| `/upload_session` | Install your Telegram user session (one-time). The bot waits for you to send `~/.unread/storage/session.sqlite` as a Telegram document. |
| `/cancel` | Drop any pending `/upload_session` state. |

Sticky settings live in-memory per chat (`BotApp._chat_state`) and reset on bot restart by design — there's nothing about a single user worth persisting separately.

## Single-user mode

The bot answers exactly one Telegram ID:

- **At startup**, the bot probes `~/.unread/storage/session.sqlite` (the user session). If an authorized session is there, its owner's ID becomes the allowlist. Otherwise the bot falls back to `UNREAD_BOT_OWNER_ID` from the env, if set.
- If neither is set, the bot refuses to start handling events — there's no safe allowlist.
- After a successful `/upload_session`, the owner ID is re-derived from the just-installed session.
- Every event is filtered by Telethon's `from_users=` AND a defense-in-depth `sender_id` check inside the handler. Anything else is silently dropped — no acknowledgement, no log noise to the sender.

`/upload_session` is gated by the bootstrap allowlist so a fresh deploy with `UNREAD_BOT_OWNER_ID` set (but no session yet) lets only you upload the session; nobody else can install themselves as the bot's owner.

## Privacy & data flow

The bot machine holds the same `~/.unread/` directory the CLI would:
SQLite cache, generated reports, your API keys, your Telegram user
session (after `/upload_session`). API calls go to your chosen
provider (OpenAI, Anthropic, Gemini, OpenRouter, or a local
OpenAI-compatible endpoint). The only network endpoints are Telegram
servers, your AI provider, and any URLs you point the bot at.

If you self-host on a VM, treat the disk like the CLI's disk: snapshot
`~/.unread/storage/`, encrypt at rest, restrict access. See
[`security.md`](security.md) for the credential-storage options
(`keystore`, passphrase-derived `pass`) the CLI also supports — they
all work for the bot.

## Running it

**Locally** (foreground, useful for first-time setup and testing):

```bash
cp .env.bot.example .env.bot
# Edit .env.bot — at minimum:
#   UNREAD_BOT_TOKEN=...            (from @BotFather)
#   UNREAD_BOT_OWNER_ID=...         (your Telegram numeric ID; optional if a session is already mounted)
#   OPENAI_API_KEY=...              (or whichever provider you configured)
unread bot run
```

**On a server with docker-compose:**

```bash
docker compose -f docker-compose.bot.yml --env-file .env.bot up -d --build
```

**On a Linux VM, pulling a prebuilt image from GHCR** — the
zero-source-checkout flow with `scripts/deploy-bot.sh` and
`docker-compose.bot.prod.yml` — see
[`bot-vm-deploy.md`](bot-vm-deploy.md) for the full recipe.
