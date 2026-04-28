# unread

[![CI](https://github.com/maxbolgarin/unread/actions/workflows/ci.yml/badge.svg)](https://github.com/maxbolgarin/unread/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local Python CLI that pulls your Telegram chats (DMs, groups, forum
topics, channels, channel comments) and analyzes them with GPT. Every
message type flows through the analyzer: **text, voice, video notes,
videos, photos, PDFs / docs, and external links** — each gets
transformed into text before the LLM sees it. Voice/video notes are
transcribed by default; images, docs, video audio, and link summaries
are opt-in per run (they cost extra). By default `unread` starts from
Telegram's **unread marker** — the spot where you stopped reading — and
both **renders the Markdown report in the terminal** and saves a copy
under `~/.unread/reports/...` with clickable links back to every cited
message.

`unread <ref>` also accepts **YouTube URLs** (captions or Whisper
transcript → time-stamped citations) and **arbitrary web pages**
(article-body extraction → paragraph-indexed citations). Same pipeline,
same caches, same report layout — see [YouTube videos](#youtube-videos)
and [Web pages](#web-pages) below.

Everything is local. The only network calls are to Telegram (via
[Telethon](https://docs.telethon.dev)), OpenAI, and — when link
enrichment is enabled — the URLs shared in your chats.

```bash
# First time — initializes ~/.unread/ and logs in to Telegram.
unread tg init

# Most common: interactive wizard — pick a chat, pick a preset, done
unread                            # pick chat → preset → period → enrich → run

# Direct, when you know which chat — the bare ref is the analyze entry
unread @somegroup                              # console-rendered + auto-saved
unread @somegroup --no-save                    # render only, don't write a file
unread @somegroup --last-days 7 --preset digest

# `tg` / `telegram` aliases — same as above but auto-init on first use
unread tg @somegroup
unread telegram @somegroup

# Other content sources — same command, different shape
unread "https://www.youtube.com/watch?v=jmzoJCn8evU"   # YouTube video
unread "https://www.paulgraham.com/greatwork.html"     # any web page (article)
unread ./report.pdf                                    # local file
unread ./meeting.mp3                                   # audio (Whisper)
unread ./screenshot.png                                # image (vision)
cat notes.txt | unread                                 # stdin (pipe)
unread -                                               # stdin (explicit)

# Q&A across your synced archive (no Telegram round-trip)
unread ask                                              # opens the wizard
unread ask "what did Bob say about migration?" @somegroup
unread ask "open questions on the API" --folder Work
unread ask "..." --global                               # all synced chats, no wizard

# Cost-guarded run with citation audit blocks + Telegram Saved Messages delivery
unread @somegroup --max-cost 0.10 --cite-context 3 --post-saved

# Dump history to a file, no OpenAI
unread dump @somegroup -o history.md --last-days 30

# Help — `--help` and `help` both work; `help <cmd>` walks subcommands.
unread help
unread help describe
unread help tg init
```

---

## Installation

Three steps. `unread` lives entirely under `~/.unread/` and works from
any working directory.

### 1. Install the prerequisites

- **Python 3.11+**
- **[`uv`](https://github.com/astral-sh/uv)** — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **`ffmpeg`** on PATH — **only** if you want to enrich video / video
  notes (voice messages, images, PDFs, and links work without it). PDF
  and DOCX extraction, HTTP fetch, HTML parsing, and image-to-base64
  are handled by `pypdf` / `python-docx` / `httpx` / `beautifulsoup4`,
  all installed automatically.

### 2. Install the CLI

```bash
git clone https://github.com/maxbolgarin/unread.git
cd unread
uv tool install --editable .
```

That puts the **`unread`** command on your PATH. `--editable` picks up
source changes automatically; pass `--reinstall` after a `git pull` if
you want freshly added Python dependencies to land too.

> **Prefer not to install globally?** Skip `uv tool install`, run
> `uv sync --extra dev` once, and prefix every command with
> `uv run` (e.g. `uv run unread @somegroup`).

### 3. First-time setup

```bash
unread tg init
```

On a fresh install this is a four-step interactive wizard:

1. **Pick where data lives.** Choose between `~/.unread/` (default), the
   current directory, or a custom path. The choice is recorded at
   `~/.unread/install.toml` and survives across runs.
2. **AI provider + key.** Pick which provider drives `analyze` / `ask`:
   - **openai** — vanilla OpenAI Chat Completions. Default. Also backs
     Whisper transcription, embeddings, and vision (used by
     `--enrich=voice`/`videonote`/`video`/`image` and `ask --semantic`).
   - **openrouter** — single key, many models, via
     <https://openrouter.ai>.
   - **anthropic** — Claude (Sonnet / Haiku) directly.
   - **google** — Gemini (2.5 Flash / Flash Lite).
   - **local** — self-hosted OpenAI-compatible server (Ollama, LM Studio,
     vLLM). No API key required.

   Then paste the corresponding key — or press Enter to skip.
   Skipping leaves the install usable for `dump`, `describe`, `sync`,
   `folders`, `chats *`, `backup`/`restore`, etc.; only `analyze` /
   `ask` need a key.

   > **Capability gaps.** Whisper / embeddings / vision are OpenAI-only.
   > If you pick Anthropic / Google / OpenRouter / Local as your chat
   > provider but also want media transcription or `--semantic`
   > retrieval, run `unread tg init` again and add an OpenAI key
   > alongside — the chat provider stays unchanged. Without one, those
   > features skip with a one-line warning.
3. **Telegram login** (optional). Answer `y` to set up `api_id` /
   `api_hash` from <https://my.telegram.org> → *API development tools*,
   then complete Telethon's phone+code prompt. Answer `n` to skip —
   you can still use `unread "<youtube-url>"` or
   `unread "<website-url>"` with just the OpenAI key.
4. **Done.** Credentials are persisted in
   `<install>/storage/data.sqlite::secrets` so you can blow away `.env`
   and the CLI keeps working.

Re-run `unread tg init` to fill in any step you skipped — only the
unsatisfied steps prompt. `unread tg init --force` re-runs Telethon
auth (useful when switching accounts) without re-prompting for folder
or keys; to re-pick the install folder, delete
`~/.unread/install.toml` first.

**Non-interactive setup** (CI, scripts): pre-populate `~/.unread/.env`:

```
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
OPENAI_API_KEY=sk-...
```

Then `unread tg init` skips the wizard prompts and goes straight to
Telethon auth. The `.env` values continue to win over anything
persisted in the secrets DB, so rotating a key only needs an `.env`
edit.

> Already running with a cwd-relative install from a previous version?
> Run `unread migrate` from your old install directory to copy
> `./.env`, `./config.toml`, `./storage/`, and `./reports/` into
> `~/.unread/`. Pass `--move` to remove the originals after.

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

## Command reference

`unread --help` shows three panels.

### Main (everyday)

| Command | Purpose |
|---|---|
| `unread [<ref>] [flags]` | Analyze a chat (default action). No args → interactive wizard. |
| `unread tg [<ref>] [flags]` / `unread telegram [<ref>] [flags]` | Same as the bare form, but auto-runs `tg init` if no Telegram session exists. |
| `unread tg init [--force]` | First-time setup: log in to Telegram, OpenAI ping, seed `~/.unread/`. `--force` wipes the saved session before logging in. |
| `unread help [<cmd>]` / `unread --help` | Show top-level help (no args) or walk into a subcommand: `unread help describe`, `unread help tg init`. |
| `unread describe [<ref>]` | List dialogs (no ref) or inspect one chat. Shows folder column. |
| `unread ask ["question"] [<ref>] [flags]` | Q&A across your synced archive — no Telegram round-trip. No args opens a wizard. |
| `unread dump [<ref>] [flags]` | Dump history to md/jsonl/csv. No OpenAI call by default. |
| `unread migrate [--move] [--dry-run]` | Move legacy cwd-relative `./storage` and `./reports` into `~/.unread/`. |

> **Subcommand-name collisions.** `unread <ref>` will route to a
> subcommand if `<ref>` matches one (e.g. `unread describe` opens the
> describe command, not a chat literally titled "describe"). Use
> `unread tg "describe"` or `unread -- describe` for the rare case of
> a chat that shares a subcommand name.

### Sync & subscriptions

| Command | Purpose |
|---|---|
| `unread sync` | Pull new messages for every active subscription. |
| `unread chats add/list/enable/disable/remove` | Manage subscriptions. Optional — one-off `analyze` already fetches. |

### Maintenance

| Command | Purpose |
|---|---|
| `unread folders` | List your Telegram folders (use with `--folder NAME`). |
| `unread stats [--by …]` | Token spend / cache hit rate — by chat, preset, model, day, kind. |
| `unread cleanup --retention 90d` | Null out old message text (preserves metadata + transcripts). |
| `unread cache stats / ls / show / purge / export` | Analysis-cache maintenance. |
| `unread cache effectiveness` | Per-(chat, preset) OpenAI prompt-cache hit rate. |
| `unread doctor` | Preflight check — Telegram session, OpenAI key, ffmpeg, DB integrity, pricing. |
| `unread backup [out]` | Snapshot `storage/data.sqlite` via `VACUUM INTO`. |
| `unread restore <file>` | Replace `data.sqlite` with a backup (current DB moved aside). |
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
| Fuzzy title | `"Bull Trading"` — substring match across your dialogs |
| YouTube URL | `https://www.youtube.com/watch?v=...` (see [YouTube videos](#youtube-videos)) |
| Website URL | `https://example.com/article` (see [Web pages](#web-pages)) |
| Local file path | `./report.pdf`, `~/notes.md`, `/tmp/recording.mp3` (see [Local files](#local-files)) |
| `-` | Read from stdin: `cat notes.txt \| unread` or `unread - < notes.txt` |

The wizard's chat picker accepts non-Latin type-to-filter (Cyrillic,
Greek, Arabic, Hebrew, Latin Extended) so searching for `биохакинг` or
`finanças` works the same as `crypto`.

### YouTube videos

`unread analyze <youtube-url>` analyzes a single video end-to-end. Flow:

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
unread analyze "https://www.youtube.com/watch?v=jmzoJCn8evU"

# Scripted (skip prompts, auto-pick captions / Whisper as needed):
unread analyze "https://youtu.be/dQw4w9WgXcQ" --yes

# Force Whisper (slower; ~$0.003/min):
unread analyze "https://youtu.be/dQw4w9WgXcQ" --youtube-source audio

# Different preset; see `unread analyze --help` for the full list.
unread analyze "https://www.youtube.com/watch?v=..." --preset summary --console
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

`unread analyze <url>` analyzes any HTTP/HTTPS web page (article, blog
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
unread analyze "https://www.paulgraham.com/greatwork.html"

# Estimate-and-exit (no LLM call):
unread analyze "https://example.com/blog/post" --dry-run

# Render to terminal instead of saving a file:
unread analyze "https://example.com/blog/post" --console

# Different preset — `summary`, `digest`, `highlights`, etc. all work:
unread analyze "https://example.com/blog/post" --preset summary

# Cost-bounded run + post the analysis to your Saved Messages:
unread analyze "https://example.com/blog/post" --max-cost 0.05 --post-saved

# Run a custom prompt against a page:
unread analyze "https://example.com/paper.html" --preset custom --prompt-file my-prompt.md
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

## `unread analyze` — flags

```bash
unread analyze [<ref>] [period] [output] [enrichment] [budget] [audit] [delivery]
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

## `unread ask` — Q&A across your synced archive

```bash
unread ask "what did we decide about the migration?" @somegroup
unread ask                                                 # opens the wizard
```

Reads only your **local DB** — no Telegram round-trip during retrieval.
The corpus is everything `analyze` / `dump` / `sync` has already pulled
(transcripts, image descriptions, doc extracts, link summaries
included).

**Synopsis**: `unread ask "QUESTION" [<ref>] [flags]`. The positional
`<ref>` accepts any chat reference (`@user`, `t.me` link, topic URL,
fuzzy title, numeric id). A topic URL like
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

### Cost feel

- **Retrieval**: free (local SQL).
- **Rerank** (default on): ~10 cheap-model calls × ~1k tokens each ≈ $0.005 per question.
- **Answer**: scales with `--limit`. With rerank+keep=50 and `gpt-5.4-mini`, typical cost is **~$0.01–0.05 per question**.

Cost is logged under `phase=ask` in `usage_log` — see `unread stats --by kind`.

---

## `unread dump` — chat history to a file

No OpenAI call by default. Same backfill + filter pipeline as `analyze`,
just writes raw messages instead of an analysis.

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

---

## Wizard (no `<ref>`)

```bash
unread analyze            # → pick chat → thread (forum) → preset → period → enrich → run
unread ask                # → pick chat → period → enrich → ask
unread dump               # → pick chat → period → enrich → run
unread describe           # → pick chat → show details / topics
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
unread analyze @forumchat --thread 42                       # one specific topic
unread analyze @forumchat --all-flat --last-days 3          # whole forum, one report
unread analyze @forumchat --all-per-topic                   # one report per topic
```

Without any of these, `unread analyze @forumchat` opens a topic picker.

`unread describe @forumchat` prints the topic list with unread counts and
local-DB counts; both `describe` and the wizard fix Telegram's stale /
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
unread analyze @somegroup --enrich=voice,image,link    # explicit set
unread analyze @somegroup --enrich-all                 # everything
unread analyze @somegroup --no-enrich                  # nothing, even defaults
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

Two independent settings let you mix and match UI and chat content language:

- `[locale] language` — **UI / saved-report headings**. Wizard, the
  `## Sources` heading appended by `--cite-context`, the `## Verification`
  heading from `--self-check`, the saved report's metadata block, the
  truncation banner. Defaults to `"en"`.
- `[locale] content_language` — **prompts / LLM input**. Picks which
  `presets/<lang>/` tree the loader reads, the image/link enricher
  prompt language, the ask system prompt, the formatter labels going
  into the LLM. Defaults to follow `language`.

The split exists because the natural use case is asymmetric: an English
speaker analyzing a Russian Telegram chat wants their wizard / saved
report metadata in English, but the LLM should still see Russian
prompts and produce Russian output (so the analysis is idiomatic).

```toml
# config.toml
[locale]
language = "en"             # UI + report headings. Wizard, ## Sources, etc.
content_language = "ru"     # Prompts the LLM gets + the language it answers in.
                            # Empty = follow `language`.
```

Per-run override:

```bash
# English UI, Russian prompts → English headings, Russian analysis body
unread analyze @somechat --language en --content-language ru
unread ask "что обсуждали?" --language en --content-language ru
```

Whisper transcription has its own knob (`[openai] audio_language`) —
empty means autodetect, decoupled from both UI and content language.

### Persisting preferences with `unread settings`

Edit your locale prefs without touching `config.toml`:

```bash
unread settings                              # interactive editor
unread settings show                         # current effective values + DB overrides
unread settings set locale.language en
unread settings set locale.content_language ru
unread settings unset locale.content_language  # drop a single override
unread settings reset                         # drop all DB overrides
```

Saved to `storage/data.sqlite` in the `app_settings` table. Applied on
every `unread` invocation; explicit `--language` / `--content-language`
flags still win.

### Migration note

When you upgrade from a pre-locale build, your existing config has no
`[locale]` block and defaults to English. To restore Russian as before:
either run `unread settings set locale.language ru` (one-time), or add
`[locale] language = "ru"` to your `config.toml`.

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
unread cache stats               # rows, disk size, saved $, breakdown
unread cache ls --limit 20       # latest entries
unread cache show <hash-prefix>  # print a stored result
unread cache export -o old.jsonl --older-than 30d
unread cache purge --older-than 30d --vacuum
unread cache effectiveness       # per-(chat, preset) prompt-cache hit rate from usage_log
```

**Truncated results are never cached.** A partial summary would
silently poison every future run.

### 2. OpenAI prompt cache (server-side)

When prompt prefix ≥ 1024 tokens and identical bytes arrive within
~5–10 minutes, OpenAI discounts repeated tokens.
`unread cache effectiveness` shows your hit rate per (chat, preset).
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
unread analyze @somegroup --max-cost 0.50    # confirm if estimate exceeds
unread analyze @somegroup --max-cost 0.50 --yes   # silently abort if over
unread analyze @somegroup --dry-run          # estimate-and-exit, no LLM call
```

Estimate covers the analysis (map + reduce); enrichment cost is **not**
included.

### Spending visibility

```bash
unread stats                     # totals by preset
unread stats --by chat           # biggest spenders by chat
unread stats --by day            # spend over time
unread stats --by kind           # chat vs audio vs ask
unread cache effectiveness       # OpenAI prompt-cache hit rate per (chat, preset)
```

If a row says `(N unpriced)` next to its call count, those rows used a
model not in your `[pricing.chat]` / `[pricing.audio]` table — add the
entry so cost stops under-reporting. `unread doctor` flags missing
pricing entries.

---

## Maintenance

```bash
# Health check — Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk, pricing
unread doctor

# Backup the data DB (VACUUM INTO — atomic, compact)
unread backup                                  # → storage/backups/data-YYYY-MM-DD_HHMMSS.sqlite
unread backup mybackup.sqlite --overwrite

# Restore a backup (current DB moved aside as data-replaced-…sqlite)
unread restore storage/backups/data-2026-04-25_…sqlite --yes

# Null out old message texts (privacy / disk reclaim)
unread cleanup --retention 90d                # preview + confirmation
unread cleanup --retention 90d --yes
unread cleanup --retention 30d --chat 1234567890

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
unread watch --interval 1h analyze --folder Work --post-saved
unread watch --interval 30m ask "anything urgent?" --folder Work
unread watch --interval 24h --max-runs 7 analyze --folder Work --digest
```

| Flag | Meaning |
|---|---|
| `--interval Nm/Nh/Nd/Nw` | Cadence (or bare seconds). Required. |
| `--max-runs N` | Stop after N runs (testing / fixed cycles). |

Ctrl-C exits cleanly between iterations. The inner command's stdout
streams live; each iteration is preceded by `── Run K  YYYY-MM-DDThh:mm:ss`.

---

## `unread folders` — Telegram folder integration

Telegram "folders" (dialog filters) become a first-class scope:

```bash
unread folders                                  # list every folder + chat counts
unread analyze --folder Work                    # batch every unread chat in folder
unread dump --folder Work                       # same for dump
unread ask "..." --folder Work                  # Q&A scoped to folder
```

Folder column shows up in:
- `unread describe` (no ref) — the dialogs table.
- `unread describe @chat` — folder line under the username row.
- The wizard's chat picker — `unread | kind | last msg | folder | title`.

Only **explicitly listed** chats are expanded — rule-based folders
("contacts", "groups", "channels" without explicit peers) aren't
walked.

---

## Subscriptions (optional)

You don't need these for one-off analysis — `unread analyze @chat` already
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
unread analyze @somegroup --preset action_items --cite-context 5 --self-check

# What did Bob say last week? In one chat, with rerank + post-answer follow-ups (default)
unread ask "what did Bob propose?" @somegroup --last-days 7

# Filter analysis to one sender (with a citable result)
unread analyze @somegroup --by Bob --preset highlights

# Cost-bounded run, with a budget alarm
unread analyze @somegroup --enrich-all --max-cost 0.50 --post-to me

# Re-run with the same flags as last time, but force a fresh cache
unread analyze @somegroup --repeat-last --no-cache

# Build a semantic index over a folder, then query it
unread ask --build-index --folder Work
unread ask "open architecture questions" --folder Work --semantic

# Forum: per-topic reports for the entire forum
unread analyze @forumchat --all-per-topic

# Dump and save every photo / voice / video / doc alongside the text
unread dump @somegroup --save-media --save-media-types photo,voice

# Analyze a long-form article — paragraph-indexed citations link back to the page
unread analyze "https://www.paulgraham.com/greatwork.html" --preset website

# Analyze a YouTube video, force Whisper instead of captions
unread analyze "https://youtu.be/dQw4w9WgXcQ" --youtube-source audio --post-saved
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
# language = "en"                        # "en" (default) / "ru" / …
# content_language = ""                  # follow `language` unless set explicitly

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
