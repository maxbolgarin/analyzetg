# analyzetg

[![CI](https://github.com/maxbolgarin/analyzetg/actions/workflows/ci.yml/badge.svg)](https://github.com/maxbolgarin/analyzetg/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local Python CLI that pulls your Telegram chats (DMs, groups, forum
topics, channels, channel comments) and analyzes them with GPT. Every
message type flows through the analyzer: **text, voice, video notes,
videos, photos, PDFs / docs, and external links** — each gets
transformed into text before the LLM sees it. Voice/video notes are
transcribed by default; images, docs, video audio, and link summaries
are opt-in per run (they cost extra). By default `atg` starts from
Telegram's **unread marker** — the spot where you stopped reading — and
writes a Markdown report to `reports/` with clickable links back to
every cited message.

Everything is local. The only network calls are to Telegram (via
[Telethon](https://docs.telethon.dev)), OpenAI, and — when link
enrichment is enabled — the URLs shared in your chats.

```bash
# First time
atg init                      # log in to Telegram, verify OpenAI key

# Most common: interactive wizard — pick a chat, pick a preset, done
atg analyze                   # pick chat → preset → (enrich?) → period → runs

# Direct, when you know which chat
atg analyze @somegroup                  # summary of unread (voice/videonote auto-transcribed)
atg analyze @somegroup --console        # render in terminal instead of a file
atg analyze @somegroup --last-days 7 --preset digest

# Turn on extra enrichments for this run
atg analyze @somegroup --enrich=image,link         # describe photos + summarize URLs
atg analyze @somegroup --enrich-all                # every media kind
atg analyze @somegroup --no-enrich                 # skip everything (text only)

# Dump history to a file, no OpenAI
atg dump @somegroup -o history.md --last-days 30
```

---

## Installation

Five steps, in order. Don't skip — `atg` won't run without the credentials
from step 3.

### 1. Install the prerequisites

- **Python 3.11+**
- **[`uv`](https://github.com/astral-sh/uv)** — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **`ffmpeg`** on PATH — **only** if you want to enrich video / video
  notes (voice messages, images, PDFs, and links work without it). PDF
  and DOCX extraction, HTTP fetch, HTML parsing, and image-to-base64
  are handled by `pypdf` / `python-docx` / `httpx` / `beautifulsoup4`,
  all installed automatically.

### 2. Clone the repo

```bash
git clone https://github.com/maxbolgarin/analyzetg.git
cd analyzetg
```

All the commands below assume you're in this directory.

### 3. Get your API credentials

- **Telegram** `api_id` / `api_hash` — log in at
  <https://my.telegram.org> → *API development tools* → create an app.
- **OpenAI** API key — <https://platform.openai.com/api-keys>.

### 4. Configure (required before installing)

```bash
cp .env.example .env
cp config.toml.example config.toml
```

Open **`.env`** and paste the credentials from step 3:

```
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
OPENAI_API_KEY=sk-...
```

`config.toml` has sane defaults — model choices, pricing table, chunk
sizes. Leave it alone until you want to tune something.

```bash
mkdir -p storage && chmod 700 storage   # SQLite isn't encrypted; rely on FS perms
```

### 5. Install the CLI

```bash
# Install globally (editable — your source edits take effect immediately)
uv tool install --editable .
```

That puts two commands on your PATH: **`atg`** (short) and `analyzetg`
(long). They're identical — use whichever you prefer.

> **Prefer not to install globally?** Skip `uv tool install` entirely,
> run `uv sync --extra dev` once, and prefix every command with
> `uv run` — e.g. `uv run atg analyze @somegroup`.

### 6. Upgrading (read this before it bites you)

```bash
git pull
uv tool install --editable . --reinstall
```

`--editable` picks up source changes automatically, but newly added
Python dependencies (`beautifulsoup4`, `pypdf`, `python-docx` — used by
the opt-in enrichments for links, PDFs, and docx) only land in the
tool's venv when you pass `--reinstall`.

**Missing the reinstall is not fatal anymore** — enrichers whose
libraries aren't installed now skip themselves with a one-line warning
like `enrich.link.lib_missing lib='beautifulsoup4' hint='run uv tool
install --editable . --reinstall'` and the rest of the analysis
continues. But to use those enrichments you still need the libraries,
so run the reinstall after any `git pull`.

If you see `ModuleNotFoundError` at **startup** (not mid-run), you're on
an old build that eager-imported the optional libs. Pull + reinstall
and it goes away.

### 7. First-time login

```bash
atg init
```

Interactive wizard: sends a code to your Telegram, creates the local
session at `storage/session.sqlite`, and does a 1-token OpenAI ping to
confirm your key. Only needed once.

---

## Where does `atg` read config and write data?

**Everything is resolved relative to the current working directory.**
Run `atg` from the repo directory (the one containing your `.env` and
`config.toml`) and you'll get:

```
./.env                          ← credentials (step 4)
./config.toml                   ← models, pricing, tuning (step 4)
./storage/session.sqlite        ← Telegram session (created by atg init)
./storage/data.sqlite           ← chats, messages, analysis cache
./reports/{chat}[/{topic}]/analyze/{preset}-{stamp}.md   ← default report path
```

If you `cd` somewhere else and run `atg`, it will look for `.env` and
`config.toml` **in that directory** — and won't find them, so the
command will fail with missing credentials. Two ways to avoid that:

- **Always `cd` into the repo first** (simplest).
- **Add a shell function + alias** that pins the directory (works from anywhere):

  **zsh** (`~/.zshrc`):
  ```zsh
  _atg_run() { (cd ~/path/to/analyzetg && command atg "$@"); }
  alias atg='nocorrect _atg_run'
  ```
  `nocorrect` disables zsh's spell-correction for `atg` arguments — without it, typing `atg stats` can trigger `zsh: correct 'stats' to 'stat'?` and end with a parse error.

  **bash** (`~/.bashrc`):
  ```bash
  atg() { (cd ~/path/to/analyzetg && command atg "$@"); }
  ```

  Reload with `source ~/.zshrc` (or open a new terminal). Reports will
  still land in the repo's `reports/` dir, not wherever you invoked from.

---

## Everyday usage

`atg` always has to know **which chat** to work on. You can either launch
the interactive wizard or point it at a chat directly.

### The wizard (no chat ref)

```bash
atg analyze            # → pick a chat → pick a preset → pick enrichments → pick a period → go
atg dump               # → pick a chat → pick a period → dump to file
atg describe           # → pick a chat → show details / topics
```

Navigation: **↑/↓** move, **type** to filter, **Enter** to select,
**SPACE** to toggle a checkbox (enrichment step), **ESC** to go back a
step, **Ctrl-C** to quit. The first item in the chat list is always
*"Run on ALL N unread chats"* — picking it batch-processes every chat
with unread messages at once.

The enrichment step pre-checks the same defaults as `config.toml`
(voice + videonote), so for most chats you can just hit Enter.

### Direct (with a chat ref)

```bash
atg analyze @somegroup                   # summary of unread
atg analyze https://t.me/somegroup       # same, via link
atg analyze -1001234567890               # by numeric chat_id
atg analyze "Bull Trading"               # fuzzy title match
```

`<ref>` accepts:

| Form | Example |
|---|---|
| `@username` | `@durov` |
| `https://t.me/…` | `https://t.me/durov/123` (jumps to message 123) |
| Forum-topic link | `https://t.me/somegroup/100/5000` (topic 100, msg 5000) |
| Private link | `https://t.me/c/1234567890/5000` |
| Invite link | `https://t.me/+AbCdEf...` (add `--join` to join it) |
| Numeric `chat_id` | `-1001234567890` or `1001234567890` |
| Fuzzy title | `"Bull Trading"` — substring match across your dialogs |

### What you get back

By default `atg analyze` writes a Markdown file to
`reports/{chat}-{preset}-{YYYY-MM-DD_HHMM}.md` and prints its path.
Every cited claim includes a clickable link back to the source message:

```markdown
Фонды переходят на индексные структуры с 2026 Q1. [#1586](https://t.me/c/3865481227/584/1586)
```

Flags:

- **`--console` / `-c`** — render the result in your terminal (pretty,
  Rich-formatted) instead of saving. Combine with `-o` to do both.
- **`-o <path>`** — custom output path. For batch mode (no `<ref>`) it's
  treated as a directory.
- **`--mark-read`** — after the analysis, advance Telegram's read
  marker to cover every processed message (so the chat looks "read" in
  your other Telegram clients).
- **`--enrich=<list>`** / **`--enrich-all`** / **`--no-enrich`** —
  control which media types get turned into text for this run. See
  [Media enrichment](#media-enrichment) below.

---

## Media enrichment

Telegram chats carry more than text. `atg` turns each non-text message
into something the LLM can read:

| Kind | What happens | Default |
|---|---|---|
| **text** | Used as-is | always on |
| **voice** (🎤) | Transcribed via OpenAI Audio (`gpt-4o-mini-transcribe`) | **on** |
| **videonote** (round) | Audio extracted by ffmpeg → transcribed | **on** |
| **external link** | HTTP fetch + BeautifulSoup clean + 1–2 sentence summary via `filter_model` | off |
| **video** | Audio extracted by ffmpeg → transcribed | off |
| **photo** | Described via vision model (`gpt-4o-mini` by default) — short caption + OCR of any on-image text | off |
| **doc** (PDF / DOCX / txt / md / code) | Text extracted locally (`pypdf` / `python-docx` / plain read); truncated to `max_doc_chars` | off |

Only voice and videonote are on by default. Everything else is **opt-in**
because each one fires its own OpenAI / network call (one per unique URL,
photo, or document). Three ways to turn extras on:

```bash
# Per-run, explicit set
atg analyze @somegroup --enrich=voice,image,link

# Per-run, everything
atg analyze @somegroup --enrich-all

# Per-run, nothing (not even voice/videonote)
atg analyze @somegroup --no-enrich
```

**Precedence** when multiple sources disagree:
`--no-enrich` → `--enrich-all` → `--enrich=<csv>` (unioned with the
preset's declared needs) → the preset's `enrich:` frontmatter → the
`[enrich]` block in `config.toml` (config defaults).

**Per-preset requirements.** A preset can declare enrichments it
genuinely needs in its frontmatter, e.g. `links.md` already has
`enrich: [link]` so picking that preset fetches + summarizes every URL
without you remembering a flag.

**Config defaults** live in `config.toml`:

```toml
[enrich]
voice = true
videonote = true
video = false
image = false
doc = false
link = false   # opt-in: one OpenAI call per unique URL
vision_model = "gpt-4o-mini"
# doc_model / link_model default to the preset's filter_model when null.
max_images_per_run = 50
max_link_fetches_per_run = 50
max_doc_bytes = 25000000     # 25 MB — covers most real PDFs/DOCX
max_doc_chars = 20000        # hard cap on extracted text per document
link_fetch_timeout_sec = 10
# skip_link_domains = ["twitter.com", "x.com"]
concurrency = 3
```

**Caching.** Each enrichment is stored once and reused:
- Voice / videonote / video / image / doc → keyed by Telegram's stable
  `document_id` or `photo_id`, so the same media forwarded across 10
  chats is processed once.
- External URLs → keyed by a normalized URL hash.

Repeat runs over the same messages cost **$0** once enrichments are
cached. Toggling flags busts only the analysis cache, not the
enrichment caches.

**Costs at a glance** (all spend flows into `atg stats`):
- Voice / videonote / video → Whisper (~$0.006/min).
- Image → one vision call per unique photo.
- Doc → free for text/PDF/DOCX extraction (local), then the extract
  rides inside the analysis prompt like any other message body.
- Link → one `filter_model` call per unique URL (cheap, `nano`-class).

The orchestrator logs a one-line summary after every run:

```
analyze.enrich summary='Enriched: voice: 12 (5 cached); image: 3; link: 7 (2 cached)'
```

Each individual enrichment call is also logged with its kind, so you can tell at a glance whether a burst of `openai.chat` calls is analysis, link summaries, or image descriptions:

```
openai.chat phase=enrich_link url_hash=abc123… prompt=181 completion=59 cost=0.00011
openai.chat phase=enrich_image doc_id=5245… prompt=1250 completion=60 cost=0.00025
openai.audio phase=enrich_voice doc_id=8917… seconds=42 cost=0.00252
openai.chat phase=map batch_hash=deadbeef… prompt=7740 completion=349 cost=0.00198
```

---

## Presets

What kind of analysis do you want? Pick a preset with `--preset`:

| Preset | What it produces |
|---|---|
| `summary` (default) | Concentrated signal — key insights, concrete ideas/decisions, 3–5 pointer messages. No recap prose. |
| `broad` | Full overview: Top-3 themes + 5–10 bullet points + tone + key messages (what the old `summary` produced) |
| `digest` | Short numbered list of topics, 1–2 lines each |
| `action_items` | Markdown table: *Who / What / Deadline / Status / Link* |
| `decisions` | Markdown table: *Decision / Who / When / Rationale / Link* |
| `highlights` | 5–15 most valuable messages, sorted by importance |
| `questions` | Open questions table: *unanswered / partial / no consensus* |
| `quotes` | Verbatim memorable quotes with author and link |
| `links` | External URLs from the chat, grouped by topic |
| `custom --prompt-file path.md` | Your own one-off prompt, no file in `presets/` needed |

> **Why the split?** The old `summary` was a structured re-telling of the
> chat — useful, but easy to replicate by scrolling. The new default
> tries to answer "what's *new* or *non-obvious* here?" and skips the
> recap. If you genuinely want the structured overview, pick
> `--preset broad`.

Prompts live in [`presets/*.md`](presets/) — edit them, add your own,
commit them to your fork. Bump `prompt_version` inside the preset file
to invalidate the cache after you change the prompt. Optional
frontmatter field `enrich: [link, image]` declares which media
enrichments this preset assumes the chat will need; they get unioned
with whatever the user passed via `--enrich`.

---

## Time window

By default `analyze` and `dump` process **only unread messages**
(`msg_id > read_marker`). To change that:

| Flag | Meaning |
|---|---|
| `--last-days 7` | Last N days |
| `--since 2026-01-15 --until 2026-01-20` | Explicit date range (either end optional) |
| `--from-msg <id>` / message link | Start at a specific message, inclusive |
| `--full-history` | Entire chat |

Precedence when multiple are set: `--full-history` > `--from-msg` >
`--since / --until / --last-days` > unread default.

---

## Forum chats (topics)

Forums are chats with topics, and each topic has its own unread marker.
Three modes, work for both `analyze` and `dump`:

```bash
# One specific topic — message links include /thread/
atg analyze @forumchat --thread 42

# Whole forum as one analysis (requires an explicit period)
atg analyze @forumchat --all-flat --last-days 3

# One report per topic (each topic's own unread)
atg analyze @forumchat --all-per-topic
# → reports/{chat-slug}/{topic-slug}-summary-YYYY-MM-DD_HHMM.md
```

Launch `atg analyze @forumchat` without any of these and you get the
wizard: a table of topics with unread counts + a *"one topic"* /
*"all-flat"* / *"per-topic"* chooser.

`atg describe @forumchat` prints the topic list with unread counts and
how many messages are already in your local DB.

---

## Download raw media

Separate from enrichment (which turns media into text for the LLM),
`atg download-media` saves the actual bytes — photos, voice notes,
video, videonotes, documents — so you can keep an archive.

```bash
# Everything from the last week
atg download-media @somegroup --last-days 7

# Just photos and PDFs from a forum topic, capped at 100 files
atg download-media @forumchat --thread 42 --types photo,doc --limit 100

# Preview what would be downloaded without writing
atg download-media @somegroup --last-days 7 --dry-run

# Overwrite previously-downloaded files
atg download-media @somegroup --overwrite

# Custom output root (default is reports/)
atg download-media @somegroup -o ~/archive/tg
```

Files land in `reports/<chat-slug>/media/` (or
`reports/<chat-slug>/<topic-slug>/media/` for a forum topic). Names are
`{msg_id}.{ext}` for photos/voice/video, and `{msg_id}_{original-name}`
for documents (preserves the real PDF/zip filename). Runs work off
messages already in the local DB — run `atg sync` or `atg analyze`
first if you need the latest.

No OpenAI calls, no cost beyond Telegram download bandwidth. Re-runs
are idempotent: existing files are skipped unless you pass
`--overwrite`.

---

## Useful recipes

```bash
# Render a chat's unread in the terminal, no file
atg analyze @somegroup --console

# Short digest of the last week, custom path
atg analyze @somegroup --last-days 7 --preset digest -o weekly.md

# Extract the most valuable messages
atg analyze @somegroup --preset highlights

# Open questions that still deserve a reply
atg analyze @somegroup --preset questions

# All external links mentioned in the chat, grouped by topic
# (links preset auto-enables link enrichment via its frontmatter)
atg analyze @somegroup --preset links

# Describe photos + read PDFs before summarizing
atg analyze @somegroup --enrich=image,doc

# Everything enriched — max context, max spend
atg analyze @somegroup --enrich-all

# Text-only, skip even voice transcription
atg analyze @somegroup --no-enrich --text-only

# From a specific message onwards (link embeds the msg_id)
atg analyze "https://t.me/somegroup/10000"

# Whole chat history → action items
atg analyze @somegroup --full-history --preset action_items

# Analyze and mark read in Telegram afterwards
atg analyze @somegroup --mark-read

# Dump-only (no OpenAI): history for the last 30 days
atg dump @somegroup -o history.md --last-days 30

# Dump with voice/videonote transcripts filled in
atg dump @somegroup -o dump.jsonl --format jsonl --with-transcribe
```

---

## Cost & caching

`analyzetg` is aggressive about caching so repeat runs are cheap or free.

### Check what you've spent

```bash
atg stats                     # totals by preset (cost_usd, tokens, calls)
atg stats --by model          # or break down by model
atg stats --by day            # or by day
```

### Analysis cache (local, biggest win)

Every analysis result is hashed by *preset + prompt version + model +
message ids + options + rendered prompt hashes* and stored in the local
SQLite `analysis_cache` table. Re-run the same command with the same
message text, enrichments, prompt context, and settings → zero-cost hit.

```bash
atg cache stats               # rows, disk size, saved $, breakdown
atg cache ls --limit 20       # latest entries
atg cache show <hash-prefix>  # print a stored result
atg cache export -o old.jsonl --older-than 30d   # archive before purging
atg cache purge --older-than 30d --vacuum        # delete + reclaim disk
```

### OpenAI prompt cache (server-side, free discount)

When prompt prefix ≥ 1024 tokens and identical within ~5–10 minutes,
OpenAI automatically discounts the repeated tokens. `atg stats` shows
the hit rate; `config.toml` enforces `temperature=0.2` and a fixed
*system → static → dynamic* message order to maximize it.

### Enrichment dedup

Every enrichment — transcript, image description, PDF extract, URL
summary — is stored once and reused across every future run:

- Media (voice, videonote, video, photo, doc) keys by Telegram's
  stable `document_id` or `photo_id` via the `media_enrichments` table.
  Forwarded 10×, fetched once.
- External links key by a normalized URL hash via `link_enrichments`,
  so a viral article shared across 3 chats is summarized once.

Toggling `--enrich` flags only busts the **analysis** cache (because
the prompt changes) — the enrichment caches survive and get reused.

---

## Maintenance

```bash
# Null out old message texts (privacy / disk reclaim)
atg cleanup --retention 90d                  # preview + confirmation prompt
atg cleanup --retention 90d --yes            # skip the prompt

# Clean up old cache results
atg cache purge --older-than 30d --vacuum

# Per-chat retention
atg cleanup --retention 30d --chat 1234567890
```

`--older-than` must be greater than zero. `0d` is treated as a no-op so
it cannot accidentally wipe the whole analysis cache.

`cleanup` preserves row metadata (ids, dates, authors, transcripts) — it
only NULLs the raw `text` column, so you keep the ability to re-analyze
later with `--with-transcribe` without losing the structure.

---

## Subscriptions (optional)

You don't need these for one-off analysis — `atg analyze @chat` already
resolves the chat and fetches what's missing. Subscriptions are for
**long-term tracking**: a fixed set of chats you want to keep in your
local DB, sync on a cron, and analyze by date ranges across many runs.

```bash
atg chats add @somegroup         # subscribe
atg chats list                   # see what's subscribed
atg sync                         # fetch new messages for every subscription
atg chats remove <chat_id>       # unsubscribe
```

> The old standalone `atg transcribe` command has been removed —
> enrichment now runs inside `analyze`. If you want to pre-cache
> transcripts in bulk before analyzing, just run `atg analyze @chat
> --full-history --no-cache` (or the preset of your choice); voice /
> videonote transcription fires by default and populates the cache,
> and the analysis itself is then a cache hit on every later run.

Forum support: `atg chats add @forum --all-topics` subscribes to every
topic; `--thread N` subscribes to one.

Channel with comments: `atg chats add @channel --with-comments`
subscribes to both the channel and its linked discussion group.

---

## How it works (short)

```
CLI (Typer)
   └─ Resolver (Telethon)        ──► SQLite: chats
   └─ Backfill                   ──► SQLite: messages
   └─ Analyzer pipeline
        ├─ Filter + dedupe
        ├─ Enrich (per-kind, opt-in)
        │     ├─ voice/videonote/video → OpenAI Audio        ──► media_enrichments(kind=transcript)
        │     ├─ photo                 → OpenAI Vision        ──► media_enrichments(kind=image_description)
        │     ├─ doc                   → pypdf / python-docx  ──► media_enrichments(kind=doc_extract)
        │     └─ link                  → httpx + bs4 + LLM    ──► link_enrichments
        ├─ Chunk (token-aware, soft-breaks on idle gaps)
        └─ Map-reduce (OpenAI)  ──► analysis_cache, analysis_runs, usage_log
```

Two SQLite files under `storage/`:

- `session.sqlite` — Telethon session.
- `data.sqlite` — chats, messages, enrichments, link summaries,
  analysis cache, token usage log. `schema.sql` is applied on every
  open, with small additive compatibility checks for older local DBs.
  A read-only `media_transcripts` view of
  `media_enrichments(kind='transcript')` is preserved for backward
  compatibility with external tooling that queried the old table
  directly.

Reports land in `reports/` (gitignored by default).

**Enrichment stage** runs between filter and chunk: each message's
media gets turned into text (or skipped, when the kind isn't enabled)
and the result is attached to the in-memory `Message`. The formatter
then composes `text → [image: …] → [doc: …] → transcript → ↳ link
summaries` into one dense line per message, which is what the LLM
actually reads.

Analyses larger than one context window are automatically **map-reduced**:
a cheap model (`gpt-5.4-nano`) summarizes chunks in parallel, then the
flagship model (`gpt-5.4`) merges them into the final report. Each map
call is cached independently, so adding one new message re-costs only
one chunk.

Before every network call, `analyze` and `dump` compare the local
max `msg_id` against Telegram's read marker — if nothing new exists,
the command exits without hitting the network at all.

---

## Development

```bash
uv run pytest                 # unit tests
uv run ruff check .           # lint
uv run ruff format .          # format
```

Contributor guide — invariants, caching layers, preset format, and editing
hazards — lives in [`CLAUDE.md`](CLAUDE.md). Read it before changing the
pipeline, DB layer, or preset prompts.

Design notes and implementation plans live under [`docs/`](docs/).

---

## License

MIT — see [LICENSE](LICENSE).
