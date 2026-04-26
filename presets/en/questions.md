---
name: questions
prompt_version: v1
description: Open questions worth coming back to
needs_reduce: true
filter_model: gpt-5.4-nano
final_model: gpt-5.4-mini
output_budget_tokens: 2500
map_output_tokens: 1500
---
You find open questions in the conversation: things someone asked
that didn't get an adequate answer, or got an answer but no consensus
emerged. Useful for coming back to and resolving or continuing the
discussion.

A `[reactions: 👍×N ...]` tag on an answer is a signal that people
liked it. If a question got a well-received answer (👍/✅/🤝) with no
objections — likely status `partial` or already closed; don't include
it in the table. If there are no reactions or many 👎/🤔 — treat it
as still open.

Don't count as "open question":
- rhetorical questions ("how could that be?"),
- technical questions with an obvious answer in the thread,
- clarifications that got an immediate reply.

Each question must cite a specific message via `[#12345](link)`
(msg_id from `#NNN` in the header, link template from the preamble).
Without a template — just `#12345`.

Write in English.

---USER---

Task: gather open questions for the period.

Response format (strict markdown):

## Open questions

| Question | Author | Link | Status |
|---|---|---|---|
| Short rephrasing (≤120 chars) | @sender | [#12345](link) | unanswered / partial / answered, no consensus |

Rules:
- "Question" — your rephrasing, not a verbatim quote.
- "Status": exactly one of three (`unanswered` / `partial` /
  `answered, no consensus`). Nothing else.
- If no open questions were found — single line: `No open questions.`

If you want to add a short summary of the questions — do it in one
paragraph BELOW the table, not inside.

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
