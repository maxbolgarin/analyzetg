"""Per-provider model catalog (`unread.ai.models`).

The registry powers the settings picker AND seeds default pricing for
catalog models that the user hasn't pinned in config.toml. Tests:

  - Each provider's defaults appear in its catalog.
  - Cross-provider isolation: GPT models don't leak into Anthropic
    listings and vice versa (the core requirement that triggered this
    change — picking Anthropic shouldn't surface GPT options).
  - Catalog pricing falls through `chat_cost` so picking a catalog model
    yields a real cost without editing config.toml.
  - Picker provider routing: `ai.chat_model` follows active provider;
    OpenAI-namespaced rows stay pinned to OpenAI.
"""

from __future__ import annotations

from unread.ai.anthropic_provider import AnthropicProvider
from unread.ai.google_provider import GoogleProvider
from unread.ai.models import (
    all_known_models,
    find_model,
    models_for_provider,
    supported_providers,
)
from unread.ai.openai_provider import OpenAIProvider
from unread.config import ChatPricing, PricingCfg, Settings
from unread.util.pricing import chat_cost, chat_pricing_for


def test_supported_providers_covers_all_adapters():
    names = set(supported_providers())
    assert {"openai", "anthropic", "google", "openrouter", "local"} <= names


def test_provider_defaults_appear_in_their_catalog():
    """Each provider's `default_chat_model` must exist in its own catalog."""
    for provider, default in (
        ("openai", OpenAIProvider.default_chat_model),
        ("anthropic", AnthropicProvider.default_chat_model),
        ("google", GoogleProvider.default_chat_model),
    ):
        ids = {m.id for m in models_for_provider(provider)}
        assert default in ids, (
            f"{provider}.default_chat_model={default!r} not in registry; "
            "the picker would offer no row matching the resolved default"
        )


def test_anthropic_pool_has_no_openai_models():
    anth_ids = {m.id for m in models_for_provider("anthropic")}
    assert not any(i.startswith("gpt-") for i in anth_ids)
    assert not any(i.startswith("gemini-") for i in anth_ids)


def test_openai_pool_has_no_claude_or_gemini_models():
    oa_ids = {m.id for m in models_for_provider("openai")}
    assert not any("claude" in i for i in oa_ids)
    assert not any("gemini" in i for i in oa_ids)


def test_google_pool_has_no_gpt_or_claude_models():
    g_ids = {m.id for m in models_for_provider("google")}
    assert not any(i.startswith("gpt-") for i in g_ids)
    assert not any("claude" in i for i in g_ids)


def test_local_pool_is_empty_so_picker_shows_custom_only():
    assert models_for_provider("local") == []


def test_role_filter_chat_includes_filter_tier_for_pinning_budget_models():
    chat_models = models_for_provider("openai", role="chat")
    ids = [m.id for m in chat_models]
    # filter-tier nano shows up in chat list — users pin it for the
    # cheap-pass slot directly.
    assert "gpt-5.4-nano" in ids


def test_role_filter_filter_excludes_flagships():
    filter_models = models_for_provider("openai", role="filter")
    ids = {m.id for m in filter_models}
    assert "gpt-5.5" not in ids
    assert "gpt-5.4-nano" in ids


def test_audio_role_only_returns_audio_models():
    audio = models_for_provider("openai", role="audio")
    assert all(m.role == "audio" for m in audio)
    ids = {m.id for m in audio}
    assert "gpt-4o-mini-transcribe" in ids


def test_find_model_resolves_across_providers():
    assert find_model("claude-opus-4-7").id == "claude-opus-4-7"
    assert find_model("gpt-5.4-mini").id == "gpt-5.4-mini"
    assert find_model("gemini-2.5-flash").id == "gemini-2.5-flash"
    assert find_model("nope-not-real") is None


def test_all_known_models_contains_each_providers_flagships():
    ids = {m.id for m in all_known_models()}
    assert {
        "gpt-5.5",
        "gpt-5.4-mini",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "gemini-2.5-flash",
    } <= ids


def test_chat_cost_falls_back_to_catalog_when_user_pricing_missing():
    """Picking a catalog model from `unread settings` should yield a
    real cost without editing config.toml. Implemented by
    `chat_pricing_for` falling through to the catalog.
    """
    s = Settings()
    # No user-supplied pricing for claude-opus-4-7; catalog supplies $5/$25.
    s.pricing = PricingCfg(chat={}, audio={})
    cost = chat_cost(
        "claude-opus-4-7",
        prompt_tokens=1_000_000,
        cached_tokens=0,
        completion_tokens=0,
        settings=s,
    )
    assert cost == 5.0


def test_chat_cost_user_pricing_wins_over_catalog():
    """If the user pinned a custom price in config.toml, it takes
    precedence over the catalog default."""
    s = Settings()
    s.pricing = PricingCfg(
        chat={"gpt-5.4-mini": ChatPricing(input=99.0, cached_input=99.0, output=99.0)},
        audio={},
    )
    cost = chat_cost(
        "gpt-5.4-mini",
        prompt_tokens=1_000_000,
        cached_tokens=0,
        completion_tokens=0,
        settings=s,
    )
    assert cost == 99.0


def testchat_pricing_for_returns_none_for_audio_model():
    s = Settings()
    assert chat_pricing_for("gpt-4o-mini-transcribe", s) is None


def test_picker_provider_routing_ai_keys_follow_active_provider():
    """Per-slot routing — each slot has its own provider+model. The
    compound picker uses `_SLOT_PROVIDERS` to constrain provider
    options (audio excludes anthropic + google) and `_SLOT_ROLE` to
    pick the catalog filter.
    """
    from unread.settings.commands import _SLOT_PROVIDERS, _SLOT_ROLE

    # Audio slot is restricted to Whisper-shape providers.
    assert "anthropic" not in _SLOT_PROVIDERS["audio"]
    assert "google" not in _SLOT_PROVIDERS["audio"]
    assert set(_SLOT_PROVIDERS["audio"]) == {"openai", "openrouter", "local"}
    # Chat / filter / vision accept all five providers.
    for slot in ("chat", "filter", "vision"):
        assert set(_SLOT_PROVIDERS[slot]) == {"openai", "openrouter", "anthropic", "google", "local"}
    # Role mapping mirrors the slot name.
    assert _SLOT_ROLE == {"chat": "chat", "filter": "filter", "audio": "audio", "vision": "vision"}


def test_per_slot_routing_picks_correct_catalog():
    """`models_for_provider(provider, role=slot_role)` is the catalog
    backing the compound picker step 2. Spot-check that slot+provider
    combinations return non-empty pools where they should."""
    # Anthropic vision: claude-* models must show up.
    anth_vision = {m.id for m in models_for_provider("anthropic", role="vision")}
    assert any(mid.startswith("claude-") for mid in anth_vision), anth_vision
    # OpenRouter audio: at least one Whisper alias.
    or_audio = {m.id for m in models_for_provider("openrouter", role="audio")}
    assert any("whisper" in mid for mid in or_audio), or_audio
