"""Tests for `unread.bot.confirm` — pre-analyze inline-keyboard confirm panel.

Pure-logic surface: callback encoding/decoding, option toggling, panel
text/button construction, TTL pruning. No Telethon network calls — the
panel builders return `(text, buttons)` tuples where `buttons` is a
list of `telethon.Button.inline(...)` rows the handler later passes to
`event.reply(..., buttons=buttons)`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from unread.bot.burst import BurstItem, combinable_items, merge_album_items, summary_line
from unread.bot.burst import _extract_tg_msg_id as extract_tg_msg_id
from unread.bot.confirm import (
    PendingRun,
    RunOptions,
    build_batch_panel,
    build_forward_choice_panel,
    build_initial_panel,
    build_tg_choice_panel,
    default_options,
    default_preset_for_kind,
    encode_callback,
    parse_callback,
    prune_pending_runs,
    tg_window_for_action,
)
from unread.config import load_settings, reset_settings

# ---------------------------------------------------------------------------
# RunOptions / defaults
# ---------------------------------------------------------------------------


def _fresh_settings():
    reset_settings()
    return load_settings()


def test_default_options_for_youtube_uses_auto_source():
    opts = default_options("youtube", _fresh_settings())
    assert opts.youtube_source == "auto"


def test_default_options_for_tg_mirrors_enrich_config_defaults():
    """enrich.image / doc / link / video all default to False in EnrichCfg."""
    opts = default_options("tg", _fresh_settings())
    assert opts.enrich_image is False
    assert opts.enrich_doc is False
    assert opts.enrich_link is False
    assert opts.enrich_video is False


def test_default_options_for_file_has_no_youtube_or_enrich_knobs():
    """File/Web kinds have no Change menu — options are empty placeholders."""
    opts = default_options("file", _fresh_settings())
    assert opts.youtube_source is None
    assert opts.enrich_image is False


def test_default_options_for_url_has_no_youtube_or_enrich_knobs():
    opts = default_options("url", _fresh_settings())
    assert opts.youtube_source is None


# ---------------------------------------------------------------------------
# Callback encoding
# ---------------------------------------------------------------------------


def test_encode_callback_run_is_short_and_parseable():
    data = encode_callback("R", 12345)
    assert isinstance(data, bytes)
    assert len(data) <= 64
    assert parse_callback(data) == ("R", 12345, None)


def test_parse_callback_rejects_unknown_action():
    with pytest.raises(ValueError):
        parse_callback(b"Z:1")


def test_parse_callback_rejects_malformed_message_id():
    with pytest.raises(ValueError):
        parse_callback(b"R:notanint")


def test_parse_callback_rejects_empty():
    with pytest.raises(ValueError):
        parse_callback(b"")


# ---------------------------------------------------------------------------
# Initial panel construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected_default",
    [
        ("file", "summary"),
        ("url", "website"),
        ("youtube", "video"),
        ("tg", "summary"),
    ],
)
def test_default_preset_for_kind_matches_cmd_analyze_fallback(kind, expected_default):
    """`_initial_text` falls back to this when the chat has no sticky `/preset`."""
    assert default_preset_for_kind(kind) == expected_default


def test_build_initial_panel_for_file_has_only_run_button():
    """Single [▶ Run] button. No Change, no Cancel — slash commands tune."""
    opts = default_options("file", _fresh_settings())
    text, buttons = build_initial_panel(
        kind="file",
        payload={"name": "document.pdf", "kind": "pdf"},
        options=opts,
        preset="summary",
        panel_msg_id=100,
    )
    assert "document.pdf" in text
    assert "summary" in text
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("Run" in label for label in labels)
    assert not any("Change" in label for label in labels)
    assert not any("Cancel" in label for label in labels)
    # Exactly one button — no nested menu lurking.
    assert len(flat) == 1


def test_build_initial_panel_for_url_has_only_run_button():
    opts = default_options("url", _fresh_settings())
    text, buttons = build_initial_panel(
        kind="url",
        payload={"url": "https://example.com/article"},
        options=opts,
        preset="website",
        panel_msg_id=200,
    )
    assert "example.com" in text
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("Run" in label for label in labels)
    assert not any("Change" in label for label in labels)
    assert len(flat) == 1


def test_build_initial_panel_for_youtube_has_only_run_button():
    opts = default_options("youtube", _fresh_settings())
    _text, buttons = build_initial_panel(
        kind="youtube",
        payload={"url": "https://youtu.be/abc"},
        options=opts,
        preset="video",
        panel_msg_id=300,
    )
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("Run" in label for label in labels)
    assert not any("Change" in label for label in labels)
    assert len(flat) == 1


def test_build_initial_panel_for_tg_has_only_run_button():
    opts = default_options("tg", _fresh_settings())
    _text, buttons = build_initial_panel(
        kind="tg",
        payload={"url": "https://t.me/somechan/123"},
        options=opts,
        preset="summary",
        panel_msg_id=400,
    )
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("Run" in label for label in labels)
    assert not any("Change" in label for label in labels)
    assert len(flat) == 1


def test_build_initial_panel_run_button_encodes_panel_msg_id():
    opts = default_options("file", _fresh_settings())
    _text, buttons = build_initial_panel(
        kind="file",
        payload={"name": "x.txt", "kind": "text"},
        options=opts,
        preset="summary",
        panel_msg_id=777,
    )
    run_btn = next(b for row in buttons for b in row if "Run" in b.text)
    assert parse_callback(run_btn.data) == ("R", 777, None)


# ---------------------------------------------------------------------------
# Panel text — shows real values, never "_(default)_" placeholder
# ---------------------------------------------------------------------------


def test_initial_text_shows_concrete_preset_when_caller_passes_empty():
    """Empty `preset` → falls back to the kind-specific default name, not _(default)_."""
    opts = default_options("tg", _fresh_settings())
    text, _buttons = build_initial_panel(
        kind="tg",
        payload={"url": "https://t.me/x/1"},
        options=opts,
        preset="",
        panel_msg_id=1,
    )
    assert "summary" in text  # default for tg
    assert "_(default)_" not in text


def test_initial_text_for_tg_lists_real_enrich_baseline_not_none_placeholder():
    """Default options → "voice, videonote" (the always-on baseline), not "(none)"."""
    opts = default_options("tg", _fresh_settings())
    text, _buttons = build_initial_panel(
        kind="tg",
        payload={"url": "https://t.me/x/1"},
        options=opts,
        preset="summary",
        panel_msg_id=1,
    )
    assert "voice" in text
    assert "videonote" in text
    assert "_(none beyond defaults)_" not in text


def test_initial_text_for_tg_includes_extra_toggled_enrichers():
    opts = default_options("tg", _fresh_settings())
    opts.enrich_image = True
    opts.enrich_link = True
    text, _buttons = build_initial_panel(
        kind="tg",
        payload={"url": "https://t.me/x/1"},
        options=opts,
        preset="summary",
        panel_msg_id=1,
    )
    assert "image" in text
    assert "link" in text


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------


def test_prune_pending_runs_drops_old_entries():
    now = 10_000.0
    chat_state: dict = {
        "pending_runs": {
            100: PendingRun(kind="file", payload={}, options=RunOptions(), created_at=now - 4000.0),
            200: PendingRun(kind="url", payload={}, options=RunOptions(), created_at=now - 500.0),
        }
    }
    prune_pending_runs(chat_state, ttl_seconds=3600, now=now)
    assert 100 not in chat_state["pending_runs"]
    assert 200 in chat_state["pending_runs"]


def test_prune_pending_runs_handles_missing_key():
    """No-op when pending_runs hasn't been seeded yet."""
    chat_state: dict = {}
    prune_pending_runs(chat_state, ttl_seconds=3600, now=time.time())
    # No crash, no key added.
    assert chat_state == {}


def test_prune_pending_runs_keeps_everything_when_all_fresh():
    now = 500.0
    chat_state: dict = {
        "pending_runs": {
            1: PendingRun(kind="file", payload={}, options=RunOptions(), created_at=now - 10.0),
            2: PendingRun(kind="tg", payload={}, options=RunOptions(), created_at=now - 200.0),
        }
    }
    prune_pending_runs(chat_state, ttl_seconds=3600, now=now)
    assert set(chat_state["pending_runs"].keys()) == {1, 2}


# ---------------------------------------------------------------------------
# Burst — pure helpers (summary_line, combinable_items)
# ---------------------------------------------------------------------------


def _item(item_kind: str, **payload: Any) -> BurstItem:
    """`item_kind` (not `kind`) so payloads with a 'kind' field don't collide."""
    return BurstItem(kind=item_kind, payload=payload, event=None)


def test_summary_line_for_each_kind():
    assert "example.com" in summary_line(_item("url", url="https://example.com/a"))
    assert "youtu.be" in summary_line(_item("youtube", url="https://youtu.be/abc"))
    assert "doc.pdf" in summary_line(_item("file", source="media", name="doc.pdf", kind="pdf"))
    assert "text message" in summary_line(_item("file", source="text", text="hi"))
    assert "t.me" in summary_line(_item("tg", url="https://t.me/x/1"))


def test_summary_line_for_album_shows_item_count():
    item = _item("file", source="media", kind="image", name="photo.jpg", album_size=3)
    line = summary_line(item)
    assert "album" in line
    assert "3" in line


def test_combinable_items_excludes_tg():
    items = [
        _item("url", url="https://a.com"),
        _item("tg", url="https://t.me/x/1"),
        _item("youtube", url="https://youtu.be/abc"),
    ]
    out = combinable_items(items)
    kinds = [it.kind for it in out]
    assert "tg" not in kinds
    assert set(kinds) == {"url", "youtube"}


# ---------------------------------------------------------------------------
# Batch panel — N items collapsed into one panel with A/M actions
# ---------------------------------------------------------------------------


def test_build_batch_panel_single_item_collapses_to_run_only():
    """A single-item 'burst' is just one source → one [▶ Run] button."""
    items = [_item("url", url="https://example.com/a")]
    _text, buttons = build_batch_panel(items=items, panel_msg_id=10)
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert len(flat) == 1
    assert "Run" in labels[0]
    assert "separately" not in labels[0].lower()
    assert "combined" not in labels[0].lower()
    assert parse_callback(flat[0].data) == ("R", 10, None)


def test_build_batch_panel_two_items_shows_both_modes():
    items = [
        _item("url", url="https://a.com/1"),
        _item("url", url="https://a.com/2"),
    ]
    text, buttons = build_batch_panel(items=items, panel_msg_id=20)
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("separately" in lbl.lower() for lbl in labels)
    assert any("combined" in lbl.lower() for lbl in labels)
    # Bullet list should mention both URLs.
    assert "a.com/1" in text
    assert "a.com/2" in text


def test_build_batch_panel_actions_encode_correctly():
    items = [
        _item("url", url="https://a.com/1"),
        _item("youtube", url="https://youtu.be/abc"),
    ]
    _text, buttons = build_batch_panel(items=items, panel_msg_id=33)
    flat = [b for row in buttons for b in row]
    sep = next(b for b in flat if "separately" in b.text.lower())
    merged = next(b for b in flat if "combined" in b.text.lower())
    assert parse_callback(sep.data) == ("A", 33, None)
    assert parse_callback(merged.data) == ("M", 33, None)


def test_build_batch_panel_hides_combined_when_only_tg():
    """A burst of TG-only links can't be merged today → no Combined button."""
    items = [
        _item("tg", url="https://t.me/x/1"),
        _item("tg", url="https://t.me/y/2"),
    ]
    _text, buttons = build_batch_panel(items=items, panel_msg_id=40)
    flat = [b for row in buttons for b in row]
    labels = [b.text for b in flat]
    assert any("separately" in lbl.lower() for lbl in labels)
    assert not any("combined" in lbl.lower() for lbl in labels)


def test_build_batch_panel_combined_label_notes_skip_count():
    """Mixed burst: combined button says 'X of Y' so the user knows TG was skipped."""
    items = [
        _item("url", url="https://a.com/1"),
        _item("tg", url="https://t.me/x/1"),
        _item("youtube", url="https://youtu.be/abc"),
    ]
    _text, buttons = build_batch_panel(items=items, panel_msg_id=50)
    flat = [b for row in buttons for b in row]
    merged = next(b for b in flat if "combined" in b.text.lower())
    # 2 combinable out of 3 → label should reflect that.
    assert "2" in merged.text and "3" in merged.text


def test_build_batch_panel_empty_is_defensive():
    text, buttons = build_batch_panel(items=[], panel_msg_id=1)
    assert "no messages" in text.lower()
    assert buttons == []


def test_parse_callback_accepts_new_actions():
    assert parse_callback(b"A:7")[0] == "A"
    assert parse_callback(b"M:8")[0] == "M"


# ---------------------------------------------------------------------------
# TG-link choice panel
# ---------------------------------------------------------------------------


def test_extract_tg_msg_id_private_form():
    assert extract_tg_msg_id("https://t.me/c/3853386994/81") == "81"


def test_extract_tg_msg_id_public_form():
    assert extract_tg_msg_id("https://t.me/somechan/42") == "42"


def test_extract_tg_msg_id_no_msg():
    assert extract_tg_msg_id("https://t.me/somechan") is None


@pytest.mark.parametrize(
    "action,window",
    [
        ("T_ONE", "msg"),
        ("T_FRM", "from_msg"),
        ("T_DAY", "1d"),
        ("T_WK", "7d"),
        ("T_MO", "30d"),
    ],
)
def test_tg_window_for_action_maps_each_button(action, window):
    assert tg_window_for_action(action) == window


def test_tg_window_for_action_returns_none_for_non_tg_actions():
    assert tg_window_for_action("R") is None
    assert tg_window_for_action("A") is None
    assert tg_window_for_action("M") is None


def test_parse_callback_accepts_tg_actions():
    for action in ("T_ONE", "T_FRM", "T_DAY", "T_WK", "T_MO"):
        data = encode_callback(action, 99)
        assert parse_callback(data) == (action, 99, None)


def test_build_tg_choice_panel_with_msg_id_shows_all_five_options():
    text, buttons = build_tg_choice_panel(
        url="https://t.me/c/3853386994/81",
        msg_id="81",
        panel_msg_id=10,
    )
    assert "t.me/c/3853386994/81" in text
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "just this msg" in labels
    assert "from this msg" in labels
    assert "last day" in labels
    assert "last week" in labels
    assert "last month" in labels
    # 2 msg-anchored buttons + 3 time-window buttons.
    assert len(flat) == 5


def test_build_tg_choice_panel_without_msg_id_hides_msg_buttons():
    """Bare @chan / t.me/chan (no msg) → only time-window buttons."""
    _text, buttons = build_tg_choice_panel(
        url="https://t.me/somechan",
        msg_id=None,
        panel_msg_id=20,
    )
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "just this msg" not in labels
    assert "from this msg" not in labels
    assert "last day" in labels
    assert len(flat) == 3


def test_build_tg_choice_panel_button_callbacks_round_trip():
    _text, buttons = build_tg_choice_panel(
        url="https://t.me/c/123/45",
        msg_id="45",
        panel_msg_id=50,
    )
    flat = [b for row in buttons for b in row]
    # Find one of each by label and verify the action encoding.
    one_btn = next(b for b in flat if "just this" in b.text.lower())
    week_btn = next(b for b in flat if "last week" in b.text.lower())
    assert parse_callback(one_btn.data) == ("T_ONE", 50, None)
    assert parse_callback(week_btn.data) == ("T_WK", 50, None)


def test_run_options_default_tg_window_is_none():
    """tg_window stays None unless the choice panel stamps it."""
    assert RunOptions().tg_window is None


# ---------------------------------------------------------------------------
# Forward picker — shown when a single forwarded-from-channel msg arrives
# ---------------------------------------------------------------------------


def test_parse_callback_accepts_forward_actions():
    for action in ("F_FULL", "F_TXT", "F_DAY", "F_WK", "F_MO"):
        data = encode_callback(action, 11)
        assert parse_callback(data) == (action, 11, None)


def test_build_forward_choice_panel_media_with_caption_shows_two_msg_buttons():
    payload = {
        "source": "media",
        "kind": "image",
        "caption": "the post text",
        "fwd_channel_id": 12345,
        "fwd_title": "BullTrading",
    }
    text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=1)
    assert "BullTrading" in text
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "image + caption" in labels
    assert "caption only" in labels
    # All 3 channel-window buttons present.
    assert "channel · day" in labels
    assert "channel · week" in labels
    assert "channel · month" in labels


def test_build_forward_choice_panel_media_without_caption_hides_caption_only():
    payload = {
        "source": "media",
        "kind": "image",
        "fwd_channel_id": 12345,
    }
    _text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=2)
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "caption only" not in labels
    assert "this media" in labels


def test_build_forward_choice_panel_text_only_forward():
    payload = {
        "source": "text",
        "text": "some text",
        "fwd_channel_id": 12345,
        "fwd_title": "SomeChan",
    }
    _text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=3)
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "this message" in labels
    assert "image + caption" not in labels


def test_build_forward_choice_panel_callbacks_round_trip():
    payload = {
        "source": "media",
        "kind": "image",
        "caption": "hi",
        "fwd_channel_id": 99,
    }
    _text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=42)
    flat = [b for row in buttons for b in row]
    full_btn = next(b for b in flat if "image + caption" in b.text.lower())
    txt_btn = next(b for b in flat if "caption only" in b.text.lower())
    week_btn = next(b for b in flat if "week" in b.text.lower())
    assert parse_callback(full_btn.data) == ("F_FULL", 42, None)
    assert parse_callback(txt_btn.data) == ("F_TXT", 42, None)
    assert parse_callback(week_btn.data) == ("F_WK", 42, None)


def test_build_forward_choice_panel_shows_from_this_msg_when_msg_id_present():
    payload = {
        "source": "text",
        "text": "x",
        "fwd_channel_id": 123,
        "fwd_msg_id": 456,
    }
    _text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=7)
    flat = [b for row in buttons for b in row]
    from_btn = next(b for b in flat if "from this msg" in b.text.lower())
    assert parse_callback(from_btn.data) == ("F_FROM", 7, None)


def test_build_forward_choice_panel_hides_from_this_msg_when_no_msg_id():
    """Forwards without channel_post (rare — anonymous or PSA) → no F_FROM."""
    payload = {
        "source": "text",
        "text": "x",
        "fwd_channel_id": 123,
        # no fwd_msg_id
    }
    _text, buttons = build_forward_choice_panel(payload=payload, panel_msg_id=8)
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "from this msg" not in labels


def test_parse_callback_accepts_f_from():
    data = encode_callback("F_FROM", 99)
    assert parse_callback(data) == ("F_FROM", 99, None)


# ---------------------------------------------------------------------------
# Telegram album merging — multi-photo posts with shared grouped_id collapse
# into ONE BurstItem so the panel doesn't show "3 items" for what's
# semantically one forwarded post.
# ---------------------------------------------------------------------------


def test_merge_album_items_collapses_grouped_id_into_one_item():
    items = [
        _item("file", source="media", kind="video", name="IMG.mp4", grouped_id=999, caption=""),
        _item("file", source="media", kind="image", name="a.jpg", grouped_id=999, caption="the post"),
        _item("file", source="media", kind="image", name="b.jpg", grouped_id=999, caption=""),
    ]
    merged = merge_album_items(items)
    assert len(merged) == 1
    assert merged[0].payload["album_size"] == 3
    # Caption was on the SECOND item; merger should still pick it up.
    assert merged[0].payload["caption"] == "the post"


def test_merge_album_items_passes_through_singletons():
    items = [
        _item("url", url="https://example.com/a"),
        _item("file", source="media", kind="image", name="x.jpg"),  # no grouped_id
    ]
    merged = merge_album_items(items)
    assert len(merged) == 2  # nothing to merge


def test_merge_album_items_keeps_forward_metadata_for_album():
    """A forwarded album should still drive the forward picker after merge."""
    items = [
        _item(
            "file",
            source="media",
            kind="image",
            name="a.jpg",
            grouped_id=42,
            caption="news",
            fwd_channel_id=12345,
            fwd_msg_id=678,
            fwd_title="NewsChan",
        ),
        _item("file", source="media", kind="image", name="b.jpg", grouped_id=42, caption=""),
    ]
    merged = merge_album_items(items)
    assert len(merged) == 1
    assert merged[0].payload["fwd_channel_id"] == 12345
    assert merged[0].payload["fwd_msg_id"] == 678
    assert merged[0].payload["caption"] == "news"
    assert merged[0].payload["album_size"] == 2


def test_merge_album_items_single_grouped_member_drops_to_one_item():
    """A grouped_id that only has one member (rare) shouldn't show 'album'."""
    items = [
        _item("file", source="media", kind="image", name="lone.jpg", grouped_id=7, caption=""),
    ]
    merged = merge_album_items(items)
    assert len(merged) == 1
    # album_size NOT set — this was a singleton, not a multi-attachment album.
    assert "album_size" not in merged[0].payload


def test_build_forward_choice_panel_falls_back_to_channel_label():
    """When fwd_title isn't set, header still renders gracefully."""
    payload = {
        "source": "text",
        "text": "x",
        "fwd_channel_id": 12345,
    }
    text, _buttons = build_forward_choice_panel(payload=payload, panel_msg_id=5)
    assert "channel" in text.lower()


# ---------------------------------------------------------------------------
# /confirm slash command — sets/clears _chat_state["confirm_disabled"]
# ---------------------------------------------------------------------------


@dataclass
class _FakeApp:
    _chat_state: dict = field(default_factory=dict)
    user_session_ready: bool = True


@dataclass
class _FakeEvent:
    chat_id: int = 42
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str, **_kw: Any) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_confirm_off_sets_disabled_flag():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "confirm", "args": ["off"]}, app=app)
    assert app._chat_state[42]["confirm_disabled"] is True
    assert any("off" in r.lower() or "disabled" in r.lower() for r in event.replies)


@pytest.mark.asyncio
async def test_confirm_on_clears_disabled_flag():
    from unread.bot.handlers import cmds

    app = _FakeApp(_chat_state={42: {"confirm_disabled": True}})
    event = _FakeEvent()
    await cmds.handle(event, {"name": "confirm", "args": ["on"]}, app=app)
    assert app._chat_state[42].get("confirm_disabled", False) is False


@pytest.mark.asyncio
async def test_confirm_bare_reports_current_state():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "confirm", "args": []}, app=app)
    # Should reply with something about the current state — and NOT
    # silently flip the flag.
    assert event.replies, "expected a reply describing current state"
    assert (
        "confirm_disabled" not in app._chat_state.get(42, {})
        or app._chat_state[42]["confirm_disabled"] is False
    )


@pytest.mark.asyncio
async def test_confirm_with_garbage_arg_is_rejected():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "confirm", "args": ["maybe"]}, app=app)
    # Garbage arg → no state change, friendly error reply.
    assert app._chat_state.get(42, {}).get("confirm_disabled") in (None, False)
    assert event.replies
