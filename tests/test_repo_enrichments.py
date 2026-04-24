"""media_enrichments / link_enrichments repo methods.

Covers the compat view on `media_transcripts` so existing callers keep working.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from analyzetg.db.repo import Repo


@pytest.fixture
async def repo(tmp_path: Path):
    r = await Repo.open(tmp_path / "t.sqlite")
    try:
        yield r
    finally:
        await r.close()


async def test_put_and_get_media_enrichment(repo: Repo):
    await repo.put_media_enrichment(
        42,
        "image_description",
        "a red cube",
        model="gpt-4o-mini",
        cost_usd=0.0012,
    )
    got = await repo.get_media_enrichment(42, "image_description")
    assert got is not None
    assert got["content"] == "a red cube"
    assert got["model"] == "gpt-4o-mini"


async def test_transcript_alias_still_works(repo: Repo):
    """Legacy put_media_transcript / get_media_transcript go through the new
    table — callers that haven't been ported yet keep working.
    """
    await repo.put_media_transcript(
        doc_id=7,
        transcript="hello world",
        model="whisper-1",
        duration_sec=15,
        language="en",
        cost_usd=0.0009,
    )
    got = await repo.get_media_transcript(7)
    assert got is not None
    assert got["transcript"] == "hello world"
    # Under the hood it's a media_enrichments row of kind='transcript'.
    raw = await repo.get_media_enrichment(7, "transcript")
    assert raw is not None
    assert raw["content"] == "hello world"


async def test_legacy_media_transcripts_table_is_migrated(tmp_path: Path):
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE media_transcripts (
            doc_id INTEGER PRIMARY KEY,
            file_sha1 TEXT,
            duration_sec INTEGER,
            transcript TEXT NOT NULL,
            model TEXT,
            language TEXT,
            cost_usd REAL,
            created_at TIMESTAMP
        );
        INSERT INTO media_transcripts(
            doc_id, file_sha1, duration_sec, transcript, model, language, cost_usd, created_at
        )
        VALUES(7, 'sha', 15, 'legacy hello', 'whisper-1', 'en', 0.001, '2026-04-24T00:00:00+00:00');
        """
    )
    conn.close()

    repo = await Repo.open(db)
    try:
        got = await repo.get_media_transcript(7)
        assert got is not None
        assert got["transcript"] == "legacy hello"
        raw = await repo.get_media_enrichment(7, "transcript")
        assert raw is not None
        assert raw["content"] == "legacy hello"
    finally:
        await repo.close()


async def test_different_kinds_coexist_for_same_doc(repo: Repo):
    # Same Telegram doc_id might have BOTH a transcript (from audio) and a
    # description (from a vision model extracting the poster frame). They
    # must not collide on the PRIMARY KEY (doc_id, kind).
    await repo.put_media_enrichment(100, "transcript", "voice text")
    await repo.put_media_enrichment(100, "image_description", "frame desc")
    t = await repo.get_media_enrichment(100, "transcript")
    d = await repo.get_media_enrichment(100, "image_description")
    assert t and t["content"] == "voice text"
    assert d and d["content"] == "frame desc"


async def test_link_enrichment_roundtrip(repo: Repo):
    await repo.put_link_enrichment(
        "hash123",
        "https://example.com/foo",
        "A short summary",
        title="Example Foo",
        model="gpt-5.4-nano",
        cost_usd=0.0004,
    )
    got = await repo.get_link_enrichment("hash123")
    assert got is not None
    assert got["url"] == "https://example.com/foo"
    assert got["summary"] == "A short summary"
    assert got["title"] == "Example Foo"


async def test_link_enrichment_upsert_updates(repo: Repo):
    await repo.put_link_enrichment("h", "u", "old", title="t", model="m", cost_usd=0.0)
    await repo.put_link_enrichment("h", "u", "new", title="t2", model="m2", cost_usd=0.01)
    got = await repo.get_link_enrichment("h")
    assert got is not None
    assert got["summary"] == "new"
    assert got["title"] == "t2"
