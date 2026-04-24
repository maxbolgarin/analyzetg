-- analyzetg schema. Mirrors the tables described in docs/analyzetg-spec.md §4.
-- Applied through migrations/001_initial.sql; re-read here for reference.

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

CREATE TABLE IF NOT EXISTS sync_state (
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL DEFAULT 0,
    last_msg_id    INTEGER,
    last_synced_at TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
);

-- Generalized enrichment cache: transcripts, image descriptions, doc extracts,
-- etc. all share this table keyed by (doc_id, kind). `media_transcripts` is
-- kept as a view for compat — see migrations/002_media_enrichments.sql.
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
    created_at        TIMESTAMP
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
