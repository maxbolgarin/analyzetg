"""`youtube_videos` table: schema + put/get + has_youtube_transcript + idempotency."""

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
    await repo.put_youtube_video(
        video_id="dQw4w9WgXcQ",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        channel_id="UCabc",
        channel_title="Rick Astley",
        channel_url="https://www.youtube.com/@RickAstleyYT",
        description="Music video",
        upload_date="20091025",
        duration_sec=212,
        view_count=1_500_000_000,
        like_count=17_000_000,
        tags=["music", "80s"],
        language="en",
        transcript="Never gonna give you up...",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
    )
    row = await repo.get_youtube_video("dQw4w9WgXcQ")
    assert row is not None
    assert row["title"] == "Never Gonna Give You Up"
    assert row["channel_title"] == "Rick Astley"
    assert row["transcript"].startswith("Never")
    assert row["transcript_source"] == "captions"
    assert row["duration_sec"] == 212
    # tags JSON-encoded round trip
    import json as _json

    assert _json.loads(row["tags"]) == ["music", "80s"]
    assert row["fetched_at"] is not None
    assert row["transcribed_at"] is not None


async def test_get_missing_returns_none(repo: Repo) -> None:
    assert await repo.get_youtube_video("nonexistent") is None


async def test_put_overwrites_existing(repo: Repo) -> None:
    """Re-putting the same video updates fields without UNIQUE collision."""
    common = {
        "video_id": "abc",
        "url": "https://www.youtube.com/watch?v=abc",
        "channel_id": None,
        "channel_url": None,
        "description": None,
        "upload_date": None,
        "view_count": None,
        "like_count": None,
        "tags": None,
        "language": None,
        "transcript_model": None,
        "transcript_cost_usd": 0.0,
    }
    await repo.put_youtube_video(
        **common,
        title="v1",
        channel_title="ch",
        duration_sec=100,
        transcript="first",
        transcript_source="captions",
    )
    await repo.put_youtube_video(
        **common,
        title="v2 — refreshed",
        channel_title="ch (renamed)",
        duration_sec=120,
        transcript="second",
        transcript_source="audio",
    )
    row = await repo.get_youtube_video("abc")
    assert row is not None
    assert row["title"] == "v2 — refreshed"
    assert row["channel_title"] == "ch (renamed)"
    assert row["duration_sec"] == 120
    assert row["transcript"] == "second"
    assert row["transcript_source"] == "audio"


async def test_persists_timed_transcript(repo: Repo) -> None:
    """Captions path: timed cues survive a put/get round-trip as JSON."""
    import json as _json

    cues = [(0, "Welcome."), (12, "First topic."), (754, "Mid-point.")]
    await repo.put_youtube_video(
        video_id="ttvid",
        url="https://www.youtube.com/watch?v=ttvid",
        title="Timed",
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=900,
        view_count=None,
        like_count=None,
        tags=None,
        language="en",
        transcript="Welcome. First topic. Mid-point.",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=cues,
    )
    row = await repo.get_youtube_video("ttvid")
    assert row is not None
    parsed = _json.loads(row["transcript_timed_json"])
    assert parsed == [list(c) for c in cues]


async def test_has_youtube_transcript(repo: Repo) -> None:
    assert await repo.has_youtube_transcript("missing") is False
    await repo.put_youtube_video(
        video_id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title=None,
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=None,
        view_count=None,
        like_count=None,
        tags=None,
        language=None,
        transcript=None,  # metadata only — no transcript yet
        transcript_source=None,
        transcript_model=None,
        transcript_cost_usd=None,
    )
    assert await repo.has_youtube_transcript("vid1") is False
    await repo.put_youtube_video(
        video_id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title=None,
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=None,
        view_count=None,
        like_count=None,
        tags=None,
        language=None,
        transcript="text now present",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
    )
    assert await repo.has_youtube_transcript("vid1") is True


async def test_schema_idempotent_on_reopen(tmp_path: Path) -> None:
    """Open + close + reopen — schema script applies cleanly twice."""
    db = tmp_path / "yt.sqlite"
    r1 = await Repo.open(db)
    await r1.put_youtube_video(
        video_id="x",
        url="https://www.youtube.com/watch?v=x",
        title=None,
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=None,
        view_count=None,
        like_count=None,
        tags=None,
        language=None,
        transcript="t",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
    )
    await r1.close()
    r2 = await Repo.open(db)
    row = await r2.get_youtube_video("x")
    assert row is not None
    assert row["transcript"] == "t"
    await r2.close()
