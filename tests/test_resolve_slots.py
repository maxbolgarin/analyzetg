"""Per-slot resolver behaviour: chat / filter / audio / vision.

Covers:
  - Default fallback chain (no override → resolver picks "openai" + class default).
  - Explicit per-slot overrides win.
  - Audio capability snap (anthropic / google / openrouter → openai;
    only openai + local speak SDK-compatible Whisper).
  - Legacy `ai.provider` mirroring still seeds slot fields via
    `_apply_one_override` for back-compat with old DB rows.
"""

from __future__ import annotations

from unread.ai.providers import (
    resolve_audio,
    resolve_chat,
    resolve_filter,
    resolve_vision,
)
from unread.config import Settings


def _settings_with(**overrides) -> Settings:
    """Construct a fresh Settings object with the given AICfg overrides.

    Skips any DB-overlay machinery so tests stay deterministic — we
    only care about the resolver math here.
    """
    s = Settings()
    for key, value in overrides.items():
        setattr(s.ai, key, value)
    return s


def test_default_resolution_falls_back_to_openai():
    s = _settings_with()
    assert resolve_chat(s) == ("openai", "gpt-5.4-mini")
    assert resolve_filter(s) == ("openai", "gpt-5.4-nano")
    assert resolve_audio(s) == ("openai", "gpt-4o-mini-transcribe")
    assert resolve_vision(s) == ("openai", "gpt-4o-mini")


def test_per_slot_provider_overrides_default():
    s = _settings_with(
        chat_provider="anthropic",
        filter_provider="google",
        vision_provider="anthropic",
    )
    assert resolve_chat(s)[0] == "anthropic"
    assert resolve_filter(s)[0] == "google"
    assert resolve_vision(s)[0] == "anthropic"
    # Audio left empty → still openai
    assert resolve_audio(s)[0] == "openai"


def test_per_slot_model_pin_wins_over_provider_default():
    s = _settings_with(chat_provider="anthropic", chat_model="claude-opus-4-7")
    assert resolve_chat(s) == ("anthropic", "claude-opus-4-7")


def test_audio_snaps_anthropic_to_openai():
    """Anthropic has no Whisper-shape API; the resolver silently snaps
    `audio_provider` back to openai so downstream code never tries to
    construct an audio client against an unsupported backend."""
    s = _settings_with(audio_provider="anthropic")
    provider, _model = resolve_audio(s)
    assert provider == "openai"


def test_audio_snaps_google_to_openai():
    s = _settings_with(audio_provider="google")
    provider, _model = resolve_audio(s)
    assert provider == "openai"


def test_audio_snaps_openrouter_to_openai():
    """OpenRouter advertises `/audio/transcriptions` but rejects multipart
    with a JSON-parse 400 — its endpoint expects a JSON body shaped
    `{"input_audio": {"data": "<b64>", "format": "..."}}` that the
    OpenAI Python SDK doesn't emit. Resolver snaps to openai so an
    upstream OPENAI_API_KEY can do the transcription instead."""
    s = _settings_with(audio_provider="openrouter")
    provider, _model = resolve_audio(s)
    assert provider == "openai"


def test_audio_keeps_local():
    """Local servers (Ollama / whisper.cpp / etc.) speak the multipart
    Whisper API verbatim — leave them alone."""
    s = _settings_with(audio_provider="local")
    assert resolve_audio(s)[0] == "local"


def test_legacy_provider_seeds_all_slots_via_apply_override():
    """`_apply_one_override` mirrors a stray `ai.provider` row onto
    every empty slot, mirroring what the bootstrap migration does for
    persisted rows."""
    from unread.db.repo import _apply_one_override

    s = Settings()
    _apply_one_override(s, "ai.provider", "anthropic")
    assert s.ai.chat_provider == "anthropic"
    assert s.ai.filter_provider == "anthropic"
    # Audio capability snap also applies during the legacy mirror.
    assert s.ai.audio_provider == "openai"
    assert s.ai.vision_provider == "anthropic"


def test_legacy_provider_does_not_overwrite_explicit_slot():
    """If a slot already has its own value, the legacy mirror leaves
    it alone — explicit per-slot config beats the umbrella."""
    from unread.db.repo import _apply_one_override

    s = Settings()
    s.ai.chat_provider = "google"
    _apply_one_override(s, "ai.provider", "anthropic")
    assert s.ai.chat_provider == "google"  # explicit wins
    assert s.ai.filter_provider == "anthropic"  # mirrored


def test_legacy_openrouter_snaps_audio_to_openai_via_apply_override():
    """OpenRouter advertises a Whisper endpoint but rejects multipart;
    `_AUDIO_PROVIDERS = {openai, local}` is the source of truth.
    `_apply_one_override("ai.provider", "openrouter")` must mirror to
    chat/filter/vision verbatim and snap audio to openai."""
    from unread.db.repo import _apply_one_override

    s = Settings()
    _apply_one_override(s, "ai.provider", "openrouter")
    assert s.ai.chat_provider == "openrouter"
    assert s.ai.filter_provider == "openrouter"
    assert s.ai.vision_provider == "openrouter"
    assert s.ai.audio_provider == "openai"  # capability snap


def test_legacy_openai_audio_default_only_when_provider_is_openai():
    """`settings.openai.audio_model_default` should only seed
    `resolve_audio`'s model when the audio slot routes to openai."""
    s = _settings_with(audio_provider="openai")
    s.openai.audio_model_default = "gpt-4o-transcribe"
    assert resolve_audio(s) == ("openai", "gpt-4o-transcribe")
    # Different audio provider → legacy openai default is ignored.
    s2 = _settings_with(audio_provider="local")
    s2.openai.audio_model_default = "gpt-4o-transcribe"
    provider, model = resolve_audio(s2)
    assert provider == "local"
    assert model != "gpt-4o-transcribe"
