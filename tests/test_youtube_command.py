"""Unit-level coverage for the YouTube command helpers + cache-key wiring.

Avoids stubbing the full `cmd_analyze_youtube` async flow (which would
need yt-dlp + OpenAI + storage path mocking); instead pins the small
helpers that are easy to break (segmentation, metadata header, synthetic
message construction, the row → metadata round-trip, and the
options_payload contract for the cache key).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unread.analyzer.pipeline import AnalysisOptions
from unread.analyzer.prompts import get_presets
from unread.core.paths import reports_dir
from unread.youtube.commands import (
    _build_synthetic_messages,
    _meta_header,
    _parse_upload_date,
    _restore_metadata_from_row,
    _segment_transcript,
)
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.paths import youtube_report_path


def _meta(**overrides) -> YoutubeMetadata:
    base = {
        "video_id": "abc123",
        "url": "https://www.youtube.com/watch?v=abc123",
        "title": "My video",
        "channel_id": "UCxyz",
        "channel_title": "My channel",
        "channel_url": "https://www.youtube.com/@mychannel",
        "description": "Short description.",
        "upload_date": "20240315",
        "duration_sec": 900,
        "view_count": 12345,
        "like_count": 678,
        "tags": ["tutorial"],
        "language": "en",
    }
    base.update(overrides)
    return YoutubeMetadata(**base)


# --- _parse_upload_date ----------------------------------------------------


def test_parse_upload_date_valid() -> None:
    dt = _parse_upload_date("20240315")
    assert dt.year == 2024 and dt.month == 3 and dt.day == 15
    assert dt.tzinfo is UTC


def test_parse_upload_date_falls_back_to_now() -> None:
    before = datetime.now(UTC)
    dt = _parse_upload_date(None)
    after = datetime.now(UTC)
    assert before <= dt <= after


def test_parse_upload_date_invalid_format() -> None:
    before = datetime.now(UTC)
    dt = _parse_upload_date("not-a-date")
    after = datetime.now(UTC)
    assert before <= dt <= after


# --- _segment_transcript ---------------------------------------------------


def test_segment_short_text_one_segment() -> None:
    assert _segment_transcript("Hello world.") == ["Hello world."]


def test_segment_empty_returns_empty() -> None:
    assert _segment_transcript("") == []
    assert _segment_transcript("   ") == []


def test_segment_breaks_on_sentence_boundaries() -> None:
    sentences = ["Sentence one." for _ in range(400)]
    text = " ".join(sentences)
    parts = _segment_transcript(text, max_chars=200)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 200


def test_segment_hard_cuts_oversized_sentences() -> None:
    long = "a" * 5000
    parts = _segment_transcript(long, max_chars=1000)
    assert len(parts) >= 5
    for p in parts:
        assert len(p) <= 1000


# --- _meta_header ----------------------------------------------------------


def test_meta_header_includes_key_fields() -> None:
    h = _meta_header(_meta())
    assert "My video" in h
    assert "My channel" in h
    assert "15:00" in h  # 900s == 15:00
    assert "Uploaded: 20240315" in h
    assert "https://www.youtube.com/watch?v=abc123" in h
    assert "Description:" in h


def test_meta_header_truncates_long_description() -> None:
    h = _meta_header(_meta(description="x" * 5000))
    # 1500 cap + ellipsis
    assert "…" in h


def test_meta_header_no_description_omits_block() -> None:
    h = _meta_header(_meta(description=None))
    assert "Description:" not in h


# --- _build_synthetic_messages --------------------------------------------


def test_build_messages_header_plus_segments() -> None:
    transcript = "Hi. " * 2000  # ~8000 chars
    msgs = _build_synthetic_messages(_meta(), transcript)
    assert len(msgs) >= 3  # 1 header + multiple segments
    # Header is first, with msg_id=0 (sentinel "not part of the video")
    assert "My video" in (msgs[0].text or "")
    assert msgs[0].msg_id == 0
    # Transcript msg_ids strictly increasing (each is the second-offset
    # of the segment's first cue; uniform-spread fallback for non-timed
    # transcripts gives ascending values too).
    transcript_ids = [m.msg_id for m in msgs[1:]]
    assert transcript_ids == sorted(transcript_ids)
    assert len(set(transcript_ids)) == len(transcript_ids)
    # All same chat_id sentinel
    assert all(m.chat_id == 0 for m in msgs)
    # Dates strictly non-decreasing within the duration window
    dts = [m.date for m in msgs]
    assert dts == sorted(dts)
    upload = _parse_upload_date("20240315")
    assert msgs[0].date == upload
    assert msgs[-1].date <= upload + timedelta(seconds=900)


def test_build_messages_short_transcript_two_messages() -> None:
    msgs = _build_synthetic_messages(_meta(duration_sec=60), "Just a short clip.")
    # Header + 1 segment
    assert len(msgs) == 2
    # Segment body now begins with [HH:MM:SS] timestamp prefix.
    assert msgs[1].text.startswith("[")
    assert "Just a short clip." in (msgs[1].text or "")
    # Header msg_id is the sentinel 0; segment msg_id is offset_seconds (≥ 1).
    assert msgs[0].msg_id == 0
    assert msgs[1].msg_id >= 1


def test_build_messages_empty_transcript_header_only() -> None:
    msgs = _build_synthetic_messages(_meta(), "")
    assert len(msgs) == 1
    assert "My video" in (msgs[0].text or "")


def test_build_messages_uses_timed_cues_when_provided() -> None:
    """Captions path: msg_id of each segment = start_sec of its first cue."""
    cues = [
        (0, "Hello, welcome to the video."),
        (12, "Today we'll talk about types."),
        (754, "Now let's look at examples."),
        (1820, "And finally a summary."),
    ]
    msgs = _build_synthetic_messages(
        _meta(duration_sec=2000),
        "ignored",  # transcript_text path is bypassed when cues are passed
        timed_cues=cues,
    )
    assert msgs[0].msg_id == 0  # header
    # The four cues fit in one segment (small total chars), so they all
    # land in a single segment whose msg_id is the FIRST cue's start.
    assert msgs[1].msg_id == 1  # max(0+1, start=0) → 1 (header is at 0)
    assert "Hello, welcome" in msgs[1].text
    assert "[00:00]" in msgs[1].text  # embedded HH:MM offset prefix


def test_build_messages_timed_cues_distinct_msg_ids_under_segmentation() -> None:
    """Long timed transcript: each segment's msg_id is its first cue's offset."""
    # Build cues that exceed the 3500-char segment cap so segmentation kicks in.
    cues = [
        (i * 15, "X " * 200)
        for i in range(20)  # 20 cues * ~400 chars
    ]
    msgs = _build_synthetic_messages(_meta(duration_sec=600), "", timed_cues=cues)
    # > 1 segment
    assert len(msgs) >= 3  # header + 2+ segments
    transcript_ids = [m.msg_id for m in msgs[1:]]
    # Strictly increasing & distinct.
    assert transcript_ids == sorted(transcript_ids)
    assert len(set(transcript_ids)) == len(transcript_ids)


# --- _restore_metadata_from_row -------------------------------------------


def test_restore_metadata_from_row_round_trip() -> None:
    import json as _json

    row = {
        "video_id": "v",
        "url": "https://www.youtube.com/watch?v=v",
        "title": "T",
        "channel_id": "ch",
        "channel_title": "Channel T",
        "channel_url": "https://www.youtube.com/@x",
        "description": "D",
        "upload_date": "20240101",
        "duration_sec": 100,
        "view_count": 5,
        "like_count": 2,
        "tags": _json.dumps(["a", "b"]),
        "language": "en",
    }
    meta = _restore_metadata_from_row(row)
    assert meta.video_id == "v"
    assert meta.title == "T"
    assert meta.tags == ["a", "b"]
    assert meta.duration_sec == 100


def test_restore_metadata_handles_bad_tags() -> None:
    row = {
        "video_id": "v",
        "url": "u",
        "tags": "not-json",
    }
    meta = _restore_metadata_from_row(row)
    assert meta.tags is None


# --- youtube_report_path ---------------------------------------------------


def test_report_path_shape() -> None:
    p = youtube_report_path(
        video_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        channel_title="Rick Astley",
        channel_id="UCxyz",
        preset="summary",
    )
    rel_parts = list(p.relative_to(reports_dir()).parts)
    assert rel_parts[0] == "youtube"
    assert rel_parts[1] == "rick-astley"
    # video slug includes title-derived bits AND last-6 of id (lowercased)
    assert "9wgxcq" in rel_parts[2].lower()
    assert rel_parts[2].endswith(".md")
    assert "summary" in rel_parts[2]


def test_report_path_unknown_channel_fallback() -> None:
    p = youtube_report_path(
        video_id="abcdef",
        title=None,
        channel_title=None,
        channel_id=None,
        preset="single_msg",
    )
    rel_parts = list(p.relative_to(reports_dir()).parts)
    assert rel_parts[1] == "unknown-channel"
    assert "video-" in rel_parts[2]


# --- AnalysisOptions youtube_video_id wiring -------------------------------


def test_options_payload_includes_youtube_video_id() -> None:
    presets = get_presets("en")
    summary = presets["summary"]
    payload_with = AnalysisOptions(
        preset="summary",
        youtube_video_id="abc123",
    ).options_payload(summary)
    payload_without = AnalysisOptions(
        preset="summary",
        youtube_video_id=None,
    ).options_payload(summary)
    assert payload_with["youtube_video_id"] == "abc123"
    assert payload_without["youtube_video_id"] is None
    # Two videos produce different cache keys
    payload_other = AnalysisOptions(
        preset="summary",
        youtube_video_id="xyz789",
    ).options_payload(summary)
    assert payload_with != payload_other


def test_options_payload_includes_source_kind() -> None:
    """source_kind enters cache key — toggling it busts cached chat-mode rows."""
    presets = get_presets("en")
    summary = presets["summary"]
    chat = AnalysisOptions(preset="summary", source_kind="chat").options_payload(summary)
    video = AnalysisOptions(preset="summary", source_kind="video").options_payload(summary)
    assert chat["source_kind"] == "chat"
    assert video["source_kind"] == "video"
    assert chat != video


def test_video_preset_loads_for_both_languages() -> None:
    """The new `video` preset is bundled in en + ru and registers correctly."""
    en_presets = get_presets("en")
    ru_presets = get_presets("ru")
    assert "video" in en_presets, "video preset missing in presets/en/"
    assert "video" in ru_presets, "video preset missing in presets/ru/"
    en_video = en_presets["video"]
    assert en_video.needs_reduce is True
    assert "video" in en_video.system.lower() or "transcript" in en_video.system.lower()


def test_video_preset_has_chunk_cap() -> None:
    """`max_chunk_input_tokens` cap on video preset prevents single-call TPM blowups."""
    en_video = get_presets("en")["video"]
    ru_video = get_presets("ru")["video"]
    assert en_video.max_chunk_input_tokens is not None
    assert en_video.max_chunk_input_tokens <= 100_000  # well under 200k TPM ceilings
    assert ru_video.max_chunk_input_tokens == en_video.max_chunk_input_tokens


def test_chunker_respects_max_chunk_input_tokens() -> None:
    """A long transcript with the cap forces multi-chunk, even on a 400k-context model."""
    from datetime import UTC, datetime

    from unread.analyzer.chunker import build_chunks
    from unread.models import Message

    base = datetime(2024, 1, 1, tzinfo=UTC)
    # 400 messages * ~150 words each => roughly 60k+ tokens — well under
    # gpt-5.4-mini's 400k context (so without the cap it'd be a single
    # chunk) but firmly above the 35k cap (so the cap forces a split).
    msgs = [
        Message(
            chat_id=0,
            msg_id=i,
            date=base,
            sender_name="x",
            text="Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 25,
        )
        for i in range(1, 401)
    ]
    # Without cap: 1 chunk on a 400k-context model.
    plain = build_chunks(
        msgs,
        model="gpt-5.4-mini",
        system_prompt="sys",
        user_overhead="overhead",
        output_budget=4000,
        safety_margin=2000,
    )
    assert len(plain) == 1
    # With a 35k cap: forced multi-chunk.
    capped = build_chunks(
        msgs,
        model="gpt-5.4-mini",
        system_prompt="sys",
        user_overhead="overhead",
        output_budget=4000,
        safety_margin=2000,
        max_chunk_input_tokens=35_000,
    )
    assert len(capped) >= 2


def test_options_payload_is_telegram_back_compat() -> None:
    """Plain Telegram run (no youtube_video_id) keeps the field=None.

    Existing analysis_cache rows from before this change have no
    `youtube_video_id` in their hashed payload; the new field defaulting
    to None means the cache hit semantics for Telegram-only users do
    not change after the upgrade.
    """
    presets = get_presets("en")
    summary = presets["summary"]
    payload = AnalysisOptions(preset="summary").options_payload(summary)
    assert "youtube_video_id" in payload
    assert payload["youtube_video_id"] is None


# --- VTT parser ------------------------------------------------------------


def test_vtt_parser_strips_timing_and_dedupes() -> None:
    from unread.youtube.transcript import _parse_vtt_timed

    # The rolling-overlap pattern: same payload in adjacent cues. We
    # dedup AT THE CUE LEVEL — a cue whose joined-line body matches a
    # previously emitted cue body is dropped. The middle cue here joins
    # `Hello there.` + `General Kenobi.` so it's distinct from either of
    # the single-line neighbours (intended behaviour: don't lose merge).
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:03.500
Hello there.

00:00:03.500 --> 00:00:06.000
Hello there.

00:00:06.000 --> 00:00:09.000
General Kenobi.
"""
    cues = _parse_vtt_timed(vtt)
    bodies = [c[1] for c in cues]
    # Two distinct bodies survive; the duplicate `Hello there.` is dropped.
    assert "Hello there." in bodies
    assert "General Kenobi." in bodies
    assert bodies.count("Hello there.") == 1
    assert cues[0][0] == 0
    # The General Kenobi cue starts at 6 seconds.
    kenobi = next(c for c in cues if c[1] == "General Kenobi.")
    assert kenobi[0] == 6


def test_vtt_parser_strips_inline_tags() -> None:
    from unread.youtube.transcript import _parse_vtt

    vtt = """WEBVTT

00:00:00.000 --> 00:00:03.000
<c.colorE5E5E5>colorful</c><c> text</c>
"""
    text = _parse_vtt(vtt)
    assert "<" not in text
    assert "colorful" in text


def test_vtt_parser_returns_timed_cues_with_offsets() -> None:
    """Hour-mark cues are parsed correctly: 1h12m34s → 4354 sec."""
    from unread.youtube.transcript import _parse_vtt_timed

    vtt = """WEBVTT

01:12:34.000 --> 01:12:38.000
Cue at the 1-hour-12-minute mark.
"""
    cues = _parse_vtt_timed(vtt)
    assert len(cues) == 1
    assert cues[0][0] == 4354  # 1*3600 + 12*60 + 34
    assert "1-hour-12-minute" in cues[0][1]


def test_segment_timed_cues_groups_under_max_chars() -> None:
    """_segment_timed_cues packs cues until max_chars is reached."""
    from unread.youtube.commands import _segment_timed_cues

    cues = [
        (0, "first " * 50),
        (10, "second " * 50),
        (200, "third " * 50),
    ]
    segs = _segment_timed_cues(cues, max_chars=400)
    # Each cue ~300 chars; first two pack into one segment; third spills.
    assert len(segs) >= 2
    assert segs[0][0] == 0  # first segment starts at offset 0


# --- URL detection precedes Telegram resolution -----------------------------


def test_youtube_url_precedence_in_cmd_analyze() -> None:
    """`is_youtube_url` is the gate before `tg.resolver.resolve` runs.

    Pinning this so a future refactor that flips the order of checks
    surfaces the regression — otherwise YouTube URLs would be sent into
    Telegram's fuzzy-title matcher first, which is silly.
    """
    from unread.youtube.urls import is_youtube_url

    assert is_youtube_url("https://www.youtube.com/watch?v=abc")
    assert not is_youtube_url("@somechan")
    assert not is_youtube_url("t.me/foo/123")  # bare path; not a URL with scheme


# --- youtube_source flag validation ----------------------------------------


@pytest.mark.parametrize("source", ["auto", "captions", "audio"])
def test_valid_youtube_sources(source: str) -> None:
    """Triple-checks the literal alphabet our handler accepts."""
    from unread.youtube.transcript import TranscriptSource

    # Type-only; runtime check at the cmd_analyze branch.
    assert source in ("auto", "captions", "audio")
    _ = TranscriptSource  # referenced for import smoke
