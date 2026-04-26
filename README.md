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
atg analyze                   # pick chat → preset → period → enrich → run

# Direct, when you know which chat
atg analyze @somegroup                  # summary of unread (voice/videonote auto-transcribed)
atg analyze @somegroup --console        # render in terminal instead of a file
atg analyze @somegroup --last-days 7 --preset digest

# Q&A across your synced archive (no Telegram round-trip)
atg ask                                              # opens the wizard
atg ask "what did Bob say about migration?" @somegroup
atg ask "open questions on the API" --folder Work
atg ask "..." --global                               # all synced chats, no wizard

# Cost-guarded run with citation audit blocks + Telegram Saved Messages delivery
atg analyze @somegroup --max-cost 0.10 --cite-context 3 --post-saved

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
sizes. The shipped file uses the convention "only overrides are
uncommented; every other knob is listed as a comment showing its
default", so you can scan it once and only flip what you want to
change. Strict-mode parsing catches typos.

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

### 6. Upgrading

```bash
git pull
uv tool install --editable . --reinstall
```

`--editable` picks up source changes automatically, but newly added
Python dependencies (`beautifulsoup4`, `pypdf`, `python-docx` — used by
the opt-in enrichments for links, PDFs, and docx) only land in the
tool's venv when you pass `--reinstall`. Run `atg doctor` after a pull
to verify your environment is clean.

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
./storage/data.sqlite           ← chats, messages, analysis cache, embeddings
./storage/backups/              ← snapshots from `atg backup`
./reports/{chat}[/{topic}]/analyze/{preset}-{stamp}.md   ← default report path
./reports/{chat}/dump/dump-{stamp}.md                    ← default dump path
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

---

## Command reference

`atg --help` shows three panels.

### Main (everyday)

| Command | Purpose |
|---|---|
| `atg init` | Interactive first-time setup. |
| `atg describe [<ref>]` | List dialogs (no ref) or inspect one chat. Shows folder column. |
| `atg analyze [<ref>] [flags]` | Analyze a chat. Default window = unread. |
| `atg ask ["question"] [<ref>] [flags]` | Q&A across your synced archive — no Telegram round-trip. No args opens a wizard. |
| `atg dump [<ref>] [flags]` | Dump history to md/jsonl/csv. No OpenAI call by default. |

### Sync & subscriptions

| Command | Purpose |
|---|---|
| `atg sync` | Pull new messages for every active subscription. |
| `atg chats add/list/enable/disable/remove` | Manage subscriptions. Optional — one-off `analyze` already fetches. |

### Maintenance

| Command | Purpose |
|---|---|
| `atg folders` | List your Telegram folders (use with `--folder NAME`). |
| `atg stats [--by …]` | Token spend / cache hit rate — by chat, preset, model, day, kind. |
| `atg cleanup --retention 90d` | Null out old message text (preserves metadata + transcripts). |
| `atg cache stats / ls / show / purge / export` | Analysis-cache maintenance. |
| `atg cache effectiveness` | Per-(chat, preset) OpenAI prompt-cache hit rate. |
| `atg doctor` | Preflight check — Telegram session, OpenAI key, ffmpeg, DB integrity, pricing. |
| `atg backup [out]` | Snapshot `storage/data.sqlite` via `VACUUM INTO`. |
| `atg restore <file>` | Replace `data.sqlite` with a backup (current DB moved aside). |
| `atg reports prune --older-than 30d` | Move stale report files to `reports/.trash/`. |
| `atg watch --interval 1h <inner cmd>` | Run an inner `atg` command on a fixed cadence. |

### Hidden (still callable, not in `--help`)

`atg download-media [<ref>]` — kept for back-compat. Use `atg dump --save-media` instead.

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

The wizard's chat picker accepts non-Latin type-to-filter (Cyrillic,
Greek, Arabic, Hebrew, Latin Extended) so searching for `биохакинг` or
`finanças` works the same as `crypto`.

---

## `atg analyze` — flags

```bash
atg analyze [<ref>] [period] [output] [enrichment] [budget] [audit] [delivery]
```

### Period (start point of the analysis window)

| Flag | Meaning |
|---|---|
| `--full-history` | Whole chat |
| `--from-msg <id>` / message link | Start at a specific message, inclusive |
| `--since YYYY-MM-DD` / `--until YYYY-MM-DD` / `--last-days N` | Date range (UTC) |
| _(none)_ | Unread only — `msg_id > read_marker` |

Precedence (first match wins): `--full-history` > `--from-msg` > `--since/--until/--last-days` > unread.

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

## `atg ask` — Q&A across your synced archive

```bash
atg ask "what did we decide about the migration?" @somegroup
atg ask                                                 # opens the wizard
```

Reads only your **local DB** — no Telegram round-trip during retrieval.
The corpus is everything `analyze` / `dump` / `sync` has already pulled
(transcripts, image descriptions, doc extracts, link summaries
included).

**Synopsis**: `atg ask "QUESTION" [<ref>] [flags]`. The positional
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
atg ask

# Positional ref — username:
atg ask "what did Bob say about migration?" @somegroup

# Positional ref — topic URL (thread auto-filled):
atg ask "open questions on the API" https://t.me/c/1234567890/4

# Across every synced chat (no wizard):
atg ask "когда дедлайн по проекту?" --global --last-days 7

# Folder scope, semantic retrieval (build index first):
atg ask "..." --folder Work --build-index
atg ask "open questions on the API" --folder Work --semantic --rerank --last-days 14

# Cheap and small:
atg ask "..." --limit 50 --model gpt-5.4-nano

# Debug retrieval before paying for the answer:
atg ask "..." @somegroup --show-retrieved --max-cost 0.05

# Single answer, no follow-up prompt (script-friendly):
atg ask "..." @somegroup --no-followup
```

### Cost feel

- **Retrieval**: free (local SQL).
- **Rerank** (default on): ~10 cheap-model calls × ~1k tokens each ≈ $0.005 per question.
- **Answer**: scales with `--limit`. With rerank+keep=50 and `gpt-5.4-mini`, typical cost is **~$0.01–0.05 per question**.

Cost is logged under `phase=ask` in `usage_log` — see `atg stats --by kind`.

---

## `atg dump` — chat history to a file

No OpenAI call by default. Same backfill + filter pipeline as `analyze`,
just writes raw messages instead of an analysis.

```bash
atg dump @somegroup -o history.md --last-days 30
atg dump @somegroup --format jsonl --with-transcribe -o dump.jsonl
atg dump @somegroup --save-media           # also save raw media files alongside
atg dump --folder Work                     # batch-dump every unread chat in folder
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
atg analyze            # → pick chat → thread (forum) → preset → period → enrich → run
atg ask                # → pick chat → period → enrich → ask
atg dump               # → pick chat → period → enrich → run
atg describe           # → pick chat → show details / topics
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
atg analyze @forumchat --thread 42                       # one specific topic
atg analyze @forumchat --all-flat --last-days 3          # whole forum, one report
atg analyze @forumchat --all-per-topic                   # one report per topic
```

Without any of these, `atg analyze @forumchat` opens a topic picker.

`atg describe @forumchat` prints the topic list with unread counts and
local-DB counts; both `describe` and the wizard fix Telegram's stale /
capped dialog-level forum counts by summing per-topic counts via
`GetForumTopicsRequest`.

---

## Media enrichment

Telegram chats carry more than text. `atg` turns each non-text message
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
atg analyze @somegroup --enrich=voice,image,link    # explicit set
atg analyze @somegroup --enrich-all                 # everything
atg analyze @somegroup --no-enrich                  # nothing, even defaults
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
`msg_date` so the cost in `atg stats` is traceable to actual messages.

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
atg analyze @somechat --language en --content-language ru
atg ask "что обсуждали?" --language en --content-language ru
```

Whisper transcription has its own knob (`[openai] audio_language`) —
empty means autodetect, decoupled from both UI and content language.

### Persisting preferences with `atg settings`

Edit your locale prefs without touching `config.toml`:

```bash
atg settings                              # interactive editor
atg settings show                         # current effective values + DB overrides
atg settings set locale.language en
atg settings set locale.content_language ru
atg settings unset locale.content_language  # drop a single override
atg settings reset                         # drop all DB overrides
```

Saved to `storage/data.sqlite` in the `app_settings` table. Applied on
every `atg` invocation; explicit `--language` / `--content-language`
flags still win.

### Migration note

When you upgrade from a pre-locale build, your existing config has no
`[locale]` block and defaults to English. To restore Russian as before:
either run `atg settings set locale.language ru` (one-time), or add
`[locale] language = "ru"` to your `config.toml`.

---

## Time window

By default `analyze` and `dump` process only messages past the chat's
read marker. To change that:

| Flag | Meaning |
|---|---|
| `--last-days N` | Last N days (UTC) |
| `--since YYYY-MM-DD --until YYYY-MM-DD` | Explicit date range (either end optional) |
| `--from-msg <id>` / message link | Start at a specific message, inclusive |
| `--full-history` | Entire chat |

Precedence: `--full-history` > `--from-msg` > `--since/--until/--last-days` > unread.

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
atg cache stats               # rows, disk size, saved $, breakdown
atg cache ls --limit 20       # latest entries
atg cache show <hash-prefix>  # print a stored result
atg cache export -o old.jsonl --older-than 30d
atg cache purge --older-than 30d --vacuum
atg cache effectiveness       # per-(chat, preset) prompt-cache hit rate from usage_log
```

**Truncated results are never cached.** A partial summary would
silently poison every future run.

### 2. OpenAI prompt cache (server-side)

When prompt prefix ≥ 1024 tokens and identical bytes arrive within
~5–10 minutes, OpenAI discounts repeated tokens.
`atg cache effectiveness` shows your hit rate per (chat, preset).
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
atg analyze @somegroup --max-cost 0.50    # confirm if estimate exceeds
atg analyze @somegroup --max-cost 0.50 --yes   # silently abort if over
atg analyze @somegroup --dry-run          # estimate-and-exit, no LLM call
```

Estimate covers the analysis (map + reduce); enrichment cost is **not**
included.

### Spending visibility

```bash
atg stats                     # totals by preset
atg stats --by chat           # biggest spenders by chat
atg stats --by day            # spend over time
atg stats --by kind           # chat vs audio vs ask
atg cache effectiveness       # OpenAI prompt-cache hit rate per (chat, preset)
```

If a row says `(N unpriced)` next to its call count, those rows used a
model not in your `[pricing.chat]` / `[pricing.audio]` table — add the
entry so cost stops under-reporting. `atg doctor` flags missing
pricing entries.

---

## Maintenance

```bash
# Health check — Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk, pricing
atg doctor

# Backup the data DB (VACUUM INTO — atomic, compact)
atg backup                                  # → storage/backups/data-YYYY-MM-DD_HHMMSS.sqlite
atg backup mybackup.sqlite --overwrite

# Restore a backup (current DB moved aside as data-replaced-…sqlite)
atg restore storage/backups/data-2026-04-25_…sqlite --yes

# Null out old message texts (privacy / disk reclaim)
atg cleanup --retention 90d                # preview + confirmation
atg cleanup --retention 90d --yes
atg cleanup --retention 30d --chat 1234567890

# Prune old report files to reports/.trash/<ts>/
atg reports prune --older-than 30d --dry-run    # see what would move
atg reports prune --older-than 30d
atg reports prune --older-than 90d --purge       # hard delete (asks first)

# Cache hygiene
atg cache purge --older-than 30d --vacuum
```

`cleanup` preserves row metadata (ids, dates, authors, transcripts) —
it only NULLs the raw `text` column.

---

## `atg watch` — scheduled runs

Foreground loop that runs an inner `atg` command on a fixed cadence.
No daemon — run under `tmux` / `nohup` for persistence.

```bash
atg watch --interval 1h analyze --folder Work --post-saved
atg watch --interval 30m ask "anything urgent?" --folder Work
atg watch --interval 24h --max-runs 7 analyze --folder Work --digest
```

| Flag | Meaning |
|---|---|
| `--interval Nm/Nh/Nd/Nw` | Cadence (or bare seconds). Required. |
| `--max-runs N` | Stop after N runs (testing / fixed cycles). |

Ctrl-C exits cleanly between iterations. The inner command's stdout
streams live; each iteration is preceded by `── Run K  YYYY-MM-DDThh:mm:ss`.

---

## `atg folders` — Telegram folder integration

Telegram "folders" (dialog filters) become a first-class scope:

```bash
atg folders                                  # list every folder + chat counts
atg analyze --folder Work                    # batch every unread chat in folder
atg dump --folder Work                       # same for dump
atg ask "..." --folder Work                  # Q&A scoped to folder
```

Folder column shows up in:
- `atg describe` (no ref) — the dialogs table.
- `atg describe @chat` — folder line under the username row.
- The wizard's chat picker — `unread | kind | last msg | folder | title`.

Only **explicitly listed** chats are expanded — rule-based folders
("contacts", "groups", "channels" without explicit peers) aren't
walked.

---

## Subscriptions (optional)

You don't need these for one-off analysis — `atg analyze @chat` already
resolves the chat and fetches what's missing. Subscriptions are for
**long-term tracking**: a fixed set of chats you keep in your local DB,
sync on a cron, and analyze by date ranges across many runs.

```bash
atg chats add @somegroup
atg chats list
atg sync
atg chats remove <chat_id>
atg chats add @forum --all-topics
atg chats add @channel --with-comments
```

---

## Examples / recipes

```bash
# Daily morning digest of your work folder, into Saved Messages, on a 24h cron
atg watch --interval 24h analyze --folder Work --preset digest --post-saved

# Audit a high-stakes report — citations get expanded, claims verified
atg analyze @somegroup --preset action_items --cite-context 5 --self-check

# What did Bob say last week? In one chat, with rerank + post-answer follow-ups (default)
atg ask "what did Bob propose?" @somegroup --last-days 7

# Filter analysis to one sender (with a citable result)
atg analyze @somegroup --by Bob --preset highlights

# Cost-bounded run, with a budget alarm
atg analyze @somegroup --enrich-all --max-cost 0.50 --post-to me

# Re-run with the same flags as last time, but force a fresh cache
atg analyze @somegroup --repeat-last --no-cache

# Build a semantic index over a folder, then query it
atg ask --build-index --folder Work
atg ask "open architecture questions" --folder Work --semantic

# Forum: per-topic reports for the entire forum
atg analyze @forumchat --all-per-topic

# Dump and save every photo / voice / video / doc alongside the text
atg dump @somegroup --save-media --save-media-types photo,voice
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
calls" in `atg stats` and `atg doctor` warns about it.

`ANALYZETG_CONFIG_PATH=/abs/path/config.toml` overrides the cwd-relative
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
- `message_embeddings` — vector store for `atg ask --semantic`.
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

`atg ask` is its own pipeline: keyword retrieval (or embedding cosine)
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

Run `atg doctor` after any pull or env change — it surfaces the
common breakage points (missing ffmpeg, broken Telegram session,
missing pricing entries, schema drift).

---

## License

MIT — see [LICENSE](LICENSE).
