# Command reference

← Back to [README](../README.md)

`unread --help` shows four panels.

## Main (everyday)

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

## Telegram

| Command | Purpose |
|---|---|
| `unread tg describe [<ref>]` | List dialogs (no ref) or inspect one chat. Shows folder column. |
| `unread tg describe folders` | List your Telegram folders (use with `--folder NAME`). |
| `unread tg login [--force]` | Re-run the Telegram-only login step. `--force` wipes the saved session first. |
| `unread tg logout` | Remove the local Telegram session. |
| `unread tg chats add <ref>` / `unread tg chats manage` | Add a subscription, or open the interactive panel to list / enable / disable / remove. Optional — one-off `analyze` already fetches. |
| `unread tg sync` | Pull new messages for every active subscription. |

## Maintenance

| Command | Purpose |
|---|---|
| `unread stats [--by …]` | Token spend / cache hit rate — by chat, preset, model, day, kind. |
| `unread cache <entity> [ls\|purge\|stats\|show\|export]` | Cache maintenance with three entity groups (`<entity>` ∈ `ai` \| `sources` \| `tg`). Bare `cache <entity>` = `ls`. |
| `unread doctor` | Preflight check — Telegram session, OpenAI key, ffmpeg, DB integrity, pricing, FDE, cloud-sync warnings. |
| `unread update [--check] [-y]` | Check PyPI for a newer release and (optionally) install it. |
| `unread security {status,set,unlock,lock,rotate-passphrase,revoke-session}` | Inspect / switch the credential-storage backend. See [security.md](security.md). |
| `unread backup up [out]` | Snapshot `storage/data.sqlite` via `VACUUM INTO`. |
| `unread backup restore <file>` | Replace `data.sqlite` with a backup (current DB moved aside). |
| `unread reports prune --older-than 30d` | Move stale report files to `reports/.trash/`. |
| `unread watch --interval 1h <inner cmd>` | Run an inner `unread` command on a fixed cadence. |

## Hidden (still callable, not in `--help`)

`unread download-media [<ref>]` — kept for back-compat. Use `unread dump --save-media` instead.

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

## `unread tg describe folders` — Telegram folder integration

Telegram "folders" (dialog filters) become a first-class scope:

```bash
unread tg describe folders                      # list every folder + chat counts
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
unread tg chats add @somegroup
unread tg chats manage                  # interactive panel: list, enable, disable, remove
unread tg sync
unread tg chats add @forum --all-topics
unread tg chats add @channel --with-comments
```

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
