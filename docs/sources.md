# Sources — what `<ref>` accepts

← Back to [README](../README.md)

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

## YouTube videos

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

## Web pages

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

## Local files

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

## Presets

What kind of analysis do you want? Pick a preset with `--preset`:

| Preset | What it produces |
|---|---|
| `summary` (default) | Concentrated signal — key insights, concrete ideas/decisions, 3–5 pointer messages. No recap prose. |
| `tldr` | Two or three sentences in one paragraph — phone-screen scan, no structure. |
| `digest` | 5–10 most important themes, 1–2 lines each. |
| `highlights` | Top 5–15 most valuable messages, sorted by importance. |
| `quotes` | Verbatim memorable quotes with author and link. |
| `links` | External URLs grouped by topic (auto-enables link enrichment). |
| `action_items` | Markdown table: *Who / What / Deadline / Status / Link*. |
| `decisions` | Markdown table: *Decision / Who / When / Rationale / Link*. |
| `questions` | Open questions table: *unanswered / partial / no consensus*. |
| `reactions` | Top-reacted messages grouped by reaction kind (👍 / 🔥 / 🤔 / 👎). |
| `single_msg` | Picked automatically when `<ref>` is a `t.me/.../<msg_id>` link. |
| `multichat` | Picked automatically for batch / folder analysis: aggregates across chats into ONE report instead of per-chat. |
| `video` | Picked automatically for YouTube URLs — transcript summary with time-stamped citations. |
| `website` | Picked automatically for article / blog / docs URLs — TL;DR + key claims + key quotes. |
| `custom --prompt-file path.md` | Your own one-off prompt; same frontmatter format as the bundled ones. |

Prompts live in [`presets/<lang>/*.md`](../presets/) — `presets/en/` for
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
