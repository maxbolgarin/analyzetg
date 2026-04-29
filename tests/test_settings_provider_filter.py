"""`unread settings` hides legacy OpenAI-only rows when chat provider is non-OpenAI.

`resolve_chat_model` only honors `settings.openai.chat_model_default`
when `ai.provider == "openai"`. Showing the row under a different
provider creates a phantom-edit experience (set the value, no effect).
"""

from __future__ import annotations

from unread.settings.commands import (
    _OPENAI_PROVIDER_ONLY_KEYS,
    _SETTINGS,
    _visible_settings,
)


def test_openai_provider_shows_all_rows():
    visible = _visible_settings("openai")
    assert visible == _SETTINGS


def test_anthropic_provider_hides_legacy_openai_keys():
    visible = _visible_settings("anthropic")
    visible_keys = {sd.key for sd in visible}
    for k in _OPENAI_PROVIDER_ONLY_KEYS:
        assert k not in visible_keys, f"{k} should be hidden under anthropic"
    # OpenAI-backed audio / vision are still visible because they
    # apply regardless of chat provider.
    assert "openai.audio_model_default" in visible_keys
    assert "enrich.vision_model" in visible_keys


def test_google_provider_hides_legacy_openai_keys():
    visible = _visible_settings("google")
    visible_keys = {sd.key for sd in visible}
    assert "openai.chat_model_default" not in visible_keys
    assert "openai.filter_model_default" not in visible_keys


def test_local_provider_hides_legacy_openai_keys():
    visible = _visible_settings("local")
    visible_keys = {sd.key for sd in visible}
    assert "openai.chat_model_default" not in visible_keys
    assert "openai.filter_model_default" not in visible_keys
