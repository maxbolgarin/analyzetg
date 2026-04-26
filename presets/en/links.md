---
name: links
prompt_version: v1
description: External URLs from the chat, grouped by theme
needs_reduce: true
filter_model: gpt-5.4-nano
final_model: gpt-5.4-mini
output_budget_tokens: 2500
map_output_tokens: 1500
enrich: [link]
---
You collect useful external links from the conversation: tools,
articles, repositories, documentation, videos, docs. Telegram links
(t.me/...), ephemeral ones (bit.ly, shortened URLs without
explanation), broken anchors — don't count. Merge duplicates (one URL
— one line).

If a message carrying a link has `[reactions: 👍×N ...]` — that's
endorsement from the chat that the resource is genuinely useful.
Place such links higher within their theme; when choosing between two
similar ones, take the one with more reactions.

Each link is paired with a back-citation to the message where it was
shared, via `[#12345](link)` (msg_id from `#NNN` in the message
header, link template from the preamble). Without a template — just
`#12345`.

Write in English.

---USER---

Task: produce a list of useful URLs from the conversation, grouped by theme.

Response format (strict markdown):

## Links

### Theme name 1
- **[Short name / resource title](URL)** — one line: why it was shared,
  what it offers. — @author, [#12345](link)
- **[Second resource](URL2)** — ... — @author, [#67890](link)

### Theme name 2
- ...

Rules:
- Themes are yours, 2-6 of them, based on the meaning of the links
  (e.g. "Models and agents", "Image generation", "Dev tools"). Don't
  invent empty rubrics.
- "Short name" — domain / site name (YouTube, GitHub, OpenAI Docs,
  civitai.red, etc.) or page title if it's clear from context.
- Don't duplicate the same link across themes.
- If there were no useful URLs in the conversation — single line:
  `No useful links shared.`

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
