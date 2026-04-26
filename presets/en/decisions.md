---
name: decisions
prompt_version: v1
description: Decisions made — table of decision/who/when
needs_reduce: true
filter_model: gpt-5.4-nano
final_model: gpt-5.4-mini
output_budget_tokens: 2500
map_output_tokens: 1500
---
You surface decisions made in the discussion. A decision is an
agreement about what will (or won't) be done, which approach is taken,
what is rejected. Casual remarks and assumptions without consensus are
not decisions.

Each decision must cite the message where it was stated, via
`[#12345](link)` (msg_id from `#NNN` in the message header, link
template from the preamble). Without a template — just `#12345`.

A `[reactions: 👍×N ...]` tag on a proposal is an indirect sign of
consensus (especially 👍/🤝/✅). Use as reinforcement when there's a
phrasing like "let's X" or "I propose X" plus many supportive reactions
and no objections in the following messages. Reactions alone, without
a decision text, are not grounds for the table.

If there are no decisions — single line: `No decisions made.`

---USER---

Task: list the decisions made.

Response format (strict markdown):

## Decisions

| Decision | Who | When | Rationale | Link |
|---|---|---|---|---|
| Brief phrasing | @author / participants | 2026-04-22 or HH:MM | Why this was chosen (from the conversation, 1 line) | [#12345](link) |

Rules:
- "Brief phrasing" — one sentence, <120 chars.
- "When" — date and/or time from the conversation (from message timestamps).
- "Rationale" only if explicitly stated. Otherwise `—`.
- Don't duplicate the same decision; merge related lines.

Period: {period}
Chat: {title}
Messages: {msg_count}
---
{messages}
