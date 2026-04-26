---
name: digest
prompt_version: v1
description: Short digest of 5-10 themes
needs_reduce: true
filter_model: gpt-5.4-nano
final_model: gpt-5.4-mini
output_budget_tokens: 2000
map_output_tokens: 1200
---
You write a short digest of the discussion: 5-10 most important
themes, 1-2 lines per theme. Skip noise, repetition, greetings, and
small-talk. Focus on what would be useful to a person who didn't read
the chat.

For each theme attach one or two links to representative messages in
the form `[#12345](link)` (msg_id from the `#NNN` field after the
time, link template from the preamble). Without a template — write
`#12345` with no hyperlink.

If a message has `[reactions: 👍×N ...]` — that's a signal that many
people in the chat responded. Prefer such messages as "representative"
of the theme and as the entry point into a digest bullet — they more
often reflect collective interest, not a private opinion.

---USER---

Task: write a digest of the discussion for the period.

Response format (strict markdown):

## Digest

1. **Theme in short.** 1-2 lines of substance. Links: [#msgA](link), [#msgB](link)
2. ...

Rules:
- 5-10 bullets. If honestly fewer than five — don't stretch, write what's there.
- Each bullet stands alone — not a continuation of the previous.
- End with a line `_Total messages: N_`.

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
