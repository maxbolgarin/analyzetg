"""URL detection + video-id extraction for the YouTube branch."""

from __future__ import annotations

import pytest

from analyzetg.youtube.urls import extract_video_id, is_youtube_url, video_url


class TestIsYoutubeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=jmzoJCn8evU",
            "https://www.youtube.com/watch?v=jmzoJCn8evU&list=RDjmzoJCn8evU&start_radio=1",
            "https://youtu.be/jmzoJCn8evU",
            "https://youtu.be/jmzoJCn8evU?t=42s",
            "https://m.youtube.com/watch?v=abc123",
            "https://music.youtube.com/watch?v=abc123",
            "https://youtube.com/shorts/abc123",
            "http://www.youtube.com/watch?v=abc123",
        ],
    )
    def test_recognized_shapes(self, url: str) -> None:
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "youtube",
            "watch?v=abc",
            "https://example.com/watch?v=abc",
            "https://t.me/c/123/456",
            "@somechannel",
            "12345",
            "ftp://youtube.com/watch?v=abc",
        ],
    )
    def test_rejects_non_youtube(self, url) -> None:
        assert is_youtube_url(url) is False


class TestExtractVideoId:
    def test_canonical_watch(self) -> None:
        assert extract_video_id("https://www.youtube.com/watch?v=jmzoJCn8evU") == "jmzoJCn8evU"

    def test_drops_list_and_start_radio(self) -> None:
        # The user's original example URL.
        url = "https://www.youtube.com/watch?v=jmzoJCn8evU&list=RDjmzoJCn8evU&start_radio=1"
        assert extract_video_id(url) == "jmzoJCn8evU"

    def test_short_form(self) -> None:
        assert extract_video_id("https://youtu.be/jmzoJCn8evU") == "jmzoJCn8evU"

    def test_short_form_with_query(self) -> None:
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42s") == "dQw4w9WgXcQ"

    def test_shorts(self) -> None:
        assert extract_video_id("https://www.youtube.com/shorts/abc123XYZ_-") == "abc123XYZ_-"

    def test_embed(self) -> None:
        assert extract_video_id("https://www.youtube.com/embed/abc123") == "abc123"

    def test_live(self) -> None:
        assert extract_video_id("https://www.youtube.com/live/abc123") == "abc123"

    def test_v_path(self) -> None:
        assert extract_video_id("https://www.youtube.com/v/abc123") == "abc123"

    def test_mobile(self) -> None:
        assert extract_video_id("https://m.youtube.com/watch?v=abc123") == "abc123"

    def test_music(self) -> None:
        assert extract_video_id("https://music.youtube.com/watch?v=abc123") == "abc123"

    def test_playlist_only_rejected(self) -> None:
        with pytest.raises(ValueError):
            extract_video_id("https://www.youtube.com/playlist?list=PLabc")

    def test_channel_handle_rejected(self) -> None:
        with pytest.raises(ValueError):
            extract_video_id("https://www.youtube.com/@somechannel")

    def test_non_youtube_host_rejected(self) -> None:
        with pytest.raises(ValueError):
            extract_video_id("https://example.com/watch?v=abc")

    def test_watch_without_v_rejected(self) -> None:
        with pytest.raises(ValueError):
            extract_video_id("https://www.youtube.com/watch?list=PL123")


def test_video_url_round_trip() -> None:
    assert video_url("jmzoJCn8evU") == "https://www.youtube.com/watch?v=jmzoJCn8evU"
    assert extract_video_id(video_url("dQw4w9WgXcQ")) == "dQw4w9WgXcQ"
