"""`website_pages` table: schema + put/get + idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.db.repo import Repo


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_put_and_get_round_trip(repo: Repo) -> None:
    await repo.put_website_page(
        page_id="abc1234567890def",
        url="https://example.com/article",
        normalized_url="https://example.com/article",
        domain="example.com",
        title="My Article",
        site_name="Example Blog",
        author="Jane Doe",
        published="2024-03-15",
        language="en",
        word_count=500,
        paragraphs=["First paragraph.", "Second paragraph."],
        content_hash="hash1",
        extractor="trafilatura",
        raw_html_size=12345,
    )
    row = await repo.get_website_page("abc1234567890def")
    assert row is not None
    assert row["title"] == "My Article"
    assert row["site_name"] == "Example Blog"
    assert row["author"] == "Jane Doe"
    assert row["published"] == "2024-03-15"
    assert row["language"] == "en"
    assert row["word_count"] == 500
    assert row["content_hash"] == "hash1"
    assert row["extractor"] == "trafilatura"
    assert row["raw_html_size"] == 12345
    assert row["fetched_at"] is not None

    import json

    assert json.loads(row["paragraphs_json"]) == ["First paragraph.", "Second paragraph."]


async def test_get_missing_returns_none(repo: Repo) -> None:
    assert await repo.get_website_page("nonexistent") is None


async def test_put_overwrites_existing(repo: Repo) -> None:
    """Re-putting the same page updates fields without UNIQUE collision."""
    common = {
        "page_id": "pid1",
        "url": "https://example.com/x",
        "normalized_url": "https://example.com/x",
        "domain": "example.com",
        "language": "en",
        "raw_html_size": 1000,
    }
    await repo.put_website_page(
        **common,
        title="v1",
        site_name="site",
        author=None,
        published=None,
        word_count=100,
        paragraphs=["first"],
        content_hash="h1",
        extractor="trafilatura",
    )
    await repo.put_website_page(
        **common,
        title="v2 — refreshed",
        site_name="site (renamed)",
        author="new author",
        published="2024-04-01",
        word_count=200,
        paragraphs=["first updated", "second"],
        content_hash="h2",
        extractor="beautifulsoup",
    )
    row = await repo.get_website_page("pid1")
    assert row is not None
    assert row["title"] == "v2 — refreshed"
    assert row["site_name"] == "site (renamed)"
    assert row["author"] == "new author"
    assert row["published"] == "2024-04-01"
    assert row["word_count"] == 200
    assert row["content_hash"] == "h2"
    assert row["extractor"] == "beautifulsoup"

    import json

    assert json.loads(row["paragraphs_json"]) == ["first updated", "second"]


async def test_schema_idempotent_on_reopen(tmp_path: Path) -> None:
    """Open + close + reopen — schema script applies cleanly twice."""
    db = tmp_path / "ws.sqlite"
    r1 = await Repo.open(db)
    await r1.put_website_page(
        page_id="x",
        url="https://example.com/x",
        normalized_url="https://example.com/x",
        domain="example.com",
        title=None,
        site_name=None,
        author=None,
        published=None,
        language=None,
        word_count=0,
        paragraphs=["body"],
        content_hash="h",
        extractor="trafilatura",
        raw_html_size=0,
    )
    await r1.close()
    r2 = await Repo.open(db)
    row = await r2.get_website_page("x")
    assert row is not None
    assert row["title"] is None
    await r2.close()
