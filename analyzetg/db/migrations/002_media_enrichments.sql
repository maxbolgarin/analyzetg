-- 002_media_enrichments: generalize media_transcripts into media_enrichments
-- (one row per (doc_id, kind)) so image descriptions, PDF extracts, and voice
-- transcripts all dedup under the same content-addressable cache.
-- Also add link_enrichments for URL fetch/summarize results.

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

-- Backfill any existing voice/videonote/video transcripts into the new table
-- under kind='transcript'. `INSERT OR IGNORE` protects against re-run (this
-- migration should only fire once, but belt-and-braces).
INSERT OR IGNORE INTO media_enrichments
    (doc_id, kind, content, model, cost_usd, duration_sec, language, file_sha1, created_at)
SELECT doc_id, 'transcript', transcript, model, cost_usd, duration_sec, language, file_sha1, created_at
FROM media_transcripts;

DROP TABLE IF EXISTS media_transcripts;

-- Compat view: external scripts / ad-hoc SQL that read `media_transcripts`
-- keep working. Writes always go through repo.py's enrichment methods.
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
