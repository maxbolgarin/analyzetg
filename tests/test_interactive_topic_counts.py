"""Topic-scoped period counts in the wizard.

Forum topics share a chat-wide msg_id sequence, so the
`_fetch_period_counts` msg_id-difference approximation is wildly wrong
for a single topic — it returns the chat-wide span between the topic's
first and last message in the period, not the topic's own count.

These tests pin the iteration-based fix:

  - `_fetch_topic_period_counts` walks the topic's messages once,
    buckets each by date, and uses the authoritative `topic_unread`
    from `GetForumTopicsRequest` for the "unread" row.
  - Periods that extend past the oldest walked message return None
    (rendered as "—") instead of an under-count.
  - `_count_custom_range_topic` mirrors the same approach for the
    user-picked custom date range step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unread.interactive import (
    _TOPIC_COUNT_CAP,
    _classify_walk_message,
    _count_custom_range_topic,
    _estimate_comments_count,
    _estimate_enrich_cost,
    _fetch_topic_period_counts,
    _format_enrich_for_plan,
    _format_msg_count_with_comments,
    _format_period_for_plan,
)


class _FakeMsg:
    __slots__ = ("date", "id")

    def __init__(self, msg_id: int, date: datetime) -> None:
        self.id = msg_id
        self.date = date


class _FakeClient:
    """Minimal stand-in for Telethon's TelegramClient.

    `iter_messages` returns the configured list (or a slice up to
    `limit`), recording each call's kwargs for assertion.
    """

    def __init__(self, messages: list[_FakeMsg]) -> None:
        self._messages = messages
        self.calls: list[dict] = []

    def iter_messages(self, chat_id, **kwargs):
        self.calls.append({"chat_id": chat_id, **kwargs})

        msgs = list(self._messages)
        offset_date = kwargs.get("offset_date")
        if offset_date is not None:
            msgs = [m for m in msgs if m.date <= offset_date]
        limit = kwargs.get("limit")

        async def _gen():
            for count, m in enumerate(msgs):
                if limit is not None and count >= limit:
                    return
                yield m

        return _gen()


@pytest.mark.asyncio
async def test_topic_period_counts_buckets_by_date():
    """Each message lands in every bucket whose boundary it post-dates."""
    now = datetime.now(UTC)
    msgs = [
        _FakeMsg(1000, now - timedelta(hours=1)),  # last24h ✓
        _FakeMsg(999, now - timedelta(hours=10)),  # last24h ✓
        _FakeMsg(998, now - timedelta(hours=50)),  # last96h ✓ (not last24h)
        _FakeMsg(997, now - timedelta(days=3)),  # last96h ✓
        _FakeMsg(996, now - timedelta(days=10)),  # last30 ✓ (not last7)
        _FakeMsg(995, now - timedelta(days=60)),  # last90 ✓ (not last30)
    ]
    client = _FakeClient(msgs)

    out, _media = await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=4)

    # Two messages are within the last 24h.
    assert out["last24h"] == 2
    # last96h includes everything up to ~96h, so the first 4 messages.
    assert out["last96h"] == 4
    assert out["last7"] == 4  # same 4 within 7 days
    assert out["last30"] == 5  # +1 at -10d
    assert out["last90"] == 6  # +1 at -60d
    # Walked all 6 → not saturated → `full` is the exact count.
    assert out["full"] == 6
    # Authoritative unread comes from the topic, not from msg-id math.
    assert out["unread"] == 4


@pytest.mark.asyncio
async def test_topic_period_counts_unread_zero_normalises_to_none():
    """Zero unread renders as '—' in the picker (matches `_fmt_count`)."""
    client = _FakeClient([])
    out, _media = await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=0)
    assert out["unread"] is None
    # Empty topic: every period bucket is exactly 0 (we walked all of them).
    for key in ("last24h", "last96h", "last7", "last30", "last90", "year_start"):
        assert out[key] == 0
    assert out["full"] == 0


@pytest.mark.asyncio
async def test_topic_period_counts_saturation_marks_long_periods_none():
    """Hitting the cap means every period whose start is older than the
    oldest walked message is flagged None — there could be more messages
    between oldest-walked and the period start that we never fetched.

    Construction: fill the cap with messages spaced two minutes apart
    so the oldest sits at roughly cap*2 minutes ago, comfortably inside
    the last24h window. last96h / last7 / last30 / last90 / year_start
    all extend past oldest-walked, so they must be None.
    """
    now = datetime.now(UTC)
    msgs = [_FakeMsg(10_000 - i, now - timedelta(minutes=2 * i)) for i in range(_TOPIC_COUNT_CAP)]
    # Sanity: oldest must sit inside last24h for this assertion.
    oldest_age = now - msgs[-1].date
    assert oldest_age < timedelta(hours=24), "test setup: oldest message must be inside last24h"
    client = _FakeClient(msgs)

    out, _media = await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=99)

    # last24h boundary is older than oldest walked → we cannot know if more
    # messages exist between them, so this bucket is also None.
    assert out["last24h"] is None
    assert out["last96h"] is None
    assert out["last7"] is None
    assert out["last30"] is None
    assert out["last90"] is None
    assert out["year_start"] is None
    # `full` is also unknown when saturated.
    assert out["full"] is None
    assert out["unread"] == 99


@pytest.mark.asyncio
async def test_topic_period_counts_saturation_keeps_buckets_at_or_inside_oldest():
    """When saturated, buckets whose boundary is *newer than* the oldest
    walked message remain exact — we walked every message in those windows.

    Spread 500 messages across a few months so the oldest sits past the
    last30 boundary; last30 is exact (every message in the last 30 days
    appears in the cap-sized walk), but last90 / year_start are unknown.
    """
    now = datetime.now(UTC)
    # Message i sits i*30 minutes back. _TOPIC_COUNT_CAP*30min ≈ 250h ≈ 10.4 days,
    # so all messages fit inside last30 but exceed last24h / last96h / last7.
    msgs = [_FakeMsg(10_000 - i, now - timedelta(minutes=30 * i)) for i in range(_TOPIC_COUNT_CAP)]
    oldest = msgs[-1].date
    # Sanity: oldest sits past last7 but inside last30.
    assert now - oldest > timedelta(days=7)
    assert now - oldest < timedelta(days=30)
    client = _FakeClient(msgs)

    out, _media = await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=99)

    # Boundaries newer than (or equal to) oldest walked → exact counts.
    # Boundaries older than oldest walked → None.
    assert out["last24h"] is not None  # exact: last 24h fits in cap
    assert out["last96h"] is not None
    assert out["last7"] is not None
    # last30 boundary is older than oldest → unknown.
    assert out["last30"] is None
    assert out["last90"] is None
    # `full` is unknown when saturated.
    assert out["full"] is None
    assert out["unread"] == 99


@pytest.mark.asyncio
async def test_topic_period_counts_returns_walk_time_media_breakdown():
    """The same walk that fills period buckets also classifies media —
    confirms the wizard can render per-kind counts on the `enrich:` row
    without an extra DB round-trip."""
    from unittest.mock import MagicMock

    from telethon.tl.types import (
        DocumentAttributeAudio,
        DocumentAttributeVideo,
        MessageMediaDocument,
        MessageMediaPhoto,
    )

    now = datetime.now(UTC)

    def _doc(*, voice=False, video=False, round_message=False):
        attrs = []
        if voice:
            a = MagicMock(spec=DocumentAttributeAudio)
            a.voice = True
            attrs.append(a)
        if video:
            v = MagicMock(spec=DocumentAttributeVideo)
            v.round_message = round_message
            attrs.append(v)
        doc = MagicMock()
        doc.attributes = attrs
        m = MagicMock(spec=MessageMediaDocument)
        m.document = doc
        return m

    msgs: list = []
    # Two voice msgs.
    for i in range(2):
        msg = MagicMock()
        msg.id = 1000 + i
        msg.date = now - timedelta(hours=i + 1)
        msg.message = ""
        msg.text = ""
        msg.media = _doc(voice=True)
        msg.entities = []
        msgs.append(msg)
    # One photo with text.
    msg = MagicMock()
    msg.id = 1100
    msg.date = now - timedelta(hours=5)
    msg.message = "look"
    msg.text = "look"
    msg.media = MagicMock(spec=MessageMediaPhoto)
    msg.entities = []
    msgs.append(msg)
    # One plain text with a URL fallback.
    msg = MagicMock()
    msg.id = 1101
    msg.date = now - timedelta(hours=6)
    msg.message = "see http://example.com"
    msg.text = "see http://example.com"
    msg.media = None
    msg.entities = []
    msgs.append(msg)

    client = _FakeClient(msgs)
    _, media = await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=4)

    assert media["voice"] == 2
    assert media["photo"] == 1
    assert media["links"] == 1  # via http fallback
    assert media["text"] == 2  # photo caption + plain-text msg
    assert media["any_media"] == 3  # 2 voice + 1 photo (link-only msg has no media)
    assert media["total"] == 4  # walked all four


@pytest.mark.asyncio
async def test_topic_period_counts_passes_reply_to_thread():
    """Sanity: the iter call must be scoped to the topic, not the chat."""
    client = _FakeClient([])
    await _fetch_topic_period_counts(client, chat_id=-1001, thread_id=42, topic_unread=0)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["reply_to"] == 42
    assert call["limit"] == _TOPIC_COUNT_CAP


@pytest.mark.asyncio
async def test_topic_period_counts_returns_none_on_iter_error():
    """RPC failures return all-None period counts but keep `topic_unread`."""

    class _BrokenClient:
        def iter_messages(self, *a, **kw):
            async def _gen():
                raise RuntimeError("boom")
                yield  # pragma: no cover

            return _gen()

    out, media = await _fetch_topic_period_counts(
        _BrokenClient(), chat_id=-1001, thread_id=42, topic_unread=7
    )
    for key in ("last24h", "last96h", "last7", "last30", "last90", "year_start", "full"):
        assert out[key] is None
    assert out["unread"] == 7
    # Media tally is best-effort: zero counts when the iteration never started.
    assert media["total"] == 0
    for key in ("voice", "videonote", "video", "photo", "doc", "links", "text"):
        assert media[key] == 0


@pytest.mark.asyncio
async def test_count_custom_range_topic_exact_when_walks_past_since():
    """If the walk reaches a message older than `since`, the count is
    exact (we've seen every message in the range)."""
    now = datetime.now(UTC)
    msgs = [
        _FakeMsg(1000, now - timedelta(days=1)),
        _FakeMsg(999, now - timedelta(days=2)),
        _FakeMsg(998, now - timedelta(days=4)),
        _FakeMsg(997, now - timedelta(days=10)),  # outside [since, until]
    ]
    client = _FakeClient(msgs)
    since = now - timedelta(days=5)
    until = now

    n = await _count_custom_range_topic(client, chat_id=-1001, thread_id=42, since=since, until=until)
    assert n == 3


@pytest.mark.asyncio
async def test_count_custom_range_topic_saturation_returns_none():
    """Cap hit before `since` is reached → unknown lower bound."""
    now = datetime.now(UTC)
    # All within range, count exceeds the cap.
    msgs = [_FakeMsg(10_000 - i, now - timedelta(minutes=i)) for i in range(_TOPIC_COUNT_CAP)]
    client = _FakeClient(msgs)
    since = now - timedelta(days=30)
    until = now

    n = await _count_custom_range_topic(client, chat_id=-1001, thread_id=42, since=since, until=until)
    assert n is None


@pytest.mark.asyncio
async def test_count_custom_range_topic_passes_until_as_offset_date():
    """Telethon's offset_date is the upper bound; verify we set it."""
    client = _FakeClient([])
    until = datetime(2026, 1, 1, tzinfo=UTC)
    await _count_custom_range_topic(client, chat_id=-1001, thread_id=42, since=None, until=until)
    assert len(client.calls) == 1
    assert client.calls[0]["offset_date"] is until
    assert client.calls[0]["reply_to"] == 42


# ---------- Walk-time media classification ----------------------------


def _make_telethon_msg(*, text: str = "", media=None, entities: list | None = None):
    """Build a duck-typed Telethon message stand-in.

    The classifier relies on `isinstance(media, …)` checks, so callers
    pass real Telethon class instances (or `MagicMock(spec=…)` stand-ins
    that pass isinstance) for `media` and entries in `entities`.
    """
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.message = text
    msg.text = text
    msg.media = media
    msg.entities = entities or []
    return msg


def test_classify_text_only():
    """Plain text → only the `text` flag, nothing else."""
    flags = _classify_walk_message(_make_telethon_msg(text="hello world"))
    assert flags["text"] is True
    for key in ("voice", "videonote", "video", "photo", "doc", "links"):
        assert flags[key] is False


def test_classify_photo():
    """`MessageMediaPhoto` → `photo` flag."""
    from unittest.mock import MagicMock

    from telethon.tl.types import MessageMediaPhoto

    flags = _classify_walk_message(
        _make_telethon_msg(text="caption", media=MagicMock(spec=MessageMediaPhoto))
    )
    assert flags["photo"] is True
    assert flags["text"] is True
    assert flags["voice"] is False
    assert flags["video"] is False


def test_classify_voice_via_document_attribute_audio():
    """`DocumentAttributeAudio(voice=True)` → `voice` (over `doc`)."""
    from unittest.mock import MagicMock

    from telethon.tl.types import DocumentAttributeAudio, MessageMediaDocument

    audio_attr = MagicMock(spec=DocumentAttributeAudio)
    audio_attr.voice = True
    doc_obj = MagicMock()
    doc_obj.attributes = [audio_attr]
    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc_obj

    flags = _classify_walk_message(_make_telethon_msg(media=media))
    assert flags["voice"] is True
    assert flags["doc"] is False
    assert flags["video"] is False


def test_classify_videonote_wins_over_video():
    """`DocumentAttributeVideo(round_message=True)` → `videonote`."""
    from unittest.mock import MagicMock

    from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

    video_attr = MagicMock(spec=DocumentAttributeVideo)
    video_attr.round_message = True
    doc_obj = MagicMock()
    doc_obj.attributes = [video_attr]
    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc_obj

    flags = _classify_walk_message(_make_telethon_msg(media=media))
    assert flags["videonote"] is True
    assert flags["video"] is False


def test_classify_plain_video():
    """`DocumentAttributeVideo(round_message=False)` → `video`."""
    from unittest.mock import MagicMock

    from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

    video_attr = MagicMock(spec=DocumentAttributeVideo)
    video_attr.round_message = False
    doc_obj = MagicMock()
    doc_obj.attributes = [video_attr]
    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc_obj

    flags = _classify_walk_message(_make_telethon_msg(media=media))
    assert flags["video"] is True
    assert flags["videonote"] is False
    assert flags["doc"] is False


def test_classify_doc_no_audio_no_video_attributes():
    """A document with no audio/video attributes → `doc`."""
    from unittest.mock import MagicMock

    from telethon.tl.types import MessageMediaDocument

    doc_obj = MagicMock()
    doc_obj.attributes = []  # plain file
    media = MagicMock(spec=MessageMediaDocument)
    media.document = doc_obj

    flags = _classify_walk_message(_make_telethon_msg(text="report.pdf", media=media))
    assert flags["doc"] is True
    assert flags["voice"] is False
    assert flags["video"] is False


def test_classify_webpage_media_is_link():
    """`MessageMediaWebPage` → `links` flag."""
    from unittest.mock import MagicMock

    from telethon.tl.types import MessageMediaWebPage

    media = MagicMock(spec=MessageMediaWebPage)
    flags = _classify_walk_message(_make_telethon_msg(text="check this", media=media))
    assert flags["links"] is True


def test_classify_links_via_url_entity():
    """A `MessageEntityUrl` in `entities` triggers the `links` flag."""
    from unittest.mock import MagicMock

    from telethon.tl.types import MessageEntityUrl

    flags = _classify_walk_message(
        _make_telethon_msg(text="see foo", entities=[MagicMock(spec=MessageEntityUrl)])
    )
    assert flags["links"] is True


def test_classify_links_via_text_http_fallback():
    """No entity, no media — `'http' in text` still flags `links` (matches `media_breakdown`)."""
    flags = _classify_walk_message(_make_telethon_msg(text="visit http://example.com"))
    assert flags["links"] is True
    assert flags["text"] is True


# ---------- Plan-row formatters ---------------------------------------


def test_format_enrich_for_plan_with_counts():
    """Each enrich kind shows as `name N` from `media_counts`. `image` and
    `link` are mapped to the DB-side keys (`photo`, `links`)."""
    media_counts = {
        "voice": 8,
        "videonote": 0,
        "video": 3,
        "photo": 12,
        "doc": 1,
        "links": 30,
        "text": 200,
    }
    out = _format_enrich_for_plan(["voice", "videonote", "link", "video", "image", "doc"], media_counts)
    # Order matches the input list — users see what they enabled.
    assert out == "voice 8 · videonote 0 · link 30 · video 3 · image 12 · doc 1"


def test_format_enrich_for_plan_missing_counts_show_bare_name():
    """When `media_counts` doesn't have a kind, fall back to the bare name."""
    out = _format_enrich_for_plan(["voice", "video"], media_counts={})
    assert out == "voice · video"


def test_format_enrich_for_plan_empty_kinds_returns_none_value():
    """Empty list → localized 'none' string for the multiline rendering."""
    out = _format_enrich_for_plan([], media_counts={"voice": 1})
    # English fallback is `none`; we don't test the literal because the
    # active locale is set elsewhere — just ensure it isn't a kind list.
    assert "·" not in out
    assert out  # non-empty


def test_format_period_for_plan_canonical():
    """Canonical periods round-trip the bare code."""
    assert _format_period_for_plan("full", None, None, None, None) == "full"
    assert _format_period_for_plan("last7", None, None, None, None) == "last7"
    assert _format_period_for_plan("unread", None, None, None, None) == "unread"


def test_format_period_for_plan_custom_with_count():
    """Custom range includes the bracketed dates and an `≈N` count when known."""
    out = _format_period_for_plan("custom", "2026-01-01", "2026-02-01", None, {"custom": 42})
    assert "custom" in out
    assert "2026-01-01..2026-02-01" in out
    assert "42" in out


def test_format_period_for_plan_custom_no_count():
    """Custom range without a known count just shows the dates."""
    out = _format_period_for_plan("custom", "2026-01-01", "2026-02-01", None, {})
    assert out == "custom (2026-01-01..2026-02-01)"


def test_format_period_for_plan_from_msg():
    """`from_msg` shows the message-id ref."""
    out = _format_period_for_plan("from_msg", None, None, "12345", None)
    assert "from_msg" in out
    assert "12345" in out


def test_format_period_for_plan_none_returns_dash():
    """A missing period code renders as the dash placeholder."""
    assert _format_period_for_plan(None, None, None, None, None) == "—"


# ---------- Enrichment cost estimate ----------------------------------


def test_estimate_enrich_cost_sums_per_kind_rates():
    """Total = Σ(count × per-unit rate). `image` and `link` map to
    `photo` / `links` count keys."""
    media = {"voice": 3, "videonote": 0, "video": 11, "photo": 27, "doc": 2, "links": 4}
    cost = _estimate_enrich_cost(["voice", "videonote", "link", "video", "image", "doc"], media)
    # Per-unit rates (see `_ENRICH_PER_UNIT_USD`):
    #   voice/videonote: 0.5min * $0.006/min = $0.003
    #   video:           1.0min * $0.006/min = $0.006
    #   image:           $0.0002
    #   link:            $0.0001
    #   doc:             $0
    expected = 3 * 0.003 + 0 * 0.003 + 11 * 0.006 + 27 * 0.0002 + 4 * 0.0001 + 2 * 0.0
    assert cost == pytest.approx(expected, rel=1e-9)


def test_estimate_enrich_cost_returns_none_when_no_kinds_or_no_counts():
    """No enrich kinds or no media counts → None (renderer skips the line)."""
    assert _estimate_enrich_cost(None, {"voice": 5}) is None
    assert _estimate_enrich_cost([], {"voice": 5}) is None
    assert _estimate_enrich_cost(["voice"], None) is None
    assert _estimate_enrich_cost(["voice"], {}) is None


def test_estimate_enrich_cost_returns_zero_when_kinds_present_but_zero_counts():
    """Enrichment enabled but every enabled kind is zero → $0 (still
    a meaningful value to display, distinct from None)."""
    cost = _estimate_enrich_cost(
        ["voice", "video"], {"voice": 0, "videonote": 0, "video": 0, "photo": 0, "doc": 0, "links": 0}
    )
    assert cost == 0.0


def test_estimate_enrich_cost_doc_is_free():
    """Doc extraction uses local parsers, no LLM cost."""
    cost = _estimate_enrich_cost(["doc"], {"doc": 100})
    assert cost == 0.0


def test_estimate_enrich_cost_unknown_kind_contributes_zero():
    """Forward-compat: an unknown enrich kind doesn't crash and adds nothing."""
    cost = _estimate_enrich_cost(["voice", "futurekind"], {"voice": 1, "futurekind": 5})
    assert cost == pytest.approx(1 * 0.003, rel=1e-9)


# ---------- Channel + comments msg count rendering --------------------


def test_format_msg_count_no_comments_renders_int():
    """Plain int when no comments are folded in."""
    assert _format_msg_count_with_comments(537, None) == "537"
    assert _format_msg_count_with_comments(537, 0) == "537"
    # Negative / zero comments treated as "no comments to add".
    assert _format_msg_count_with_comments(537, -3) == "537"


def test_format_msg_count_with_comments_renders_breakdown():
    """`N + ~M comments` shape so the user sees both contributions."""
    out = _format_msg_count_with_comments(537, 5234)
    assert "537" in out
    assert "5234" in out
    assert "comments" in out
    assert "~" in out  # signals approximation


def test_format_msg_count_none_channel_returns_dash():
    """Missing channel count → dash placeholder."""
    assert _format_msg_count_with_comments(None, None) == "—"
    assert _format_msg_count_with_comments(None, 100) == "—"


# ---------- Linked-chat comments count estimation ---------------------


class _CommentsClient:
    """Minimal Telethon stand-in for `_estimate_comments_count` tests.

    Captures every `get_messages` and `get_input_entity` call so tests
    can assert on the lookup pattern. `entity_resolve_raises` lets a
    test simulate the Telethon-cache miss that the entity-resolve
    fallback was added to handle.
    """

    def __init__(self, channel_oldest_unread=None, entity_resolve_raises=False):
        self.calls: list[tuple[int, dict]] = []
        self.entity_calls: list[int] = []
        self._channel_oldest_unread = channel_oldest_unread
        self._entity_resolve_raises = entity_resolve_raises

    async def get_messages(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))
        if "min_id" in kwargs and self._channel_oldest_unread is not None:
            return [self._channel_oldest_unread]
        return [type("M", (), {"id": 1000})()]

    async def get_input_entity(self, chat_id):
        self.entity_calls.append(chat_id)
        if self._entity_resolve_raises:
            raise ValueError(f"Could not find the input entity for {chat_id}")
        return object()


@pytest.mark.asyncio
async def test_estimate_comments_count_uses_period_since_for_date_period():
    """For a date-bounded period (e.g. last7), the linked chat lookup
    must use a `since` cutoff matching that period, not a chat-wide span."""
    client = _CommentsClient()
    n = await _estimate_comments_count(
        client,
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 0},
        linked_chat_id=-1639,
        period="last7",
        custom_since=None,
        custom_until=None,
    )
    captured = [kw for _, kw in client.calls]
    # `_count_custom_range` makes 2 calls (upper + lower); the lower
    # bound must carry an `offset_date` ~ 7 days back to scope the
    # linked-chat estimate to the same window.
    assert len(captured) == 2
    has_since = any(call.get("offset_date") is not None and call.get("reverse") is True for call in captured)
    assert has_since, "lower-bound call must carry offset_date for the period start"
    # Both calls returned id=1000, so count = 1 (max - min + 1 = 1).
    assert n == 1


@pytest.mark.asyncio
async def test_estimate_comments_count_unread_uses_oldest_unread_msg_date():
    """`unread` period: derive `since` from the date of the oldest unread
    channel message (one extra `get_messages(min_id=read_max, reverse=True)`
    lookup) so the comments estimate covers the same span."""
    from datetime import datetime as _dt

    oldest_unread_date = _dt(2026, 5, 1, tzinfo=UTC)
    oldest_unread_msg = type("M", (), {"id": 88500, "date": oldest_unread_date})()
    client = _CommentsClient(channel_oldest_unread=oldest_unread_msg)
    n = await _estimate_comments_count(
        client,
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 88265},
        linked_chat_id=-1639,
        period="unread",
        custom_since=None,
        custom_until=None,
    )
    captured = client.calls
    # 1 channel lookup (oldest unread) + 2 linked-chat lookups (count_custom_range).
    assert len(captured) == 3
    # First call: channel, with min_id=read_max.
    assert captured[0][0] == -1001
    assert captured[0][1].get("min_id") == 88265
    assert captured[0][1].get("reverse") is True
    # Lower-bound on linked chat: must carry the derived oldest_unread_date.
    linked_calls = [(cid, kw) for cid, kw in captured if cid == -1639]
    assert any(kw.get("offset_date") == oldest_unread_date for _, kw in linked_calls), (
        "linked-chat lower-bound call must carry the oldest-unread date"
    )
    assert n == 1


@pytest.mark.asyncio
async def test_estimate_comments_count_returns_none_for_from_msg_period():
    """`from_msg` is the uncommon path — we skip estimation rather than
    resolve the ref to a date. Plan still renders without it."""

    class _C:
        async def get_messages(self, *a, **kw):  # pragma: no cover — should not be called
            raise AssertionError("get_messages should not be called for from_msg period")

        async def get_input_entity(self, _cid):  # pragma: no cover
            raise AssertionError("get_input_entity should not be called for from_msg period")

    n = await _estimate_comments_count(
        _C(),
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 88265},
        linked_chat_id=-1639,
        period="from_msg",
        custom_since=None,
        custom_until=None,
    )
    assert n is None


@pytest.mark.asyncio
async def test_estimate_comments_count_unread_with_no_read_marker_returns_none():
    """No read marker (e.g. fresh channel subscription) → no derivable
    `since` → return None instead of estimating against a meaningless window."""

    class _C:
        async def get_messages(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must not call when read marker is 0")

        async def get_input_entity(self, _cid):  # pragma: no cover
            raise AssertionError("get_input_entity should not be called when no read marker")

    n = await _estimate_comments_count(
        _C(),
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 0},
        linked_chat_id=-1639,
        period="unread",
        custom_since=None,
        custom_until=None,
    )
    assert n is None


@pytest.mark.asyncio
async def test_estimate_comments_count_resolves_linked_entity_before_get_messages():
    """Telethon's `get_messages(int_chat_id)` only works if the entity
    is in its session cache. The linked discussion of a channel may
    not be there (the user has never opened it directly), so we must
    `get_input_entity` first to populate the cache. This test pins
    that the resolve happens — and that a resolve failure aborts the
    estimate cleanly with `None` instead of letting `get_messages`
    raise and swallow into a misleading-looking failure."""
    # Happy path: entity resolves, count returns.
    client_ok = _CommentsClient()
    n = await _estimate_comments_count(
        client_ok,
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 0},
        linked_chat_id=-1639,
        period="last7",
        custom_since=None,
        custom_until=None,
    )
    assert client_ok.entity_calls == [-1639], (
        "must resolve the linked-chat input entity before calling get_messages on it"
    )
    assert n is not None

    # Resolution failure: bail with None, never call get_messages on the
    # linked chat. (The channel-lookup-by-min_id branch isn't reached
    # for a date period, so no get_messages calls at all here.)
    client_fail = _CommentsClient(entity_resolve_raises=True)
    n = await _estimate_comments_count(
        client_fail,
        channel_chat={"chat_id": -1001, "read_inbox_max_id": 0},
        linked_chat_id=-1639,
        period="last7",
        custom_since=None,
        custom_until=None,
    )
    assert client_fail.entity_calls == [-1639]
    assert client_fail.calls == [], "must not get_messages after entity resolve failure"
    assert n is None
