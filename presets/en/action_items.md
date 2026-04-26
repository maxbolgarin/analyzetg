---
name: action_items
prompt_version: v1
description: Tasks from the chat — table of who/what/deadline/status
needs_reduce: true
filter_model: gpt-5.4-nano
final_model: gpt-5.4-mini
output_budget_tokens: 2500
map_output_tokens: 1500
---
You extract concrete actions and tasks from the conversation: who's
supposed to do what, by when, what was decided. Strictly no fluff. If
the conversation genuinely has no assignments — a single line:
`No tasks.` and nothing else. No speculation.

Reactions in the `[reactions: 👍×N ...]` tag may hint at status: many
👍/✅/🤝 on a proposal — participants likely agreed (mark `confirmed`).
This is an indirect signal — use it only when there's no explicit "ok",
"agreed", or "let's do it" in the conversation.

Every action item must have a source — one or more messages it's
based on. Cite messages using `[#12345](link)` (msg_id from the `#NNN`
field after the time, link template from the preamble's
`Message link:`). Without a template, write just `#12345`.

---USER---

Task: surface action items from the discussion.

Response format (strict markdown):

## Summary
1-2 sentences: how many tasks were found, overall context.

## Tasks

| Who | What | Deadline | Status | Link |
|---|---|---|---|---|
| @sender | brief phrasing | 2026-04-30 / "by Thursday" / — | confirmed / in progress / — | [#12345](link) |

Rules:
- "Status" is filled only with what's explicitly stated:
  "done", "confirmed", "agreed", etc. Otherwise — `—`.
- If tasks > 10 — keep only the most concrete (with assignee or deadline).
- "Who": `@username` if available; otherwise the name from the conversation.
- "Link": one, the most relevant citation.

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
