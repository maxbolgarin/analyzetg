"""`unread settings` per-slot Models section + API keys.

Each of the four model slots (chat / filter / audio / vision) appears
as a single compound row that writes both `ai.<slot>_provider` and
`ai.<slot>_model`. The legacy `_OPENAI_PROVIDER_ONLY_KEYS` filter
went away with `ai.provider` — `_visible_settings` is now a no-op
identity transform.
"""

from __future__ import annotations

from unread.settings.commands import (
    _SETTINGS,
    _SLOT_PROVIDERS,
    _TOP_SETTINGS,
    _visible_settings,
)


def test_visible_settings_is_identity():
    """The legacy provider-aware filter is gone; pool round-trips unchanged."""
    for active in ("openai", "anthropic", "google", "openrouter", "local"):
        assert _visible_settings(active, _SETTINGS) == _SETTINGS


def test_top_level_has_four_model_slots():
    """The top-level menu must surface all four per-slot rows."""
    slot_keys = {sd.key for sd in _TOP_SETTINGS if sd.kind == "slot_model"}
    assert slot_keys == {"__slot_chat__", "__slot_filter__", "__slot_audio__", "__slot_vision__"}


def test_top_level_has_five_api_key_rows():
    """Each provider (incl. local URL) gets one API-keys row."""
    api_keys = {sd.key for sd in _TOP_SETTINGS if sd.kind == "api_key"}
    assert api_keys == {
        "__api_key:openai__",
        "__api_key:openrouter__",
        "__api_key:anthropic__",
        "__api_key:google__",
        "__api_key:local__",
    }


def test_audio_slot_excludes_non_whisper_providers():
    """Capability filter — only providers whose audio API is on-the-wire
    compatible with the OpenAI SDK's multipart upload can be picked for
    audio. anthropic / google have no audio endpoint at all; openrouter
    advertises one but rejects multipart with a JSON 400. See
    `unread.ai.providers._AUDIO_PROVIDERS`."""
    assert "anthropic" not in _SLOT_PROVIDERS["audio"]
    assert "google" not in _SLOT_PROVIDERS["audio"]
    assert "openrouter" not in _SLOT_PROVIDERS["audio"]
    assert set(_SLOT_PROVIDERS["audio"]) == {"openai", "local"}


def test_chat_filter_vision_slots_accept_all_providers():
    expected = {"openai", "openrouter", "anthropic", "google", "local"}
    for slot in ("chat", "filter", "vision"):
        assert set(_SLOT_PROVIDERS[slot]) == expected
