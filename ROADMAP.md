# Roadmap

Backlog of improvements grouped by impact ÷ effort. Tier 1 items are
implemented (or being implemented now). The rest live here so they
don't fall out of memory between sessions.

Format per item: short name, why it matters, sketch of approach,
rough size. "Size" is t-shirt: S = afternoon, M = day, L = multi-day.

---

## Tier 2 — bigger features, real value

### Folder digest (multi-chat single report)  M

Today `--folder NAME` runs one report per chat. Add `--digest` so it
merges every chat in a folder into one prompt → one report:

> "Today across MyTeam — chat A said X, chat B said Y, chat C said Z."

Map-reduce already supports this shape. The new piece is grouping
chats inside the formatter (similar to the existing topic-grouped
flat-forum output) plus an extra section header per chat in the user
template. Reduce-stage prompt needs a small tweak so it knows the map
chunks span chats, not just chunks within a chat.

### Scheduled runs  M

Daily morning digests are the main use case. Two paths:

- `unread schedule add --daily 09:00 analyze --folder Work --digest` — writes
  a launchd plist (mac) / systemd timer (linux) / cron line. State stored
  in `~/.unread/schedules.json`.
- `unread watch --interval 1h analyze --folder Work` — foreground loop, fires
  the analyze command on a fixed cadence, prints to stdout. Simpler, no
  OS integration; user runs it under `tmux` / `nohup`.

Start with `watch` (zero OS coupling). Add `schedule` if real users want
"set and forget".

### PII redaction before OpenAI  S

Pre-prompt scrub for: phone numbers, emails, IBANs, credit-card-shaped
sequences. Replace with `[redacted-phone]` etc. Optional `--redact` flag,
opt-in via `[analyze] redact = true` in config.

Implementation: regex-based, applied in `analyzer/filters.py:effective_text`
before the formatter sees the message. The redacted token replaces the PII
in the prompt but the original stays in the DB and the saved report (so
the user still sees their own data; only the LLM is shielded).

Unit test per regex (each matcher independently fuzzed).

### Per-chat default preset / enrich  S

`config.toml` already supports nested dicts. Add:

```toml
[chat."@somegroup"]
default_preset = "digest"
enrich_kinds = ["voice", "image"]

[chat."MyTeam Forum"]
default_preset = "action_items"
```

Looked up by `username` first, then by `title` (case-insensitive
substring). When the user runs `unread analyze @somegroup` with no
`--preset`, we read this map and apply.

Saves typing for users who run the same flag combo on the same chats
repeatedly.

### Stream LLM output  S

Long reduces sit silent for 30–60s. OpenAI Chat Completions supports
`stream=True` — pipe partial chunks to a Rich `Live` console.

Touch only `analyzer/openai_client.py:chat_complete` — every call site is
already isolated. `ChatResult` shape stays the same; we accumulate the
stream into `text` and surface partials via a callback.

### Cost-by-chat dashboard  S

`unread stats --by chat` already exists but bare. Add a `--top N` mode
showing biggest spenders, biggest cache savers, most unread, most active
senders. Two extra SQL queries; render as a Rich Table.

---

## Tier 3 — robustness / observability

### Telegram flood-wait UX  S

`tg/sync.py:backfill` currently raises `FloodWaitError` to the top.
Catch it, show:

```
Telegram says wait 300s — pausing… (Ctrl-C to abort)
[━━━━━─────] 132s remaining
```

Then resume automatically. Use `asyncio.sleep` + Rich Progress.
Telethon's `flood_sleep_threshold` parameter on the client also needs
tuning so short waits are absorbed silently.

### Map-output preview  S

Add a `--show-map` flag that prints each map chunk's output as an
expandable section in the saved report:

```
<details><summary>chunk 3 of 7 — covers msg_ids 4001..4500</summary>
<map output here>
</details>
```

Lets you debug "why did the reduce miss this point". Zero pipeline
churn — just an extra rendering pass in `_print_and_write`.

### Token-budget-aware reduce  M

If the map phase produces N chunks whose summaries together exceed
the reduce model's context, we currently raise. Better: chunk the
reduce too (reduce-of-reduces) — recursive map-reduce up to a small
depth (max 3 levels).

Useful for "year-of-history" runs. Implement in `analyzer/pipeline.py`
as a loop around the reduce step.

### Chat-language auto-detect  M

`audio_language = "ru"` is global. Voice transcription accuracy on
non-Russian chats drops noticeably with the wrong language hint.

Detect per chat by scanning text in already-synced messages (langdetect
or a small statistical detector on Cyrillic / Latin / CJK script
ratios). Cache on `chats` row (`language` column). Use it as the
default for `audio_language` unless the user overrides.

---

## Tier 4 — small UX polish (~1h each)

### Mark-read undo  S

Store the previous `read_inbox_max_id` on the `chats` row before
advancing. `unread unread <chat>` rolls it back via Telethon's
`ReadHistoryRequest` with the old `max_id`.

### Backfill progress bar  S

`get_messages(limit=0).total` returns the chat-wide total without
fetching. Use it to show `[━━━━━──────] 4321/12000` during sync.
Just a Rich Progress around the existing iter loop.

### `unread open <chat>` deep-link  S

`open tg://resolve?domain=somegroup` (mac) / `xdg-open …` (linux)
opens the chat in the Telegram app. Handy after a report says
"see msg #4321" — one keystroke to go look.

For private chats: `tg://openmessage?user_id=…&message_id=…`.

### Wizard "filter by folder" step  S

Currently you pick from all 651 dialogs. Add an optional first step:
"🗂 Filter by folder…" that narrows the list. The folder column already
exposes the data; this turns it into a real filter.

### Run-history command  S

`unread history <chat>` lists previous analysis runs (date, preset,
msg_count, cost) from the `analysis_runs` table. Data is already
written but never displayed. Useful for "what preset did I use last
time".

### Web UI  L

Many users are CLI-averse. Out of scope unless a clear demand
materializes — but if it does, the current architecture already
supports it: `analyzer.pipeline.run_analysis` takes typed inputs and
returns a typed result. A FastAPI server is ~200 lines on top.

---

## Architectural cleanup (slow boring work, real payoff)

### Move wizard out of `interactive.py`  M

File is ~1700 lines. Split:

- `wizard/picker.py` — chat / thread / period / enrich pickers
- `wizard/state.py` — step machine + `InteractiveAnswers` dataclass
- `wizard/run.py` — `run_interactive_analyze` / `run_interactive_dump`
- `wizard/expand_printable.py` — the non-ASCII filter shim

Already cohesive enough that the split is mostly mechanical.

### Replace per-CLI-command lazy-import boilerplate  S

Every `cli.py` handler does `from … import cmd_x`. A small registry
decorator could let each handler module declare itself:

```python
@register_command("analyze", panel="Main")
async def cmd_analyze(...): ...
```

Cuts 30+ lines from `cli.py` and centralizes the panel assignment so
all panel decisions are auditable in one place.

### Multi-provider abstraction  M

`openai_client.py` could grow a thin adapter layer
(`AnthropicClient`, `OllamaClient`) returning the same `ChatResult`.

Useful for:
- Cheaper local models for the map phase
- A fallback when OpenAI is down
- Privacy-sensitive users who don't want OpenAI to see their messages
  at all

The pricing table already keys by model name — works for any
provider's labels. Build a `ChatProvider` Protocol; implement
`OpenAIChatProvider` (current code) and `AnthropicChatProvider` first.

---

## Done (Tier 1)

The items below are implemented; this section becomes the changelog
when we cut a release.

- `unread ask "question"` — Q&A across synced corpus
- `--max-cost N` budget guard on `analyze` / `dump`
- Wizard reorder: period → enrich, with period-scoped media counts
- `unread doctor` — preflight / health check
- `unread backup` / `unread restore` for `data.sqlite`
- `--post-saved` — push analysis result to Telegram Saved Messages
