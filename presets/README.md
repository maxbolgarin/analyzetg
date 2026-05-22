# Presets

Each `.md` here is one analysis preset for `unread analyze --preset <name>`.
Files starting with `_` (`_base.md`, `_reduce.md`) are not presets — they
are shared building blocks for the map-reduce pipeline.

Presets live in per-language directories under `presets/<lang>/`. The
`presets/en/` and `presets/ru/` trees are autonomous: a preset doesn't
have to exist in every language. The wizard reads from
`settings.locale.report_language` (falling back to `language`) to pick
the active directory.

## Visible presets

Listed in the order they appear in the interactive picker. `summary` is
the default when no `--preset` flag is passed.

| Preset | What you get | When to reach for it |
|---|---|---|
| `summary` | Concentrate of the chat: TL;DR + main insights + ideas/decisions + worth-reading messages | Default — quickly tell whether a chat is worth reading deeper |
| `tldr` | Two or three sentences in one paragraph, no structure | Absolute shortest read; phone-screen scan |
| `digest` | 5–10 most important themes, 1–2 lines each | Scanning digest of an active chat |
| `highlights` | Top 5–15 most valuable messages, ranked | "Just show me the gems" |
| `quotes` | Memorable quotes verbatim, with author + citation | Save the lines worth keeping |
| `links` | External URLs grouped by theme | Collect tools / articles shared in the chat |
| `action_items` | Markdown table: who / what / deadline / status | Find what needs to be done after the conversation |
| `decisions` | Markdown table: decision / who / when / rationale | "What did we agree on?" |
| `questions` | Open questions table — unanswered / partial / no-consensus | Things to come back to |
| `reactions` | Top reaction-driven messages, grouped by reaction type | What the chat actually responded to |

Every preset that emits per-message citations renders them as
`[#12345](https://t.me/username/12345)` (or `https://t.me/c/<id>/12345`
for private chats; `.../<thread_id>/12345` for forum topics). Public
chats with no username and 1:1 DMs don't get a hyperlink — the preset
falls back to plain `#12345`.

## Auto-selected (hidden) presets

These have `hidden: true` and don't appear in the wizard picker — the
analysis pipeline routes inputs to them automatically. The CLI's
`--preset` flag still accepts the name explicitly.

| Preset | Selected by | Purpose |
|---|---|---|
| `single_msg` | `unread analyze https://t.me/.../<msg_id>` for one voice note / video-circle / long post | Tight summary of one message |
| `multichat` | `unread tg chats run` (batch) and the `unread @group --dry-run` flow | Cross-chat synthesis: per-chat short answer in one report |
| `video` | YouTube URL — `unread <youtube-url>` | Transcript summary with time-stamped citations |
| `website` | Article / blog / docs URL — `unread <web-url>` | Page summary: TL;DR + key claims + key quotes |

## Forum chats

When the analyzed chat is a Telegram forum, the preamble's link
template includes the `thread_id`:
`https://t.me/{chat}/{thread_id}/{msg_id}`. Presets don't need to know
about this — the template comes from the preamble.

Forum analysis has three modes (CLI flags):

- `--thread N` — one topic only. The preset sees that thread's
  messages and cites them via plain `#msg_id`.
- `--all-flat` — whole forum, flat. Messages from all topics appear
  interleaved; links omit `/thread/` but still navigate to the right
  message.
- `--all-per-topic` — one report per topic. Reports land at
  `reports/{chat-slug}/{topic-slug}-{preset}-{stamp}.md`.

## File format

```markdown
---
name: summary                   # must match the filename stem
prompt_version: v1              # bump invalidates analysis_cache rows
description: short one-liner    # shown in the wizard picker
hidden: false                   # true = exclude from wizard picker
needs_reduce: true              # require a reduce stage in map-reduce
filter_model: gpt-5.4-nano      # model for the map phase (per-chunk)
final_model: gpt-5.4-mini       # model for the final answer / single-chunk
output_budget_tokens: 2000      # max tokens for the final answer
map_output_tokens: 1500         # max tokens per chunk in the map phase
max_chunk_input_tokens: 35000   # optional: hard input cap per chunk (e.g. video)
enrich: [link]                  # optional: media enrichments to enable
---
System-prompt body. Sets the model's role and constraints.

---USER---

User-prompt body (the per-call instruction). Must include the four
pipeline placeholders: {period}, {title}, {msg_count}, {messages}.

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
```

## What the model sees in `{messages}`

Per-message format:

```
[HH:MM #12345] sender_name [fwd: src] [voice 0:23]: ↩reply_sender text
```

- `#12345` is the `msg_id`. Use it in citations.
- The preamble carries one of:
  - `Message link: https://t.me/...{msg_id}` — substitute `{msg_id}` to
    build the link.
  - No template — that means the chat doesn't support t.me links (1:1
    DM with no username); presets fall back to plain `#12345`.

Untrusted message bodies are wrapped in
`<<<UNTRUSTED_CONTENT id=…>>> … <<<END_UNTRUSTED>>>` markers. The base
prompt instructs the model to treat anything inside as data, not
instructions — so prompt-injection attempts in chat content can't hijack
the analysis.

## Adding a custom preset

1. Copy any file (e.g. `summary.md` → `my_preset.md`) under
   `presets/<lang>/`.
2. Update the `name:` field to match the new filename stem and rewrite
   the prompts.
3. Run: `uv run unread analyze @chat --preset my_preset`.

For a one-off prompt without registering a file, use
`--preset custom --prompt-file path/to/my.md` — same format. A custom
prompt-file with no frontmatter gets the project's default settings;
add a frontmatter block to override them.

## Cache

`prompt_version` from the frontmatter is part of the `analysis_cache`
key. As long as the version doesn't change, repeated runs of the same
preset on the same messages come from cache (free). Bump it when you
materially change the prompt body. Edits to `_base.md`, `_reduce.md`,
or the forum addendum bump `BASE_VERSION` in
`unread/analyzer/prompts.py` instead — that bust every preset's cache
in one go.
