-- 004_analysis_cache_truncated: track whether a cached result hit
-- output_budget_tokens. pipeline._call_cached already refuses to cache
-- truncated results, so new rows will always be 0 via normal flow. The
-- explicit column is defense-in-depth: if a future code path bypasses the
-- gate, the truncation status is persisted and surfaced on cache hit.

ALTER TABLE analysis_cache ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0;
