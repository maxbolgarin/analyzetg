-- 002_reactions: store per-message reactions as a compact JSON blob
--                 { "<emoji-or-custom-id>": <count>, ... }
-- Read on sync (tg/sync.py:normalize), rendered by analyzer/formatter.py as
-- `[reactions: 👍×3 ❤×1]` so the LLM can weight frequently-reacted messages.

ALTER TABLE messages ADD COLUMN reactions TEXT;
