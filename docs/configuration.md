# Configuration, costs, and operations

← Back to [README](../README.md)

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

## `config.toml`

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
| `ffmpeg not found` | Install per the platform table in [install.md](install.md); `unread doctor` confirms detection |
| `OPENAI_API_KEY missing` but you set it elsewhere | The CLI reads `~/.unread/.env`, not `~/.zshrc`. Either edit `~/.unread/.env` or run `unread init` to persist via the wizard |
| `attempt to write a readonly database` | `chmod -R 700 ~/.unread/storage` — the install dir lost write perms (sudo install, restored backup with wrong owner) |
| `storage permissions overpermissive` (doctor warning) | Run the `chmod 700 … && chmod 600 …` line printed by doctor — older installs predate the 0o700 hardening |
| Cost reports look truncated / `unread stats` shows zeros | `unread cache stats` to confirm the prompt cache is hitting (hit-rate table at the bottom); if not, verify `[pricing]` covers your model in `~/.unread/config.toml` |
| Cache directory is huge | `unread cache ai stats` then `unread cache ai purge --older-than 30d --vacuum`; also `unread cache sources purge` for cached source text |
| Migrating to a new install dir / moved `~/.unread/` | Set `UNREAD_HOME=/new/path`, or copy `.env` / `config.toml` / `storage/` / `reports/` into the new dir manually |
| Russian locale, English `--help` | Currently English help only; full localization is on the roadmap |
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
and editing hazards — lives in [`CLAUDE.md`](../CLAUDE.md). Read it
before changing the pipeline, DB layer, or preset prompts.

Run `unread doctor` after any pull or env change — it surfaces the
common breakage points (missing ffmpeg, broken Telegram session,
missing pricing entries, schema drift).
