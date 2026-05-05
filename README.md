# unread

[![CI](https://github.com/maxbolgarin/unread/actions/workflows/ci.yml/badge.svg)](https://github.com/maxbolgarin/unread/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/unread.svg)](https://pypi.org/project/unread/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Read less, know more.** A local CLI that pulls Telegram chats, YouTube
> videos, web articles, and local files into one searchable archive — and
> analyzes, queries, or dumps them through the AI provider you choose.

`unread` collapses the "I have too much to read" problem into three verbs:

- `unread <ref>` — **analyze**: produce a structured Markdown report (digest, action items, decisions, …) with clickable citations back to every source message.
- `unread ask <ref> "Q"` — **ask**: get a single-shot answer with citations from a chat archive, a video, an article, or a file.
- `unread dump <ref>` — **dump**: save the source verbatim (the original file, the chat history, the transcript) to `~/.unread/reports/`.

`<ref>` is **the same set of shapes** for all three commands: a Telegram
handle / link, a YouTube URL, a web URL, a local file, or stdin.

```bash
unread @somegroup --last-days 7              # weekly digest of a Telegram group
unread "https://youtu.be/jmzoJCn8evU"        # summarize a video
unread "https://paulgraham.com/greatwork"    # summarize an article
unread ./meeting.mp3                         # transcribe + analyze a recording
unread ask "what did Bob decide?" @somegroup
unread dump @somegroup -o history.md --last-days 30
```

## Why unread

- **One CLI, every source.** Telegram chats / topics / channels, YouTube, websites, PDFs, DOCX, audio (Whisper), video (audio extracted then transcribed), images (vision), and stdin — same flags, same caches, same report shape.
- **Bring your own model.** OpenAI, Anthropic (Claude), Google (Gemini), OpenRouter, or a local OpenAI-compatible server (Ollama, LM Studio, vLLM). Switch at any time with `unread settings`.
- **Local-first.** Everything lives in `~/.unread/` (SQLite for chats, embeddings, analysis cache, secrets). The only network calls are to Telegram, your AI provider, and any URLs you point at.
- **Cost-aware.** Per-call token + USD accounting, a `--max-cost` cap, two-layer caching (local content cache + provider prompt cache), and `unread stats` for spend reports. Re-running an unchanged chat is free; follow-ups are cheap.
- **Citation-grounded.** Every claim in a report links back to the message / paragraph / timestamp that supports it. `--cite-context` adds an audit block under each citation so you can verify without leaving the terminal.

## Quickstart (60 seconds)

```bash
# 1. Install (Python 3.11+; uv handles the venv and binary)
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
uv tool install unread

# 2. Set up — interactive wizard: install folder, AI provider key, optional Telegram login
unread init

# 3. Run something
unread "https://paulgraham.com/greatwork.html"         # any web page
unread @somegroup --last-days 7                        # last week of a chat
unread ask "what did Bob decide?" @somegroup           # Q&A
unread doctor                                          # verify the install
```

That's it. No virtualenv to activate, no system packages to manage,
no `pip` conflicts. `uv tool install` keeps `unread` and its deps in
their own isolated environment.

## Documentation

| Topic | Section |
|---|---|
| All command flags + defaults | [`unread <ref>` flags](#unread-ref--flags), [`ask`](#unread-ask--qa-over-any-source), [`dump`](#unread-dump--history-and-extracted-text-to-a-file) |
| Where files live | [Where does `unread` read config and write data?](#where-does-unread-read-config-and-write-data) |
| Credential storage | [Security](#security) |
| Telegram chat references | [Chat references](#chat-references) |
| YouTube + web pages | [YouTube videos](#youtube-videos), [Web pages](#web-pages) |
| Forum topics | [Forum chats (topics)](#forum-chats-topics) |
| Voice / image / link enrichment | [Media enrichment](#media-enrichment) |
| Output presets | [Presets](#presets) |
| Languages | [Language](#language) |
| Cost & caching | [Cost & caching](#cost--caching) |
| Backups, cleanup, watch | [Maintenance](#maintenance) |
| `config.toml` reference | [Configuration (`config.toml`)](#configuration-configtoml) |
| Debugging | [Troubleshooting](#troubleshooting) |
| Contributing | [Development](#development) |

---

## Installation

### One line, all platforms

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

### What works without extra dependencies

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

### Other install methods

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

`unread doctor` verifies the install at any time. `unread login --force`
re-runs Telethon auth without touching keys.

> **Migrating from a cwd-relative install** (older versions wrote into
> the working directory)? Move `./.env`, `./config.toml`, `./storage/`,
> and `./reports/` into `~/.unread/` manually — or set
> `UNREAD_HOME=$(pwd)` to keep the cwd-relative layout.

---

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

---

## Security

`unread` stores three classes of high-value data on disk: API keys
(OpenAI, Anthropic, Google, OpenRouter, Telegram api_id/api_hash), the
Telegram session (full-account auth — anyone with this file can log
in as you), and the cached chat content (messages, transcripts,
analysis reports). The defenses below address the realistic threats:
other users on the same machine, backup leakage (Time Machine /
iCloud / Dropbox / NAS), stolen disks without FDE, and Telegram
session theft.

### File permissions (always on)

`~/.unread/` is created mode `0o700` and every file written inside
(`data.sqlite`, `session.sqlite`, reports, `.env`) is tightened to
`0o600` immediately after creation. The `media/` and runtime cache
directories are also `0o700`. Verify with `unread doctor` — it flags
overpermissive modes, warns when the install lives under a known
cloud-sync folder (iCloud Drive, Dropbox, OneDrive, Google Drive),
and reports FileVault / LUKS state.

`unread`'s structured logger has an API-key redactor: anything
matching `sk-…`, `sk-ant-…`, `sk-or-…`, `AIza…`, `gsk_…`, or known
secret-shaped event-dict keys (`api_key`, `api_hash`, `passphrase`,
`session_string`, `auth_key`, …) gets masked before rendering. So
even if a debug session is shared, raw credentials don't leak.

### Three storage backends — `unread security`

The credential-storage backend is one-shot switchable:

```bash
unread security status               # active backend, slot inventory, FDE check
unread security set plain            # plaintext on disk (default)
unread security set keystore         # OS keychain (recommended)
unread security set pass             # passphrase-encrypted (strongest)
unread security set plain            # … and back, any direction
```

| Backend | Storage | Defends against | UX |
|---|---|---|---|
| `plain` | `~/.unread/storage/data.sqlite` (plaintext) | Other local users (via `0o700`/`0o600`) | Zero friction |
| `keystore` | macOS Keychain / Linux Secret Service / Windows Credential Manager | Other local users + backup leakage (Keychain isn't backed up) | Zero friction — unlocked with your login |
| `pass` | Same DB, but every value encrypted with a key derived from your passphrase. The Telegram session moves into an encrypted Telethon `StringSession` and the on-disk `session.sqlite[.session]` file disappears entirely. | All of the above + stolen disk without FDE + VPS host operator + Telegram session theft from a backup | Passphrase prompt; cache it for the shell session via `unread security unlock` |

#### `keystore` — the recommended default for personal machines

`unread security set keystore` migrates every saved API key into the
OS-native keychain. Verify on macOS with:

```bash
security find-generic-password -s unread -a openai.api_key
sqlite3 ~/.unread/storage/data.sqlite "SELECT key, length(value) FROM secrets"
# → DB rows are blank; values live in Keychain under service "unread"
```

No passphrase is ever asked — the keychain is unlocked when you log
in. Keychain content is encrypted at rest with a key bound to your
user account. Backups (Time Machine, iCloud) by default exclude the
Keychain database, so a leaked Time Machine snapshot of `~/.unread/`
no longer contains your API keys. On Linux, `keystore` requires a
running Secret Service (`gnome-keyring` / KWallet); on headless
hosts the wizard skips this offer silently and you stay on `plain`.

#### `pass` — passphrase-encrypted, strongest at-rest guarantee

`unread security set pass` runs an interactive prompt: pick a
passphrase, the CLI runs `Scrypt` (n=2¹⁷, ~100 ms) to derive a key,
and re-encrypts every secret value plus the Telegram session string
under `ChaCha20Poly1305`. The plaintext `session.sqlite[.session]`
file is removed at the end — there's nothing on disk an attacker
can copy to impersonate you on Telegram, even from a backup.

**On every command** that reads encrypted secrets, the key is
sourced in this order: in-process cache → `UNREAD_PASSPHRASE` env
var (handy for cron / CI) → `getpass()` prompt (TTY only). To skip
the prompt across invocations:

```bash
unread security unlock              # cache the derived key until you `lock`
unread security unlock --keep 30m   # … or for a bounded TTL
unread chats run                    # no prompt
unread security lock                # wipe the cache now
unread security rotate-passphrase   # change the passphrase
```

The cached key lives at `$XDG_RUNTIME_DIR/unread/key` on Linux
(tmpfs — auto-cleared on reboot) or `~/.unread/.runtime/key` on
macOS / fallback. Mode is `0o600` from creation. The passphrase
itself is **never** persisted — only the derived key, only when you
explicitly `unlock`.

What encrypted mode does NOT defend against: malware running as
your user (same UID can read decrypted process memory regardless),
or a coerced-passphrase attack. For both, the mitigation is at the
OS level (FileVault, app sandboxing, hardware tokens), not at
`unread`'s layer.

### Telegram session hygiene

```bash
unread security revoke-session
```

Removes the local Telethon session file and prints a reminder to
revoke remotely from Telegram → Settings → Devices → Active Sessions.
Doing both is the only way to fully invalidate a leaked session.

### Quick recommendations

- **Personal Mac / Windows machine:** `unread security set keystore`. Zero friction, defends backup leakage, fits the realistic threat model.
- **VPS / shared host / paranoid laptop with no FDE:** `unread security set pass`, optionally `unread security unlock --keep 1h` per shell.
- **Headless Linux / Docker / CI:** stay on `plain`, set `UNREAD_PASSPHRASE` only if you've also enabled `pass` mode and need automation.
- **Anywhere:** turn on FileVault / LUKS, exclude `~/.unread/` from `tmutil`/cloud sync, run `unread doctor` after first setup.

### Privacy: PII redaction before the LLM

`--redact` (or `analyze.redact = true` in config) scrubs PII from the
text sent to the LLM provider, while keeping originals in the local
DB and the saved Markdown report. Only the API payload is redacted.

```bash
unread @somegroup --redact
```

Patterns scrubbed: phone numbers (E.164 with `+` prefix), emails,
IBANs, and Luhn-valid credit-card numbers. Each match is replaced
with `[redacted-phone]` / `[redacted-email]` / `[redacted-iban]` /
`[redacted-card]`, and the run summary shows per-kind counts so you
can see what was filtered. Caching honors the flag — toggling
`--redact` produces a different cache row, so you never serve a
non-redacted cached result on a redacted run (or vice versa).

The match is intentionally conservative (regex with strict word
boundaries) to keep false positives low. SHA hashes and order-id
numerics are not flagged; non-E.164 phone shapes (raw 10-digit US
numbers without `+1` prefix) pass through. If you need stricter
redaction, layer your own preset prompt that asks the LLM to
generalize personal references — `--redact` complements that, it
doesn't replace it.

---

## Command reference

`unread --help` shows four panels.

### Main (everyday)

| Command | Purpose |
|---|---|
| `unread [<ref>] [flags]` | Analyze a chat (default action). No args → interactive wizard. |
| `unread tg [<ref>] [flags]` / `unread telegram [<ref>] [flags]` | Same as the bare form, but auto-runs `init` if no Telegram session exists. |
| `unread init [--force]` | Full interactive setup: install folder, AI provider + key, optional Telegram login. `--force` wipes the saved session before logging in. |
| `unread help [<cmd>]` / `unread --help` | Show top-level help (no args) or walk into a subcommand: `unread help tg`, `unread help init`. |
| `unread ask [<ref>] ["question"] [flags]` | Q&A over any ref (Telegram, YouTube, website, local file, stdin) — no Telegram round-trip for non-TG sources. No args opens a wizard. |
| `unread dump [<ref>] [flags]` | Dump history / extracted text to md/jsonl/csv. Accepts Telegram refs, URLs, local files, and stdin. No OpenAI call by default. |

> **Subcommand-name collisions.** `unread <ref>` will route to a
> subcommand if `<ref>` matches one (e.g. `unread settings` opens the
> settings command, not a chat literally titled "settings"). Use
> `unread tg "settings"` or `unread -- settings` for the rare case of
> a chat that shares a subcommand name.

### Telegram

| Command | Purpose |
|---|---|
| `unread tg describe [<ref>]` | List dialogs (no ref) or inspect one chat. Shows folder column. |
| `unread describe folders` | List your Telegram folders (use with `--folder NAME`). |
| `unread login [--force]` | Re-run the Telegram-only login step. `--force` wipes the saved session first. |
| `unread logout` | Remove the local Telegram session. |
| `unread chats add/list/enable/disable/remove` | Manage subscriptions. Optional — one-off `analyze` already fetches. |
| `unread sync` | Pull new messages for every active subscription. |

### Maintenance

| Command | Purpose |
|---|---|
| `unread stats [--by …]` | Token spend / cache hit rate — by chat, preset, model, day, kind. |
| `unread cache <entity> [ls\|purge\|stats\|show\|export]` | Cache maintenance with three entity groups (`<entity>` ∈ `ai` \| `sources` \| `tg`). Bare `cache <entity>` = `ls`. See per-entity table below. |
| `unread doctor` | Preflight check — Telegram session, OpenAI key, ffmpeg, DB integrity, pricing, FDE, cloud-sync warnings. |
| `unread update [--check] [-y]` | Check PyPI for a newer release and (optionally) install it. |
| `unread security {status,set,unlock,lock,rotate-passphrase,revoke-session}` | Inspect / switch the credential-storage backend. See the [Security](#security) section. |
| `unread backup up [out]` | Snapshot `storage/data.sqlite` via `VACUUM INTO`. |
| `unread backup restore <file>` | Replace `data.sqlite` with a backup (current DB moved aside). |
| `unread reports prune --older-than 30d` | Move stale report files to `reports/.trash/`. |
| `unread watch --interval 1h <inner cmd>` | Run an inner `unread` command on a fixed cadence. |

### Hidden (still callable, not in `--help`)

`unread download-media [<ref>]` — kept for back-compat. Use `unread dump --save-media` instead.

---

## Chat references

`<ref>` accepts:

| Form | Example |
|---|---|
| `@username` | `@durov` |
| `https://t.me/…` | `https://t.me/durov/123` (jumps to message 123) |
| Forum-topic link | `https://t.me/somegroup/100/5000` (topic 100, msg 5000) |
| Private link | `https://t.me/c/1234567890/5000` |
| Invite link | `https://t.me/+AbCdEf...` (add `--join` to join it) |
| Numeric `chat_id` | `-1001234567890` or `1001234567890` |
| YouTube URL | `https://www.youtube.com/watch?v=...` (see [YouTube videos](#youtube-videos)) |
| Website URL | `https://example.com/article` (see [Web pages](#web-pages)) |
| Local file path | `./report.pdf`, `~/notes.md`, `/tmp/recording.mp3` (see [Local files](#local-files)) |
| `-` | Read from stdin: `cat notes.txt \| unread` or `unread - < notes.txt` |

**Fuzzy chat-title match** (`"Bull Trading"` → substring search across your dialogs)
is intentionally NOT a bare-`unread <ref>` form — bare `unread "some text"` would
silently try to authenticate with Telegram for every typo. Use `unread tg "Bull
Trading"` instead, or pick from the wizard via `unread` with no args.

The wizard's chat picker accepts non-Latin type-to-filter (Cyrillic,
Greek, Arabic, Hebrew, Latin Extended) so searching for `биохакинг` or
`finanças` works the same as `crypto`.

### YouTube videos

`unread <youtube-url>` analyzes a single video end-to-end. Flow:

1. yt-dlp fetches metadata (title, channel, duration, captions index).
2. A summary panel shows up + an interactive picker asks for the
   transcript source — captions (free), audio + Whisper (paid, with a
   cost estimate), or cancel. Skipped when stdin isn't a TTY, when
   `--yes` is passed, or when an explicit `--youtube-source` flag was set.
3. Captions are fetched as VTT (or audio is downloaded → Whisper), and
   each cue's start-second becomes that segment's `msg_id`.
4. The bundled `video` preset runs over the time-stamped synthetic
   messages. Citations land as `[#754](https://www.youtube.com/watch?v=ID&t=754s)`
   — every citation in the report is a clickable jump to that moment.
5. Re-runs hit the `youtube_videos` cache (metadata + transcript +
   timed cues) — no yt-dlp, no Whisper, no LLM-side re-spend if cached.

```bash
# Interactive default: shows metadata + asks for source.
unread "https://www.youtube.com/watch?v=jmzoJCn8evU"

# Scripted (skip prompts, auto-pick captions / Whisper as needed):
unread "https://youtu.be/dQw4w9WgXcQ" --yes

# Force Whisper (slower; ~$0.003/min):
unread "https://youtu.be/dQw4w9WgXcQ" --youtube-source audio

# Different preset; see `unread --help` for the full list.
unread "https://www.youtube.com/watch?v=..." --preset summary --console
```

Reports land under `reports/youtube/<channel-slug>/<video-slug>-<preset>-<ts>.md`.
Default preset for YouTube is `video` (system prompt tuned for transcripts,
time-stamped citations).

Telegram videos / video-circles (single-message mode) auto-flag as
`source_kind="video"` too: `=== Video: <title> ===` in the preamble and
the LLM is told it's analyzing a video transcript, not a chat snippet.

Supported URL shapes: `youtube.com/watch?v=…` (with arbitrary `&list=`,
`start_radio`, `t=` params, all stripped), `youtu.be/`, `youtube.com/shorts/`,
`youtube.com/embed/`, `youtube.com/live/`, `m.youtube.com`, `music.youtube.com`.
Playlist-only and channel-only links are rejected with a clean error
(playlist support is on the roadmap).

Telegram-only flags (`--folder`, `--thread`, `--all-flat`, `--all-per-topic`,
`--with-comments`, `--from-msg`, `--full-history`, `--since/--until/--last-days/--last-hours`,
`--msg`, `--repeat-last`, `--mark-read`) are rejected for YouTube refs with
a clear error.

`unread doctor` warns if `yt-dlp` isn't installed.

### Web pages

`unread <url>` analyzes any HTTP/HTTPS web page (article, blog
post, documentation, essay) end-to-end. Auto-detected from the URL
shape: anything that isn't a YouTube link or a Telegram link
(`t.me/...`) routes here. No flag needed.

Flow:

1. **HTTP fetch** — `httpx` GET with a browser-shaped User-Agent and a
   30-second timeout. 4xx/5xx, non-HTML responses, and oversize pages
   (>5 MB raw HTML, configurable) error out with a clean message.
2. **Article extraction** — primary extractor is
   [`trafilatura`](https://github.com/adbar/trafilatura) (best-in-class
   article-body detection: drops nav / sidebar / footer / cookie
   banners, preserves headings + lists). Falls back to a BeautifulSoup
   pipeline (semantic-tag pass, then whole-body `get_text` if the page
   has only `<div>`/`<span>`).
3. **Segmentation** — extracted text is split into paragraph-shaped
   chunks (≤3500 chars each, preferring blank-line boundaries). The
   metadata block (title, site, author, publish date, word count, URL)
   becomes synthetic message `#0`; paragraphs are `#1..#N`.
4. **Analysis** — the bundled `website` preset (system prompt tuned for
   single-author article body, not a chat conversation) runs over the
   synthetic messages. Citations land as `[#7](https://example.com/article)`
   — clicking a citation opens the page itself (paragraph anchors aren't
   exposed because most sites don't generate stable ones).
5. **Cache** — re-runs on the same URL hit the `website_pages` cache
   (metadata + paragraphs + content hash). The analysis cache key
   includes the **content hash**, so re-running after a page edit
   produces a cache miss while an unchanged re-fetch reuses the
   previous run.

```bash
# Default — fetch, extract, run the website preset, save under reports/website/...
unread "https://www.paulgraham.com/greatwork.html"

# Estimate-and-exit (no LLM call):
unread "https://example.com/blog/post" --dry-run

# Render to terminal instead of saving a file:
unread "https://example.com/blog/post" --console

# Different preset — `summary`, `digest`, `highlights`, etc. all work:
unread "https://example.com/blog/post" --preset summary

# Cost-bounded run + post the analysis to your Saved Messages:
unread "https://example.com/blog/post" --max-cost 0.05 --post-saved

# Run a custom prompt against a page:
unread "https://example.com/paper.html" --preset custom --prompt-file my-prompt.md
```

Reports land under `reports/website/<domain-slug>/<title-slug>-<preset>-<ts>.md`.
Default preset is `website` (Russian translation in `presets/ru/website.md`).

URL normalization for cache keying strips fragments + common tracking
params (`utm_*`, `fbclid`, `gclid`, `mc_cid`, `ref`, …) so the same
article shared with different referrer tags hits the same cache row.

Telegram-only flags are rejected for website URLs with a clear error
(same list as YouTube, plus `--cite-context` since web pages have no
surrounding-context store to expand into).

**Limitation: JS-rendered SPAs**. unread fetches raw HTML only — no
headless browser, no JS engine. Single-page apps (React / Angular /
Vue / Svelte sites that paint content client-side) typically serve
~1–5 KB of bootstrapping markup with no readable text. Those URLs
fail with a clear "appears to be a JavaScript-rendered single-page
app" error, suggesting you try a static article URL or paste the
content elsewhere. Most blogs, news sites, docs, and Markdown-rendered
pages work fine — it's specifically the SPA case that doesn't.

Configuration knobs (under `[website]` in `config.toml`):

```toml
[website]
# fetch_timeout_sec = 30
# max_html_bytes = 5_000_000              # 5 MB hard cap on raw HTML
# max_paragraphs = 400                    # post-split cap; rejects pathological pages
# user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/..."
```

### Local files

`unread <path>` analyzes any local file. Auto-detected from the ref shape
— anything that starts with `./`, `../`, `/`, `~/`, or `file://`, plus
bare filenames that match an existing file in the current directory,
routes to the file analyzer instead of being interpreted as a chat
title.

```bash
unread ./report.pdf                  # PDF (text-extracted via pypdf)
unread ./contract.docx               # DOCX (python-docx)
unread ./meeting-notes.md            # Markdown / plain text / source code
unread ./recording.mp3               # Audio (Whisper transcription)
unread ./standup.mp4                 # Video (ffmpeg → audio → Whisper)
unread ./diagram.png                 # Image (vision description, then summary)
cat notes.txt | unread                # Stdin (auto-detect when piped)
unread - < notes.txt                  # Stdin (explicit `-` form)
unread ./README.md --no-save          # Console-only, don't write a report file
```

Supported types:

| Kind | Extensions | What happens |
|---|---|---|
| Text / code | `.txt`, `.md`, `.csv`, `.json`, `.py`, `.js`, `.ts`, `.go`, `.rs`, `.html`, `.xml`, `.yaml`, `.toml`, `.sh`, `.sql`, `.tex`, … | Read as UTF-8 (with CP1251 / Latin-1 fallbacks) and analyzed as a document. |
| PDF | `.pdf` | `pypdf` extracts text page-by-page (200k-char cap). Scanned/image-only PDFs error out with an OCR hint. |
| DOCX | `.docx` | `python-docx` extracts paragraphs. |
| Audio | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus` | Transcribed via Whisper (needs `OPENAI_API_KEY`). |
| Video | `.mp4`, `.mov`, `.mkv`, `.webm` | ffmpeg pulls the audio track, Whisper transcribes (needs `ffmpeg` + OpenAI). |
| Image | `.png`, `.jpg`, `.webp`, `.gif` | Vision model describes the image, then analyzed as text (needs OpenAI). |
| Stdin | `-` or piped | Treated as plain text. |

Reports land at `~/.unread/reports/files/<kind>/<file-slug>-<preset>-<stamp>.md`. Citations
in the report use `file://` URIs (`[#7](file:///abs/path)`) so clicking
re-opens the source. Re-running on an unchanged file hits
`local_files` cache → no LLM cost.

Flags that only make sense for Telegram chats (`--folder`, `--thread`,
`--all-flat`, etc.) are rejected with a clear error when given a file
or stdin ref.

---

## `unread <ref>` — flags

```bash
unread [<ref>] [period] [output] [enrichment] [budget] [audit] [delivery]
```

### Period (start point of the analysis window)

| Flag | Meaning |
|---|---|
| `--full-history` | Whole chat |
| `--from-msg <id>` / message link | Start at a specific message, inclusive |
| `--since YYYY-MM-DD` / `--until YYYY-MM-DD` / `--last-days N` / `--last-hours N` | Date / hour range (UTC) |
| _(none)_ | Unread only — `msg_id > read_marker` |

Precedence (first match wins): `--full-history` > `--from-msg` > `--last-hours` > `--since/--until/--last-days` > unread. (When both `--last-hours` and `--last-days` are passed, the hour-granular flag wins.)

### Output

| Flag | Meaning |
|---|---|
| `-o <path>` / `--output` | Custom output path (file for single chat, dir for batch) |
| `-c` / `--console` | Render to terminal as Rich-styled markdown |
| `-s` / `--save` | Skip the wizard's output picker; save to default path |

### Enrichment

| Flag | Meaning |
|---|---|
| `--enrich=voice,image,link` | Enable a specific subset for this run |
| `--enrich-all` | Every kind (voice, videonote, video, image, doc, link) |
| `--no-enrich` | Disable everything, even config defaults |
| `--include-transcripts/--text-only` | Include enrichment text in the analyzable body |

### Forum routing

| Flag | Meaning |
|---|---|
| `--thread N` | One specific topic |
| `--all-flat` | Whole forum as one analysis (defaults to per-topic unread; honors `--last-days` / `--since` / `--full-history`) |
| `--all-per-topic` | One report per topic |

### Cost / safety

| Flag | Meaning |
|---|---|
| `--max-cost N` | Estimate cost upfront; abort or confirm if over budget. Pass `--yes` to abort silently. |
| `--dry-run` | Resolve, backfill, count, print the cost band, exit before any LLM call. |
| `--no-cache` | Don't read or write `analysis_cache` (forces a fresh run). |

### Audit / quality

| Flag | Meaning |
|---|---|
| `--cite-context N` | Append a `## Источники` section to the report with N messages of context around every cited `[#msg_id](url)`. Capped at 30 citations. |
| `--self-check` | Run a cheap-model verifier pass; appends a `## Verification` section listing unsupported claims. |
| `--by <sender>` | Filter to one sender. Substring match on `sender_name` (case-insensitive) or numeric `sender_id`. |

### Delivery

| Flag | Meaning |
|---|---|
| `--mark-read` / `--no-mark-read` | Tri-state. Without flag → prompt interactively. |
| `--post-saved` | Send the result to your Telegram Saved Messages (split into 4000-char chunks). |
| `--post-to <ref>` | Generalization — post to any chat (`me` for Saved Messages, `@channel`, etc.). |

### Workflow shortcuts

| Flag | Meaning |
|---|---|
| `--folder NAME` | Without `<ref>`: batch-analyze every chat in this Telegram folder with unread messages. |
| `--repeat-last` | Reuse the saved flags from the last successful analyze on `<ref>`. Explicit CLI flags still win. |
| `--preset NAME` / `--prompt-file path.md` | Pick a preset; `custom` + `--prompt-file` for ad-hoc. |
| `--with-comments` | For a Telegram channel: also pull messages from its **linked discussion group** (comments) over the same period and run them through the same enrichment toggles. The report renders channel posts and comments as two sections with their own citation links. The wizard asks interactively when the picked chat is a channel with a linked group. Available on `analyze`, `ask`, and `dump`. |
| `--model M` / `--filter-model M` | Override per-run model picks. |
| `--min-msg-chars N` | Drop messages shorter than N chars (after enrichment). |
| `--yes` / `-y` | Skip interactive confirmations (per-topic Y/n, batch Y/n, over-budget Y/n). |

### What you get back

A Markdown file at `reports/{chat}[/{topic}]/analyze/{preset}-{stamp}.md`
by default. Every cited claim is a clickable link back to the source:

```markdown
Фонды переходят на индексные структуры с 2026 Q1. [#1586](https://t.me/c/3865481227/584/1586)
```

---

## `unread ask` — Q&A over any source

```bash
unread ask "what did we decide about the migration?" @somegroup
unread ask https://youtu.be/dQw4w9WgXcQ "what is the main argument?"
unread ask ./notes.pdf "what are the action items?"
unread ask                                                 # opens the wizard
```

For **Telegram refs**, reads only your **local DB** — no Telegram
round-trip during retrieval. The corpus is everything `analyze` /
`dump` / `sync` has already pulled (transcripts, image descriptions,
doc extracts, link summaries included).

For **non-Telegram refs** (YouTube URLs, website URLs, local files,
stdin), `ask` extracts the source text directly — no Telegram
client is opened.

**Synopsis**: `unread ask [<ref>] ["QUESTION"] [flags]`. The positional
`<ref>` accepts any Telegram chat reference (`@user`, `t.me` link, topic URL,
fuzzy title, numeric id) **or** any non-Telegram ref (YouTube URL,
website URL, local file path, `-` / piped stdin). A topic URL like
`https://t.me/c/1234567890/4` auto-fills `--thread`. Without
`<ref>` / `--chat` / `--folder` / `--global` the command opens the
wizard (chat picker → period → enrich → confirm → backfill → answer);
without a question, the wizard prompts for it inline. The four scope
sources are mutually exclusive — pick one per call.

### Pipeline

1. **Tokenize** the question — bilingual (English + Russian) stop-word filter, drops short tokens.
2. **Retrieve** top-N messages by keyword `LIKE` over `text || transcript`. Default pool 500 with rerank, or 200 without.
3. **(Optional)** Rerank: cheap model rates each candidate 1–5 against the question; keep top-K (default 50). Drops ask cost ~5–10× on media-heavy chats.
4. **(Optional)** Semantic: `text-embedding-3-small` cosine over a precomputed index; composes with rerank.
5. **Format** with the same dense-line formatter analyze uses, group by chat title for cross-chat answers.
6. **Ask** the flagship model with a Q&A system prompt that mandates `[#msg_id](link)` citations.
7. Print to terminal (default) or save to `-o file.md`.

### Flags

| Flag | Meaning |
|---|---|
| `<ref>` (positional) | Restrict to one chat — `@user`, `t.me` link, topic URL (auto-fills `--thread`), fuzzy title, numeric id. Mutually exclusive with `--chat` / `--folder` / `--global`. |
| `--chat <ref>` | Same as positional `<ref>` but explicit. |
| `--folder NAME` | Restrict to chats in this Telegram folder. |
| `--global` / `-g` | Search every synced chat in the local DB (no wizard, no Telegram calls). The pre-wizard default. |
| `--thread N` | Restrict to a forum topic (used with `--chat` / `<ref>`; topic URLs auto-fill this). |
| `--since/--until/--last-days` | Date filter. |
| `--limit N` | Max messages to retrieve (default 200; bumped to 500 when rerank is on). |
| `--rerank/--no-rerank` | Two-stage retrieval (default on; toggled in `[ask]` config). |
| `--semantic` | Use precomputed embeddings instead of keyword retrieval. Requires `--build-index` once. |
| `--build-index` | Embed every body-bearing message in the scoped chat(s). Idempotent. |
| `--refresh` | Backfill new messages from Telegram before retrieval. Requires `--chat` or `--folder`. |
| `--show-retrieved` | Print the retrieved messages with scores before the LLM call (debug). |
| `--no-followup` | Skip the post-answer "Continue chatting?" prompt (cron / scripts / non-interactive). |
| `--max-cost N` | Abort if the estimated USD cost exceeds N. |
| `--model M` | Override the answering model. |
| `--enrich=voice,image,link` / `--enrich-all` / `--no-enrich` | Run media enrichment (transcripts, image descriptions, link summaries, …) over the scoped chats + period BEFORE retrieval. Same flag shape as `analyze`. The wizard offers an enrich step too. |
| `-o <path>` / `--console` | Save to file / force terminal render. |

After every answer the CLI prompts `Continue chatting? [y/N]` (default
`n`). Press `y` to drop into multi-turn follow-ups (each new question
sees prior turns as message history); press Enter to exit. Pass
`--no-followup` to suppress the prompt entirely.

### Examples

```bash
# No args — opens the wizard (asks for the question, then chat → period → confirm):
unread ask

# Positional ref — username:
unread ask "what did Bob say about migration?" @somegroup

# Positional ref — topic URL (thread auto-filled):
unread ask "open questions on the API" https://t.me/c/1234567890/4

# Across every synced chat (no wizard):
unread ask "когда дедлайн по проекту?" --global --last-days 7

# Folder scope, semantic retrieval (build index first):
unread ask "..." --folder Work --build-index
unread ask "open questions on the API" --folder Work --semantic --rerank --last-days 14

# Cheap and small:
unread ask "..." --limit 50 --model gpt-5.4-nano

# Debug retrieval before paying for the answer:
unread ask "..." @somegroup --show-retrieved --max-cost 0.05

# Single answer, no follow-up prompt (script-friendly):
unread ask "..." @somegroup --no-followup
```

### Ask over non-Telegram sources

`unread ask <ref> "QUESTION"` accepts the same set of ref shapes as
`unread <ref>` and `unread dump <ref>`. The pre-Telegram dispatch picks
a source-specific extractor based on the ref shape, so a YouTube ask
never opens a Telegram client.

```bash
unread ask https://youtu.be/dQw4w9WgXcQ "what's the main argument?"
unread ask https://example.com/article "summarize the methodology"
unread ask ./notes.pdf "what are the action items?"
echo "raw text…" | unread ask "what is this about?"
```

For documents under the `[ask].doc_full_text_cutoff_tokens` threshold
(default 32k tokens), `ask` sends the full extracted text to the model
in one call. Above the cutoff, `ask` falls back to chunked retrieval —
the same machinery used for chat-archive queries. Tune the cutoff by
setting `doc_full_text_cutoff_tokens` under `[ask]` in
`~/.unread/config.toml`.

`--chat`, `--folder`, and `--global` are rejected when the ref is a
URL/file/stdin — a doc ref already names the source.

### Cost feel

- **Retrieval**: free (local SQL).
- **Rerank** (default on): ~10 cheap-model calls × ~1k tokens each ≈ $0.005 per question.
- **Answer**: scales with `--limit`. With rerank+keep=50 and `gpt-5.4-mini`, typical cost is **~$0.01–0.05 per question**.

Cost is logged under `phase=ask` in `usage_log` — see `unread stats --by kind`.

---

## `unread dump` — history and extracted text to a file

No OpenAI call by default. Accepts the same set of ref shapes as
`unread <ref>`: Telegram refs, YouTube URLs, website URLs, local files,
and stdin. For Telegram refs, the same backfill + filter pipeline as
`analyze` is used, just writing raw messages instead of an analysis.

```bash
unread dump @somegroup -o history.md --last-days 30
unread dump @somegroup --format jsonl --with-transcribe -o dump.jsonl
unread dump @somegroup --save-media           # also save raw media files alongside
unread dump --folder Work                     # batch-dump every unread chat in folder
```

| Flag | Meaning |
|---|---|
| `--format md/jsonl/csv` | Output format (default `md`). |
| `--with-transcribe` | Run the audio enricher before writing (legacy alias for `--enrich=voice,videonote`). |
| `--enrich=...` / `--enrich-all` / `--no-enrich` | Same enrichment flags as `analyze`. |
| `--save-media [--save-media-types ...]` | Also save raw media files next to the dump. |
| `--folder NAME` | Without `<ref>`: batch-dump every unread chat in folder. |
| All period / forum / output / `--mark-read` flags | Same as `analyze`. |

### `dump` for websites and YouTube

`unread dump <url>` works for any HTTP(S) page or YouTube link too. No
Telegram credentials needed — the URL is fetched and saved straight to
`~/.unread/reports/`. Pick the artifact via `--mode` (or let the
interactive picker choose on a TTY):

| URL kind | Mode | What you get |
|---|---|---|
| Website  | `text` | `article.md` — readable text only. |
| Website  | `full` | `article.md` + `_files/img-N.<ext>` — text plus every inlined image. Cap with `--max-images N` (default 50). |
| YouTube  | `transcript` | `metadata.json` + `transcript.md` (cue-tagged when captions are available). Honors `--youtube-source auto\|captions\|audio`. |
| YouTube  | `audio` | `metadata.json` + `audio.mp3`. Needs ffmpeg. |
| YouTube  | `video` | `metadata.json` + `video.mp4` (or `.mkv`/`.webm` fallback). Needs ffmpeg. |

```bash
unread dump https://example.com/article --mode=text
unread dump https://example.com/article --mode=full --max-images 20
unread dump https://youtu.be/<id> --mode=transcript
unread dump https://youtu.be/<id> --mode=audio
unread dump https://youtu.be/<id> --mode=video
```

`--mode` is required in non-TTY runs (CI, piped stdin); the interactive
picker only fires on a real terminal. Telegram-only flags
(`--folder`, `--since`, `--from-msg`, `--save-media`, `--mark-read`,
…) are rejected with a clear error when used against a URL.

### `dump` for local files and stdin

`unread dump <path>` saves a byte-for-byte copy of the file under
`~/.unread/reports/files/<kind>/<original-name>-<stamp>.<original-ext>`.
The original extension is preserved — a `.ts` file lands as `.ts`,
a PDF as `.pdf`, an audio file as `.mp3`. No extraction, no markdown
wrap, no LLM call:

```bash
unread dump ./src/data/content.ts        # → ~/.unread/reports/files/text/content-<stamp>.ts
unread dump ./report.pdf                 # → ~/.unread/reports/files/pdf/report-<stamp>.pdf
unread dump ./meeting.mp3                # → ~/.unread/reports/files/audio/meeting-<stamp>.mp3
echo "raw text…" | unread dump           # → ~/.unread/reports/files/stdin/<slug>-<stamp>.txt
```

Use `unread <path>` (analyze) or `unread ask <path>` if you want
extraction-then-markdown — the LLM-bound paths consume markdown, but
`dump` is the "save the original" verb and a re-encode would be lossy.

---

## Wizard (no `<ref>`)

```bash
unread            # → pick chat → thread (forum) → preset → period → enrich → run
unread ask                # → pick chat → period → enrich → ask
unread dump               # → pick chat → period → enrich → run
unread tg describe        # → pick chat → show details / topics
```

Navigation: **↑/↓** move, **type to filter** (works for Cyrillic /
Greek / Arabic / Hebrew / Latin Extended too), **Enter** select,
**SPACE** toggles a checkbox (enrichment step), **→** in the
enrichment step also toggles, **ESC** goes back a step, **Ctrl-C**
quits.

Top of the chat picker:

- **🔍 Search all dialogs (not just unread)** — first item; jumps into a fuzzy picker over every dialog.
- **🚀 Run on ALL N unread chats (M total messages)** — second item; batch mode.
- Then the column-aligned chat list: `unread | kind | last msg | folder | title`.

The **enrichment step** runs **after** the period step (so the
"(N in db)" decoration on each option reflects the period the user just
chose). The header line shows `For the chosen period: N messages
already synced, M with media, K with URLs.` — instant feel for what
turning on `--enrich=image` will actually cost.

The `🚀` batch entry is offered when `analyze` is run without flags;
when picked, the wizard skips period/enrich and per-chat unread is the
fixed window.

---

## Forum chats (topics)

Forums are chats with topics, each with its own unread marker. Three
modes for both `analyze` and `dump`:

```bash
unread @forumchat --thread 42                       # one specific topic
unread @forumchat --all-flat --last-days 3          # whole forum, one report
unread @forumchat --all-per-topic                   # one report per topic
```

Without any of these, `unread @forumchat` opens a topic picker.

`unread tg describe @forumchat` prints the topic list with unread counts and
local-DB counts; both `tg describe` and the wizard fix Telegram's stale /
capped dialog-level forum counts by summing per-topic counts via
`GetForumTopicsRequest`.

---

## Media enrichment

Telegram chats carry more than text. `unread` turns each non-text message
into something the LLM can read:

| Kind | What happens | Default |
|---|---|---|
| **text** | Used as-is | always on |
| **voice** (🎤) | Transcribed via OpenAI Audio (`gpt-4o-mini-transcribe`) | **on** |
| **videonote** (round) | Audio extracted by ffmpeg → transcribed | **on** |
| **external link** | HTTP fetch + BeautifulSoup clean + 1–2 sentence summary via `filter_model` | **on** |
| **video** | Audio extracted by ffmpeg → transcribed | off |
| **photo** | Described via vision model (`gpt-4o-mini` by default) — short caption + OCR of any on-image text | off |
| **doc** (PDF / DOCX / txt / md / code) | Text extracted locally (`pypdf` / `python-docx` / plain read); truncated to `max_doc_chars` | off |

Three ways to control:

```bash
unread @somegroup --enrich=voice,image,link    # explicit set
unread @somegroup --enrich-all                 # everything
unread @somegroup --no-enrich                  # nothing, even defaults
```

**Precedence** (first wins): `--no-enrich` → `--enrich-all` →
`--enrich=<csv>` (unioned with the preset's `enrich:` frontmatter) →
preset's frontmatter alone → `[enrich]` block in `config.toml`.

**Concurrency / per-doc lock.** The orchestrator serializes per
`document_id` so a voice forwarded across multiple chats in one run
gets one Whisper call, not N.

**Caching.** Each enrichment result is stored once and reused: media
keyed by Telegram's stable `document_id` / `photo_id`
(`media_enrichments`); links keyed by normalized URL hash
(`link_enrichments`). Repeat runs over the same messages cost **$0**
once enrichments are cached.

**Costs at a glance**:
- Voice / videonote / video → Whisper (~$0.006/min).
- Image → one vision call per unique photo.
- Doc → free (local extraction).
- Link → one `filter_model` call per unique URL (cheap, `nano`-class).

The orchestrator logs a one-line summary, plus per-call lines tagged
with `phase=enrich_<kind>` and the originating `chat_id` / `msg_id` /
`msg_date` so the cost in `unread stats` is traceable to actual messages.

---

## Presets

What kind of analysis do you want? Pick a preset with `--preset`:

| Preset | What it produces |
|---|---|
| `summary` (default) | Concentrated signal — key insights, concrete ideas/decisions, 3–5 pointer messages. No recap prose. |
| `broad` | Full overview: top-3 themes + 5–10 bullet points + tone + key messages. |
| `digest` | Short numbered list of topics, 1–2 lines each. |
| `action_items` | Markdown table: *Who / What / Deadline / Status / Link*. |
| `decisions` | Markdown table: *Decision / Who / When / Rationale / Link*. |
| `highlights` | 5–15 most valuable messages, sorted by importance. |
| `questions` | Open questions table: *unanswered / partial / no consensus*. |
| `quotes` | Verbatim memorable quotes with author and link. |
| `links` | External URLs grouped by topic (auto-enables link enrichment). |
| `reactions` | Top-reacted messages grouped by reaction kind (👍 / 🔥 / 🤔 / 👎). |
| `single_msg` | Picked automatically when `<ref>` is a `t.me/.../<msg_id>` link. |
| `multichat` | Cross-chat synthesis. With no `<ref>` (batch / folder), aggregates messages across chats into ONE report instead of per-chat. |
| `custom --prompt-file path.md` | Your own one-off prompt; same frontmatter format as the bundled ones. |

Prompts live in [`presets/<lang>/*.md`](presets/) — `presets/en/` for
English, `presets/ru/` for Russian. Each language directory is
autonomous: a language can have any subset of presets, and the loader
does NOT fall back across languages. Edit them, add your own, commit
them to your fork. Bump `prompt_version` after changing the body to
invalidate the cache. Optional frontmatter `enrich: [link, image]`
declares which media enrichments this preset assumes (unioned with
`--enrich`); `description:` is shown by the wizard's preset picker.

**Reaction signals.** Messages whose reaction count meets
`[analyze] high_impact_reactions` (default 3) are tagged
`[high-impact]` in the LLM prompt. Presets that care about prominence
(`highlights`, `reactions`, `summary`) lean on the marker; others
ignore it.

---

## Language

Three independent settings cover the three roles a "language" can play:

| Setting | Drives | Default |
|---|---|---|
| `[locale] language` | **UI language**. Wizard, settings menu, banners, status messages, the `## Sources` heading from `--cite-context`, the `## Verification` heading from `--self-check`, the saved report's metadata block, the truncation banner. Drives `i18n.t()`. | `"en"` |
| `[locale] report_language` | **Report language**. Picks which `presets/<lang>/` tree the loader reads, the system prompt's base rules, the ask system prompt, the image / link enricher prompts, the formatter labels going *into* the LLM. The LLM writes the analysis in this language. | `""` → follow `language` |
| `[locale] content_language` | **Source-content hint** (Whisper-style). When set, the system prompt gets one extra line telling the LLM "the source content is in `<X>`". When empty, no hint is sent — the LLM auto-detects from the source text. Use only as an explicit override. | `""` → AI auto-detects |

The split exists because the three roles are genuinely different:

- An English speaker analyzing a Russian Telegram chat wants their
  wizard / saved-report metadata in English (`language=en`) but the LLM
  should produce Russian summaries (`report_language=ru`). No source
  hint needed — the chat is in Russian and the LLM detects that.
- A Russian speaker reading a Chinese Wikipedia article wants Russian
  reports (`language=ru`, `report_language=ru`) but the LLM doesn't
  always nail "this article is in Chinese" purely from the prose
  (especially when the report headings are Russian). Set
  `content_language=zh` and the model trusts the hint.

```toml
# config.toml
[locale]
language = "en"             # UI: wizard, banners, settings menu.
report_language = "ru"      # LLM writes analyses in Russian.
content_language = ""       # Empty = let the AI auto-detect the source.
                            # Set to "zh"/"ru"/… as a Whisper-style override.
```

Per-run overrides:

```bash
# English UI, Russian report; let the LLM detect what the chat is in.
unread @somechat --language en --report-language ru

# Russian UI + Russian report, but the source is Chinese (override the
# LLM's source-language detection).
unread https://zh.wikipedia.org/wiki/物理学 --report-language ru --content-language zh

# Ask follows the same shape.
unread ask "что обсуждали?" --language en --report-language ru
```

Whisper transcription has its own knob (`[openai] audio_language`) —
empty means autodetect; decoupled from all three locale axes.

### Persisting preferences with `unread settings`

Edit your locale prefs without touching `config.toml`:

```bash
unread settings                                  # interactive editor (three rows under Languages)
unread settings show                             # current effective values + DB overrides
unread settings set locale.language en
unread settings set locale.report_language ru
unread settings set locale.content_language zh   # Whisper-style override; empty = auto-detect
unread settings unset locale.content_language    # drop a single override
unread settings reset                            # drop all DB overrides
```

Saved to `storage/data.sqlite` in the `app_settings` table. Applied on
every `unread` invocation; explicit `--language` / `--report-language` /
`--content-language` flags still win.

### Migration note

`[locale] content_language` was renamed to `[locale] report_language` in
v1.x — the old name now means a Whisper-style source-language hint
(empty = auto-detect). If you had `content_language = "ru"` set under
the old semantics (LLM writes in Russian), re-set it as
`report_language = "ru"` and clear `content_language` (or leave it empty
to let the AI auto-detect). The CLI flag `--content-language` was
similarly renamed to `--report-language`; the new `--content-language`
flag is the source hint.

---

## Time window

By default `analyze` and `dump` process only messages past the chat's
read marker. To change that:

| Flag | Meaning |
|---|---|
| `--last-hours N` | Last N hours (UTC) — finer than `--last-days` |
| `--last-days N` | Last N days (UTC) |
| `--since YYYY-MM-DD --until YYYY-MM-DD` | Explicit date range (either end optional) |
| `--from-msg <id>` / message link | Start at a specific message, inclusive |
| `--full-history` | Entire chat |

Precedence: `--full-history` > `--from-msg` > `--last-hours` > `--since/--until/--last-days` > unread. When both `--last-hours` and `--last-days` are set, `--last-hours` wins.

YYYY-MM-DD strings are interpreted as **UTC** days (matches how
`messages.date` is stored).

---

## Cost & caching

Three caches, aggressive by design:

### 1. Local `analysis_cache`

Every analysis result is hashed by *preset + prompt_version + model +
sorted msg_ids + options_payload + system/user-prompt hashes* and
stored in SQLite. Re-run the same query → zero-cost hit. Toggling
`--enrich`, `--by`, the model, or any other option-payload field busts
the relevant rows.

```bash
unread cache stats               # rows, disk size, saved $, breakdown + prompt-cache hit rate
unread cache ls --limit 20       # latest entries
unread cache show <hash-prefix>  # print a stored result
unread cache export -o old.jsonl --older-than 30d
unread cache purge --older-than 30d --vacuum
```

**Truncated results are never cached.** A partial summary would
silently poison every future run.

### 2. OpenAI prompt cache (server-side)

When prompt prefix ≥ 1024 tokens and identical bytes arrive within
~5–10 minutes, OpenAI discounts repeated tokens.
`unread cache stats` shows your hit rate per (chat, preset) at the
bottom of its output.
`config.toml` enforces `temperature=0.2` and a fixed
`system → static_context → dynamic` message order to maximize hits.

### 3. Enrichment dedup

Per-kind forever-cache:

- Media (voice / videonote / video / photo / doc) keyed by Telegram's
  stable `document_id` or `photo_id` via `media_enrichments`.
- External links keyed by normalized URL hash via `link_enrichments`.

Forwarded 10× = fetched once.

### Up-front cost guard

```bash
unread @somegroup --max-cost 0.50    # confirm if estimate exceeds
unread @somegroup --max-cost 0.50 --yes   # silently abort if over
unread @somegroup --dry-run          # estimate-and-exit, no LLM call
```

Estimate covers the analysis (map + reduce); enrichment cost is **not**
included.

### Spending visibility

```bash
unread stats                     # totals by preset
unread stats --by chat           # biggest spenders by chat
unread stats --by day            # spend over time
unread stats --by kind           # chat vs audio vs ask
unread cache stats               # OpenAI prompt-cache hit rate per (chat, preset) (bottom of output)
```

If a row says `(N unpriced)` next to its call count, those rows used a
model not in your `[pricing.chat]` / `[pricing.audio]` table — add the
entry so cost stops under-reporting. `unread doctor` flags missing
pricing entries.

---

## Maintenance

```bash
# Version info (use this when filing bug reports)
unread --version                               # or `unread -V`

# Health check — Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk, pricing
unread doctor

# Diagnostic bundle for GitHub issues — version, doctor, redacted config + .env
unread bug-report                              # prints to stdout
unread bug-report --out report.txt             # writes to a file

# Backup the data DB (VACUUM INTO — atomic, compact)
unread backup up                               # → storage/backups/data-YYYY-MM-DD_HHMMSS.sqlite
unread backup up mybackup.sqlite --overwrite

# Restore a backup (current DB moved aside as data-replaced-…sqlite)
unread backup restore storage/backups/data-2026-04-25_…sqlite --yes

# `unread cache` is split into three entity groups, each exposing the
# same five commands: bare `<entity>` = ls, plus purge / stats / show / export.

# Analysis cache (per-LLM-call result rows in `analysis_cache`)
unread cache ai                                       # list newest first
unread cache ai stats                                 # size, age range, by (preset, model), prompt-cache hit rate
unread cache ai show <hash-prefix>                    # print a stored result
unread cache ai purge --older-than 90d                # delete by age / preset / model / --all
unread cache ai export -o ai.jsonl                    # dump rows to JSONL or markdown

# Source caches (extracted pages / YouTube transcripts / local-file text)
unread cache sources                                  # ls all kinds
unread cache sources ls --kind website                # one kind only
unread cache sources stats                            # rows + age per kind
unread cache sources show <page_id|video_id|file_id>  # row metadata + paragraph preview
unread cache sources purge --domain zh.wikipedia.org  # filter: --url / --domain / --kind / --older-than / --all
unread cache sources export -o sources.jsonl          # JSONL inventory; pass --include-paragraphs for full text

# Telegram message cache (per-row `messages` table — synced chat history)
unread cache tg                                       # per-chat counts (text / transcripts / age)
unread cache tg stats                                 # totals + chats / messages / transcripts / age range
unread cache tg show 1234567890                       # one chat's stats
unread cache tg purge --retention 90d                 # blank old message text (renamed from `unread cleanup`)
unread cache tg purge --retention 30d --chat 1234567890
unread cache tg export -o tg.jsonl --chat 1234567890  # dump cached rows; user-facing reports = `unread dump`

# Prune old report files to reports/.trash/<ts>/
unread reports prune --older-than 30d --dry-run    # see what would move
unread reports prune --older-than 30d
unread reports prune --older-than 90d --purge       # hard delete (asks first)

# Cache hygiene
unread cache purge --older-than 30d --vacuum
```

`cleanup` preserves row metadata (ids, dates, authors, transcripts) —
it only NULLs the raw `text` column.

---

## `unread watch` — scheduled runs

Foreground loop that runs an inner `unread` command on a fixed cadence.
No daemon — run under `tmux` / `nohup` for persistence.

```bash
unread watch --interval 1h tg chats run
unread watch --interval 1h --folder Work --post-saved
unread watch --interval 30m ask --folder Work "anything urgent?"
unread watch --interval 6h --max-runs 4 https://example.com/blog
```

| Flag | Meaning |
|---|---|
| `--interval Ns/Nm/Nh/Nd/Nw` | Cadence (or bare seconds). Defaults to `1h`. |
| `--max-runs N` | Stop after N runs (testing / fixed cycles). |

Bare `unread watch` (no inner command) prints this command's help.

Ctrl-C exits cleanly between iterations. The inner command's stdout
streams live; each iteration is preceded by `── Run K  YYYY-MM-DDThh:mm:ss`.

---

## `unread describe folders` — Telegram folder integration

Telegram "folders" (dialog filters) become a first-class scope:

```bash
unread describe folders                         # list every folder + chat counts
unread --folder Work                            # batch every unread chat in folder
unread dump --folder Work                       # same for dump
unread ask "..." --folder Work                  # Q&A scoped to folder
```

Folder column shows up in:
- `unread tg describe` (no ref) — the dialogs table.
- `unread tg describe @chat` — folder line under the username row.
- The wizard's chat picker — `unread | kind | last msg | folder | title`.

Only **explicitly listed** chats are expanded — rule-based folders
("contacts", "groups", "channels" without explicit peers) aren't
walked.

---

## Subscriptions (optional)

You don't need these for one-off analysis — `unread @chat` already
resolves the chat and fetches what's missing. Subscriptions are for
**long-term tracking**: a fixed set of chats you keep in your local DB,
sync on a cron, and analyze by date ranges across many runs.

```bash
unread chats add @somegroup
unread chats list
unread sync
unread chats remove <chat_id>
unread chats add @forum --all-topics
unread chats add @channel --with-comments
```

---

## Examples / recipes

```bash
# Daily morning digest of your work folder, into Saved Messages, on a 24h cron
unread watch --interval 24h analyze --folder Work --preset digest --post-saved

# Audit a high-stakes report — citations get expanded, claims verified
unread @somegroup --preset action_items --cite-context 5 --self-check

# What did Bob say last week? In one chat, with rerank + post-answer follow-ups (default)
unread ask "what did Bob propose?" @somegroup --last-days 7

# Filter analysis to one sender (with a citable result)
unread @somegroup --by Bob --preset highlights

# Cost-bounded run, with a budget alarm
unread @somegroup --enrich-all --max-cost 0.50 --post-to me

# Re-run with the same flags as last time, but force a fresh cache
unread @somegroup --repeat-last --no-cache

# Build a semantic index over a folder, then query it
unread ask --build-index --folder Work
unread ask "open architecture questions" --folder Work --semantic

# Forum: per-topic reports for the entire forum
unread @forumchat --all-per-topic

# Dump and save every photo / voice / video / doc alongside the text
unread dump @somegroup --save-media --save-media-types photo,voice

# Analyze a long-form article — paragraph-indexed citations link back to the page
unread "https://www.paulgraham.com/greatwork.html" --preset website

# Analyze a YouTube video, force Whisper instead of captions
unread "https://youtu.be/dQw4w9WgXcQ" --youtube-source audio --post-saved
```

---

## Configuration (`config.toml`)

The shipped file uses the convention: only settings you override are
uncommented; every knob is listed (commented-out, showing its default)
so it's discoverable. **Strict mode is on** — typos fail loudly with a
clear "extra inputs not permitted" error and the offending key.

Most-tuned settings:

```toml
[openai]
chat_model_default = "gpt-5.4-mini"      # final / single-chunk model
filter_model_default = "gpt-5.4-nano"    # map phase + cheap rerank + self-check
# audio_language = ""                    # Whisper hint; empty = autodetect

[locale]
# language = "en"                        # UI: "en" (default) / "ru" / …
# report_language = ""                   # LLM output; empty = follow `language`
# content_language = ""                  # Whisper-style source hint; empty = AI auto-detects

[analyze]
min_msg_chars = 3                        # filter: drop messages shorter than N chars
dedupe_forwards = true                   # collapse identical forwards/memes
output_budget_tokens = 1500              # reduce / single-chunk max_tokens
high_impact_reactions = 3                # `[high-impact]` marker threshold

[enrich]
vision_model = "gpt-4o-mini"

[ask]
rerank_enabled = true                    # default: rerank candidates before the answer
rerank_top_k = 500                       # candidate pool size before rerank
rerank_keep = 50                         # what survives rerank → flagship
```

`[pricing.chat.<model>]` / `[pricing.audio]` populate the cost table.
Models that aren't priced still work — they just show as "unpriced
calls" in `unread stats` and `unread doctor` warns about it.

`UNREAD_CONFIG_PATH=/abs/path/config.toml` overrides the cwd-relative
discovery.

---

## How it works

```
CLI (Typer)
  ├─ Resolver (Telethon)        ──► SQLite: chats
  ├─ Backfill (incremental)     ──► SQLite: messages
  └─ Analyzer pipeline
       ├─ Filter + dedupe (+ optional --by sender filter)
       ├─ Enrich (per-kind, opt-in)
       │    ├─ voice/videonote/video → OpenAI Audio    ──► media_enrichments(kind=transcript)
       │    ├─ photo                 → OpenAI Vision    ──► media_enrichments(kind=image_description)
       │    ├─ doc                   → pypdf / python-docx
       │    └─ link                  → httpx + bs4 + LLM ──► link_enrichments
       ├─ Chunk (token-aware, soft-breaks on idle gaps)
       ├─ Map-reduce (OpenAI)   ──► analysis_cache, analysis_runs, usage_log
       ├─ Optional --self-check (cheap-model verifier)
       └─ Optional --cite-context (expand `[#msg_id](url)` to message blocks)
```

Three SQLite tables under `storage/data.sqlite` matter most:

- `messages` — every message you've synced, plus media metadata + transcripts.
- `media_enrichments` / `link_enrichments` — per-kind enrichment caches keyed by stable IDs.
- `analysis_cache` — keyed analyses (zero-cost re-runs).

Plus newer:

- `chat_last_run_args` — backs `--repeat-last`.
- `message_embeddings` — vector store for `unread ask --semantic`.
- `usage_log` — every OpenAI call, with `phase=` tag for cost attribution.

Reports land in `reports/` (gitignored). Each cited claim is a
clickable link back to the source message. With `--cite-context N` the
report file additionally contains `<details>` fold blocks with N
messages around every citation, so the report is self-auditable
without re-opening Telegram.

Analyses larger than one context window are automatically map-reduced:
`filter_model` summarizes chunks in parallel, `final_model` merges. Each
map call is cached independently — adding one new message at the tail
re-costs only one chunk.

`unread ask` is its own pipeline: keyword retrieval (or embedding cosine)
→ optional rerank → format → single LLM call with citations. No
map-reduce; the candidate pool is bounded by `--limit`.

Before every network call, `analyze` and `dump` compare the local max
`msg_id` against Telegram's read marker — if nothing new exists, the
command exits without hitting the network.

---

## Troubleshooting

When something breaks, **always start with `unread doctor`** — it
surfaces 90% of the common issues with a fix hint inline. If you're
filing a GitHub issue, run `unread bug-report` and paste the bundle:
it gathers version, platform, doctor output, and your config with
every secret masked.

| Symptom | Fix |
|---|---|
| `Telegram session expired` / asks for code on every run | `unread init --force` (re-runs Telethon auth without re-prompting for keys) |
| `yt-dlp DownloadError` (private / region-locked / format change) | `uv tool upgrade unread` — yt-dlp tracks YouTube changes; running an outdated wheel breaks first |
| `ffmpeg not found` | Install per the platform table above; `unread doctor` confirms detection |
| `OPENAI_API_KEY missing` but you set it elsewhere | The CLI reads `~/.unread/.env`, not `~/.zshrc`. Either edit `~/.unread/.env` or run `unread init` to persist via the wizard |
| `attempt to write a readonly database` | `chmod -R 700 ~/.unread/storage` — the install dir lost write perms (sudo install, restored backup with wrong owner) |
| `storage permissions overpermissive` (doctor warning) | Run the `chmod 700 … && chmod 600 …` line printed by doctor — older installs predate the 0o700 hardening |
| Cost reports look truncated / `unread stats` shows zeros | `unread cache stats` to confirm the prompt cache is hitting (hit-rate table at the bottom); if not, verify `[pricing]` covers your model in `~/.unread/config.toml` |
| Cache directory is huge | `unread cache ai stats` then `unread cache ai purge --older-than 30d --vacuum`; also `unread cache sources purge` for cached source text |
| Migrating to a new install dir / moved `~/.unread/` | Set `UNREAD_HOME=/new/path`, or copy `.env` / `config.toml` / `storage/` / `reports/` into the new dir manually |
| Russian locale, English `--help` | Currently English help only; full localization is on the [roadmap](ROADMAP.md) |
| Want to start fresh | Delete `~/.unread/storage/data.sqlite` and re-run `unread init` — credentials persist in `data.sqlite::secrets`, so this resets cache + analysis runs while keeping or refreshing keys |

For anything else: `unread bug-report > report.txt` and paste into a
new issue at <https://github.com/maxbolgarin/unread/issues>.

---

## Development

```bash
uv sync --extra dev
uv run pytest -q              # all tests (pytest-asyncio auto mode)
uv run ruff check .           # lint
uv run ruff format --check .  # format check (CI runs this)
```

Contributor guide — invariants, caching layers, preset format, schema,
and editing hazards — lives in [`CLAUDE.md`](CLAUDE.md). Read it
before changing the pipeline, DB layer, or preset prompts.

Design notes and roadmap live under [`docs/`](docs/) and
[`ROADMAP.md`](ROADMAP.md).

Run `unread doctor` after any pull or env change — it surfaces the
common breakage points (missing ffmpeg, broken Telegram session,
missing pricing entries, schema drift).

---

## License

MIT — see [LICENSE](LICENSE).
