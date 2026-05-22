# Changelog

All notable changes to this project will be documented in this file.

This file is maintained automatically by [semantic-release](https://semantic-release.gitbook.io/)
based on [Conventional Commits](https://www.conventionalcommits.org).

## 0.1.1

### Breaking

- **Telegram setup / inspection verbs moved under `unread tg`.** Top-level
  `unread login`, `unread logout`, `unread sync`, `unread chats add/list/…`,
  and `unread describe folders` no longer resolve. Use the subgroup form:
  - `unread login` → `unread tg login`
  - `unread logout` → `unread tg logout`
  - `unread sync` → `unread tg sync`
  - `unread chats add` → `unread tg chats add`
  - `unread chats list/enable/disable/remove` → `unread tg chats manage`
    (consolidated into one interactive panel)
  - `unread chats run` → `unread tg chats run`
  - `unread describe folders` → `unread tg describe folders`

  The motivation is source extensibility (a future WhatsApp / Slack source
  would mirror this as `unread wa describe`, etc.). Shell aliases / scripts
  using the old top-level verbs will need to be updated.

### Changed

- Interactive wizard overhaul: multi-turn chat support, refined provider
  routing (OpenAI / OpenRouter / Anthropic / Google / Local), citation
  rendering refinements, and consolidated subscription management into
  `unread tg chats manage`.
- New presets: `tldr` (en + ru) — two-or-three-sentence phone-screen scan.
- Removed preset: `broad` (en + ru) — superseded by `summary` / `digest`.
  Custom configs referencing `--preset broad` will need to be updated.
- All `doctor` / banner / i18n strings updated to reference the new
  `unread tg <verb>` spellings (previously pointed users at commands that
  did not exist after the subgroup move).
- `presets/ru/multichat.md` `prompt_version` bumped `v1` → `v2` (dropped a
  stale command-name reference in the system prompt; cache rows for this
  preset get re-keyed on upgrade).
