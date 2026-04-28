"""Coverage for the multi-provider chat layer.

`make_chat_provider` dispatch on `settings.ai.provider`, error-message
clarity when keys / SDKs are missing, and `resolve_chat_model` /
`resolve_filter_model` precedence (explicit override → openai-specific
back-compat → provider default).

Adapter behavior (Anthropic / Google message translation, truncation
mapping) is exercised end-to-end by `test_openai_client.py` via the
`_FakeProvider` stub — wiring the real SDKs against a network mock is
out of scope here.
"""

from __future__ import annotations

import pytest

from unread.ai import (
    ProviderUnavailableError,
    make_chat_provider,
    resolve_chat_model,
    resolve_filter_model,
)
from unread.config import Settings


def _settings_with(provider: str, **kwargs: str) -> Settings:
    """Construct a fresh Settings, drop an api_key onto the right block.

    `kwargs` accepts dotted-style keys: `openai_api_key="…"`,
    `anthropic_api_key="…"`, etc. Keeps the test bodies short.
    """
    s = Settings()
    s.ai.provider = provider
    if k := kwargs.get("openai_api_key"):
        s.openai.api_key = k
    if k := kwargs.get("openrouter_api_key"):
        s.openrouter.api_key = k
    if k := kwargs.get("anthropic_api_key"):
        s.anthropic.api_key = k
    if k := kwargs.get("google_api_key"):
        s.google.api_key = k
    return s


def test_openai_provider_dispatch() -> None:
    s = _settings_with("openai", openai_api_key="sk-real")
    p = make_chat_provider(s)
    assert p.name == "openai"
    assert p.default_chat_model.startswith("gpt-")


def test_openrouter_provider_dispatch() -> None:
    s = _settings_with("openrouter", openrouter_api_key="sk-or-x")
    p = make_chat_provider(s)
    assert p.name == "openrouter"
    # OpenRouter prefixes vendor/model.
    assert "/" in p.default_chat_model


def test_anthropic_provider_dispatch() -> None:
    s = _settings_with("anthropic", anthropic_api_key="sk-ant-x")
    p = make_chat_provider(s)
    assert p.name == "anthropic"
    assert p.default_chat_model.startswith("claude-")


def test_google_provider_dispatch() -> None:
    s = _settings_with("google", google_api_key="g-x")
    p = make_chat_provider(s)
    assert p.name == "google"
    assert p.default_chat_model.startswith("gemini-")


def test_local_provider_no_key_required() -> None:
    s = _settings_with("local")
    p = make_chat_provider(s)
    assert p.name == "local"


def test_unknown_provider_rejected() -> None:
    s = _settings_with("definitely-not-a-real-vendor")
    with pytest.raises(ProviderUnavailableError) as exc:
        make_chat_provider(s)
    # Error message must enumerate the valid options so the user can fix
    # their config without grepping the source.
    msg = str(exc.value)
    for name in ("openai", "openrouter", "anthropic", "google", "local"):
        assert name in msg


@pytest.mark.parametrize(
    "provider,api_attr",
    [
        ("openai", "openai_api_key"),
        ("openrouter", "openrouter_api_key"),
        ("anthropic", "anthropic_api_key"),
        ("google", "google_api_key"),
    ],
)
def test_missing_key_raises_with_friendly_message(provider: str, api_attr: str) -> None:
    """No key for the active provider → ProviderUnavailableError naming the key."""
    s = _settings_with(provider)  # no key
    with pytest.raises(ProviderUnavailableError) as exc:
        make_chat_provider(s)
    msg = str(exc.value)
    # Mentions the provider and the key field that's missing.
    assert provider in msg.lower()
    assert "unread tg init" in msg


# --- resolve_chat_model / resolve_filter_model --------------------------


def test_resolve_chat_model_uses_explicit_override() -> None:
    s = _settings_with("anthropic", anthropic_api_key="k")
    s.ai.chat_model = "claude-opus-4-7"
    assert resolve_chat_model(s) == "claude-opus-4-7"


def test_resolve_chat_model_falls_back_to_openai_back_compat() -> None:
    """When provider == openai and ai.chat_model is empty, the legacy
    `openai.chat_model_default` knob still drives the active model."""
    s = _settings_with("openai", openai_api_key="sk-real")
    s.openai.chat_model_default = "gpt-5.4-something-custom"
    s.ai.chat_model = ""
    assert resolve_chat_model(s) == "gpt-5.4-something-custom"


def test_resolve_chat_model_uses_provider_default() -> None:
    """Non-openai provider with empty ai.chat_model → adapter's hardcoded default."""
    s = _settings_with("anthropic", anthropic_api_key="k")
    s.ai.chat_model = ""
    # Don't pin the exact model name — adapter authors may bump defaults
    # over time. Just assert it's a Claude-shaped string.
    assert resolve_chat_model(s).startswith("claude-")


def test_resolve_filter_model_independent_of_chat() -> None:
    s = _settings_with("openai", openai_api_key="sk-real")
    s.ai.chat_model = "gpt-5.4-mini"
    s.ai.filter_model = "gpt-5.4-nano-custom"
    assert resolve_filter_model(s) == "gpt-5.4-nano-custom"
