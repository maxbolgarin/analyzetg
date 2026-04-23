# analyzetg

A local Python CLI that pulls your Telegram chats (DMs, groups, forum
topics, channels, channel comments), transcribes voice messages, video
notes and videos via OpenAI, and analyzes the result with GPT. By default
it starts from Telegram's **unread marker** — the spot where you stopped
reading — and writes a Markdown report to `reports/` with clickable
links back to every cited message.

Everything is local. The only network calls are to Telegram (via
[Telethon](https://docs.telethon.dev)) and OpenAI.

```bash
# First time
atg init                      # log in to Telegram, verify OpenAI key

# Most common: interactive wizard — pick a chat, pick a preset, done
atg analyze                   # pick chat → preset → period → runs

# Direct, when you know which chat
atg analyze @somegroup                  # summary of unread messages
atg analyze @somegroup --console        # render in terminal instead of a file
atg analyze @somegroup --last-days 7 --preset digest

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
- **`ffmpeg`** on PATH — **only** if you want to transcribe videos
  (voice messages and video notes work without it)

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

### 6. First-time login

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
./reports/{chat}/analyze/…md    ← analysis reports (default output)
```

If you `cd` somewhere else and run `atg`, it will look for `.env` and
`config.toml` **in that directory** — and won't find them, so the
command will fail with missing credentials. Two ways to avoid that:

- **Always `cd` into the repo first** (simplest).
- **Add a shell alias** that pins the directory (works from anywhere):
  ```bash
  # ~/.zshrc or ~/.bashrc
  alias atg='(cd ~/path/to/analyzetg && atg "$@")'
  ```
  Reports will still land in the repo's `reports/` dir.

---

## Everyday usage

`atg` always has to know **which chat** to work on. You can either launch
the interactive wizard or point it at a chat directly.

### The wizard (no chat ref)

```bash
atg analyze            # → pick a chat → pick a preset → pick a period → go
atg dump               # → pick a chat → pick a period → dump to file
atg describe           # → pick a chat → show details / topics
```

Navigation: **↑/↓** move, **type** to filter, **Enter** to select,
**ESC** to go back a step, **Ctrl-C** to quit. The first item in the
chat list is always *"Run on ALL N unread chats"* — picking it
batch-processes every chat with unread messages at once.

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

---

## Presets

What kind of analysis do you want? Pick a preset with `--preset`:

| Preset | What it produces |
|---|---|
| `summary` (default) | Top-3 themes + 5–10 bullet points + tone + key messages |
| `digest` | Short numbered list of topics, 1–2 lines each |
| `action_items` | Markdown table: *Who / What / Deadline / Status / Link* |
| `decisions` | Markdown table: *Decision / Who / When / Rationale / Link* |
| `highlights` | 5–15 most valuable messages, sorted by importance |
| `questions` | Open questions table: *unanswered / partial / no consensus* |
| `quotes` | Verbatim memorable quotes with author and link |
| `links` | External URLs from the chat, grouped by topic |
| `custom --prompt-file path.md` | Your own one-off prompt, no file in `presets/` needed |

Prompts live in [`presets/*.md`](presets/) — edit them, add your own,
commit them to your fork. Bump `prompt_version` inside the preset file
to invalidate the cache after you change the prompt.

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
atg analyze @somegroup --preset links

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
message ids + options* and stored in the local SQLite
`analysis_cache` table. Re-run the same command → zero-cost hit.

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

### Transcription dedup

Each Telegram voice/videonote has a stable `document_id`. One
transcription per document, even if the voice is forwarded across 10
chats.

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
atg transcribe --since 2026-01-01   # transcribe pending voices in a window
atg chats remove <chat_id>       # unsubscribe
```

Forum support: `atg chats add @forum --all-topics` subscribes to every
topic; `--thread N` subscribes to one.

Channel with comments: `atg chats add @channel --with-comments`
subscribes to both the channel and its linked discussion group.

---

## How it works (short)

```
CLI (Typer)
   └─ Resolver (Telethon)   ──► SQLite: chats
   └─ Backfill             ──► SQLite: messages
   └─ Transcriber (OpenAI) ──► SQLite: media_transcripts
   └─ Analyzer (OpenAI)    ──► SQLite: analysis_cache, runs, usage_log
```

Two SQLite files under `storage/`:

- `session.sqlite` — Telethon session.
- `data.sqlite` — chats, messages, transcripts, analysis cache, token
  usage log.

Reports land in `reports/` (gitignored by default).

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
uv run pytest                 # unit tests (9+ test files)
uv run ruff check .           # lint
uv run ruff format .          # format
```

Full spec: [`docs/analyzetg-spec.md`](docs/analyzetg-spec.md) (if present).

---

## License

MIT — see [LICENSE](LICENSE).
