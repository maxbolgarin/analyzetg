"""`Repo.list_source_cache` / `count_source_cache` / `purge_source_cache`.

Covers the per-source content caches (`website_pages`, `youtube_videos`,
`local_files`) used by `unread cache sources [purge]`. These live next
to `analysis_cache` but were not covered by `cache purge` (which only
deletes per-LLM-call result rows). Each test seeds a known set of rows,
exercises one filter or the all-entries gate, and checks the row count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.db.repo import Repo


async def _seed_website(repo: Repo, *, page_id: str, url: str, domain: str) -> None:
    await repo.put_website_page(
        page_id=page_id,
        url=url,
        normalized_url=url,
        domain=domain,
        title=f"Page {page_id}",
        site_name=domain,
        author=None,
        published=None,
        language="en",
        word_count=100,
        paragraphs=["body"],
        content_hash=f"hash-{page_id}",
        extractor="trafilatura",
        raw_html_size=1234,
    )


async def _seed_youtube(repo: Repo, *, video_id: str) -> None:
    await repo.put_youtube_video(
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        title=f"Video {video_id}",
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=120,
        view_count=None,
        like_count=None,
        tags=None,
        language="en",
        transcript="hello world",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
        transcript_lang_kind="manual",
    )


async def _seed_file(repo: Repo, *, file_id: str, name: str) -> None:
    await repo.put_local_file(
        file_id=file_id,
        abs_path=f"/tmp/{name}",
        name=name,
        kind="text",
        extension=".txt",
        content_hash=f"file-hash-{file_id}",
        paragraphs=["paragraph"],
        extract_size=42,
    )


async def test_source_cache_kinds_classmethod() -> None:
    """The `kind` allowlist is exposed for CLI flag validation."""
    assert Repo.source_cache_kinds() == ("website", "youtube", "file")


async def test_purge_website_by_url(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_website(repo, page_id="p1", url="https://a.example.com/x", domain="a.example.com")
        await _seed_website(repo, page_id="p2", url="https://b.example.com/y", domain="b.example.com")

        n = await repo.purge_source_cache("website", url="https://a.example.com/x")
        assert n == 1

        rows = await repo.list_source_cache("website")
        assert {r["id"] for r in rows} == {"p2"}
    finally:
        await repo.close()


async def test_purge_website_by_domain(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_website(
            repo, page_id="p1", url="https://zh.wikipedia.org/wiki/A", domain="zh.wikipedia.org"
        )
        await _seed_website(
            repo, page_id="p2", url="https://zh.wikipedia.org/wiki/B", domain="zh.wikipedia.org"
        )
        await _seed_website(
            repo, page_id="p3", url="https://en.wikipedia.org/wiki/C", domain="en.wikipedia.org"
        )

        n = await repo.purge_source_cache("website", domain="zh.wikipedia.org")
        assert n == 2

        rows = await repo.list_source_cache("website")
        assert {r["id"] for r in rows} == {"p3"}
    finally:
        await repo.close()


async def test_purge_all_requires_explicit_flag(tmp_path: Path) -> None:
    """Calling purge with no filters must NOT silently wipe the table —
    only `all_entries=True` is the wipe signal."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_website(repo, page_id="p1", url="https://a.com/x", domain="a.com")

        with pytest.raises(ValueError, match="refusing to wipe"):
            await repo.purge_source_cache("website")

        # And the row is still there.
        rows = await repo.list_source_cache("website")
        assert len(rows) == 1
    finally:
        await repo.close()


async def test_purge_all_entries_flag_wipes(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_website(repo, page_id="p1", url="https://a.com/x", domain="a.com")
        await _seed_website(repo, page_id="p2", url="https://b.com/y", domain="b.com")

        n = await repo.purge_source_cache("website", all_entries=True)
        assert n == 2

        rows = await repo.list_source_cache("website")
        assert rows == []
    finally:
        await repo.close()


async def test_purge_all_entries_rejects_filters(tmp_path: Path) -> None:
    """`all_entries=True` plus filters is contradictory; reject upfront."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        with pytest.raises(ValueError, match="mutually exclusive"):
            await repo.purge_source_cache("website", all_entries=True, domain="a.com")
    finally:
        await repo.close()


async def test_count_source_cache_returns_age_range(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_website(repo, page_id="p1", url="https://a.com/x", domain="a.com")
        await _seed_website(repo, page_id="p2", url="https://b.com/y", domain="b.com")

        c = await repo.count_source_cache("website")
        assert c["rows"] == 2
        assert c["oldest"] is not None
        assert c["newest"] is not None
    finally:
        await repo.close()


async def test_purge_youtube_and_files_kinds(tmp_path: Path) -> None:
    """The same purge helpers work across all three source-cache tables."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_youtube(repo, video_id="abc123")
        await _seed_youtube(repo, video_id="def456")
        await _seed_file(repo, file_id="f1", name="report.pdf")
        await _seed_file(repo, file_id="f2", name="notes.md")

        # Wipe all youtube rows; files untouched.
        assert await repo.purge_source_cache("youtube", all_entries=True) == 2
        assert await repo.list_source_cache("youtube") == []
        assert len(await repo.list_source_cache("file")) == 2

        # Now wipe the files.
        assert await repo.purge_source_cache("file", all_entries=True) == 2
        assert await repo.list_source_cache("file") == []
    finally:
        await repo.close()


async def test_domain_filter_on_kind_without_domain_col_matches_nothing(
    tmp_path: Path,
) -> None:
    """Regression: passing `--domain` to a kind with no domain column
    (youtube, file) must NOT silently wipe the table because the WHERE
    clause ended up empty. It should match zero rows instead."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        await _seed_youtube(repo, video_id="abc123")
        await _seed_youtube(repo, video_id="def456")

        # Domain doesn't apply to youtube → 0 deletes, all rows survive.
        n = await repo.purge_source_cache("youtube", domain="something.example.com")
        assert n == 0
        assert len(await repo.list_source_cache("youtube")) == 2

        # Same for file.
        await _seed_file(repo, file_id="f1", name="report.pdf")
        n = await repo.purge_source_cache("file", domain="x.com")
        assert n == 0
        assert len(await repo.list_source_cache("file")) == 1
    finally:
        await repo.close()


async def test_unknown_kind_raises(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        with pytest.raises(ValueError, match="Unknown source-cache kind"):
            await repo.list_source_cache("unknown-kind")
        with pytest.raises(ValueError, match="Unknown source-cache kind"):
            await repo.count_source_cache("unknown-kind")
        with pytest.raises(ValueError, match="Unknown source-cache kind"):
            await repo.purge_source_cache("unknown-kind", all_entries=True)
    finally:
        await repo.close()
