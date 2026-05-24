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

from unread.bot.burst import BurstItem, combinable_items, summary_line
from unread.bot.confirm import (
    PendingRun,
    RunOptions,
    build_batch_panel,
    build_initial_panel,
    default_options,
    default_preset_for_kind,
    encode_callback,
    parse_callback,
    prune_pending_runs,
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
