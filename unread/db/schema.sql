-- unread schema — single source of truth.
--
-- Applied directly by `Repo.open()` via `_apply_schema()` on every connect.
-- Every statement uses `IF NOT EXISTS`, so re-applying against an existing DB
-- is a no-op. There are no migrations; when the schema changes, edit this
-- file and either (a) rely on SQLite's idempotency if the change is
-- additive-only, or (b) document a manual "delete storage/data.sqlite and
-- re-sync" step for destructive changes.

CREATE TABLE IF NOT EXISTS chats (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    title          TEXT,
    username       TEXT,
    linked_chat_id INTEGER,
    first_seen_at  TIMESTAMP,
    updated_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id              INTEGER NOT NULL,
    thread_id            INTEGER NOT NULL DEFAULT 0,
    title                TEXT,
    source_kind          TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    start_from_msg_id    INTEGER,
    start_from_date      TIMESTAMP,
    transcribe_voice     INTEGER DEFAULT 1,
    transcribe_videonote INTEGER DEFAULT 1,
    transcribe_video     INTEGER DEFAULT 0,
    -- Per-subscription analyze defaults consumed by `unread chats run`. NULL /
    -- empty values fall back to config / CLI defaults; explicit values
    -- here let `unread chats run` walk every enabled sub and analyze each one
    -- with its own settings without re-prompting.
    preset               TEXT DEFAULT 'summary',
    period               TEXT DEFAULT 'unread',
    enrich_kinds         TEXT,            -- CSV (e.g. 'voice,link'); NULL = config defaults
    mark_read            INTEGER DEFAULT 1,
    post_to              TEXT,            -- chat ref to post the report to (`me`, @chan, …)
    added_at             TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id          INTEGER NOT NULL,
    msg_id           INTEGER NOT NULL,
    thread_id        INTEGER,
    date             TIMESTAMP NOT NULL,
    sender_id        INTEGER,
    sender_name      TEXT,
    text             TEXT,
    reply_to         INTEGER,
    forward_from     TEXT,
    media_type       TEXT,
    media_doc_id     INTEGER,
    media_duration   INTEGER,
    transcript       TEXT,
    transcript_model TEXT,
    reactions        TEXT,   -- JSON object: {"<emoji|custom_id>": <count>}
    PRIMARY KEY (chat_id, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_date
    ON messages(chat_id, thread_id, date);
CREATE INDEX IF NOT EXISTS idx_msg_has_media
    ON messages(chat_id, media_type) WHERE media_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_msg_untranscr
    ON messages(chat_id, media_doc_id)
    WHERE media_doc_id IS NOT NULL AND transcript IS NULL;
-- Companion index for untranscribed_media() queries that DON'T pin a chat_id
-- (e.g. `transcribe` across the whole DB). Without this, the scan skips the
-- chat_id-leading partial index above and degrades to a full table scan.
CREATE INDEX IF NOT EXISTS idx_msg_untranscr_all
    ON messages(date)
    WHERE media_doc_id IS NOT NULL AND transcript IS NULL AND media_type IS NOT NULL;

CREATE TABLE IF NOT EXISTS sync_state (
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL DEFAULT 0,
    last_msg_id    INTEGER,
    last_synced_at TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
);

-- Generalized enrichment cache: transcripts, image descriptions, doc extracts,
-- etc. all share this table keyed by (doc_id, kind). A `media_transcripts`
-- view below preserves the pre-unification read API for ad-hoc SQL.
CREATE TABLE IF NOT EXISTS media_enrichments (
    doc_id       INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    model        TEXT,
    cost_usd     REAL,
    duration_sec INTEGER,
    language     TEXT,
    file_sha1    TEXT,
    extra_json   TEXT,
    created_at   TIMESTAMP,
    PRIMARY KEY (doc_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_media_enrich_kind
    ON media_enrichments(kind);

-- Compat view so ad-hoc SQL / external scripts reading `media_transcripts`
-- keep working. All writes go through `Repo.put_media_enrichment`.
CREATE VIEW IF NOT EXISTS media_transcripts AS
    SELECT doc_id, file_sha1, duration_sec, content AS transcript,
           model, language, cost_usd, created_at
    FROM media_enrichments
    WHERE kind = 'transcript';

CREATE TABLE IF NOT EXISTS link_enrichments (
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    summary     TEXT NOT NULL,
    title       TEXT,
    model       TEXT,
    cost_usd    REAL,
    fetched_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_link_enrich_fetched
    ON link_enrichments(fetched_at);

-- One embedding row per (chat_id, msg_id, model). Built lazily by
-- `unread ask --build-index`; `unread ask --semantic` reads them. Vector is
-- `array.array('f', floats).tobytes()` — float32, native byte order.
-- Re-embedding a message under a new model produces a new row, not an
-- update, so old answers stay reproducible if you ever switch back.
CREATE TABLE IF NOT EXISTS message_embeddings (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    created_at TIMESTAMP,
    PRIMARY KEY (chat_id, msg_id, model)
);

CREATE INDEX IF NOT EXISTS idx_msg_emb_chat
    ON message_embeddings(chat_id, model);

CREATE TABLE IF NOT EXISTS analysis_cache (
    batch_hash        TEXT PRIMARY KEY,
    preset            TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    result            TEXT NOT NULL,
    prompt_tokens     INTEGER,
    cached_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          REAL,
    truncated         INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP
);

-- One row per (chat_id, thread_id) — the args of the *most recent*
-- successful `unread analyze` run on that scope. Used by the wizard's
-- "🔁 Repeat last run" entry to reconstruct flags without remembering
-- them. JSON because the cmd_analyze surface adds flags often.
CREATE TABLE IF NOT EXISTS chat_last_run_args (
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    args_json  TEXT NOT NULL,
    updated_at TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL DEFAULT 0,
    preset         TEXT NOT NULL,
    from_date      TIMESTAMP,
    to_date        TIMESTAMP,
    msg_count      INTEGER,
    chunk_count    INTEGER,
    batch_hashes   TEXT,
    final_result   TEXT,
    total_cost_usd REAL,
    created_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    cached_tokens     INTEGER,
    completion_tokens INTEGER,
    audio_seconds     INTEGER,
    cost_usd          REAL,
    context           TEXT,
    created_at        TIMESTAMP
);

-- Persistent user settings — overrides for the [locale] / [openai] config
-- blocks set via `unread settings`. Applied on top of config.toml on every
-- repo open so the user can save their language preferences once and
-- forget. Keys use dotted paths matching the config schema, e.g.
-- "locale.language" or "openai.audio_language". Empty string for value
-- means "no override" (we still store the row to keep the user's history
-- intact); use DELETE to remove an override entirely.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- Persisted secrets written by `unread tg init` — Telegram api_id /
-- api_hash and the OpenAI api_key. Lets a user delete `~/.unread/.env`
-- after the first successful interactive setup and keep working off
-- the saved values. Schema mirrors `app_settings`; kept in a separate
-- table so the `unread settings` CLI can't accidentally surface or
-- mutate them. Allowlisted keys: telegram.api_id, telegram.api_hash,
-- openai.api_key.
CREATE TABLE IF NOT EXISTS secrets (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- One row per analyzed YouTube video. Reused across runs: a re-analyze of
-- the same `video_id` skips both yt-dlp metadata and Whisper. Transcript
-- is stored inline; the audio file itself is not retained.
--
-- `transcript_source` ∈ {'captions', 'audio'} captures whether the text
-- came from YouTube's own captions (free) or Whisper transcription
-- (Whisper model + cost recorded separately).
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id            TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    channel_id          TEXT,
    channel_title       TEXT,
    channel_url         TEXT,
    description         TEXT,
    upload_date         TEXT,        -- yt-dlp returns YYYYMMDD; stored as-is
    duration_sec        INTEGER,
    view_count          INTEGER,
    like_count          INTEGER,
    tags                TEXT,        -- JSON array
    language            TEXT,
    transcript          TEXT,
    transcript_source   TEXT,        -- 'captions' | 'audio'
    transcript_model    TEXT,        -- whisper model id when source='audio'
    transcript_cost_usd REAL,
    transcript_timed_json TEXT,      -- JSON [[start_sec, "text"], …]; only for captions
    fetched_at          TIMESTAMP NOT NULL,
    transcribed_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_youtube_channel
    ON youtube_videos(channel_id, fetched_at);

-- One row per analyzed web page. Re-analyzing the same URL skips both the
-- HTTP fetch and the trafilatura/BS4 extraction. `paragraphs_json` is the
-- post-split content array (one entry per synthetic message for the LLM);
-- `content_hash` over that array drives `analysis_cache` invalidation so
-- a page edit produces a cache miss while an unchanged re-fetch reuses
-- the previous run.
CREATE TABLE IF NOT EXISTS website_pages (
    page_id         TEXT PRIMARY KEY,        -- sha256(normalized_url)[:16]
    url             TEXT NOT NULL,           -- as supplied by the user
    normalized_url  TEXT NOT NULL,
    domain          TEXT,
    title           TEXT,
    site_name       TEXT,
    author          TEXT,
    published       TEXT,                    -- ISO date when extractable
    language        TEXT,
    word_count      INTEGER,
    paragraphs_json TEXT NOT NULL,           -- JSON array of strings
    content_hash    TEXT NOT NULL,
    extractor       TEXT,                    -- 'trafilatura' | 'beautifulsoup'
    raw_html_size   INTEGER,
    fetched_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_website_pages_domain
    ON website_pages(domain, fetched_at);

-- One row per analyzed local file. Same shape as `website_pages` so the
-- analyzer's caching logic can treat files and websites symmetrically.
-- `content_hash` is sha256 of the extracted text, NOT of the source
-- bytes — so a re-saved Word doc with whitespace tweaks but identical
-- extracted prose still hits the cache. `paragraphs_json` is the array
-- of synthetic-message bodies fed to the LLM.
--
-- Stdin invocations write a row too, with `abs_path = ""` and
-- `kind = "stdin"`; `file_id` is sha256 of the stdin bytes so piping
-- the same content twice still hits cache.
CREATE TABLE IF NOT EXISTS local_files (
    file_id         TEXT PRIMARY KEY,        -- sha256(abs_path or stdin bytes)[:16]
    abs_path        TEXT NOT NULL,           -- "" for stdin
    name            TEXT NOT NULL,           -- basename or "stdin"
    kind            TEXT NOT NULL,           -- text | pdf | docx | audio | video | image | stdin
    extension       TEXT,
    content_hash    TEXT NOT NULL,
    paragraphs_json TEXT NOT NULL,
    extract_size    INTEGER,
    fetched_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_files_kind
    ON local_files(kind, fetched_at);
