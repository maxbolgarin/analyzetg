"""`unread.ai.vision_provider` factory + adapter constructibility.

These tests verify the factory dispatches correctly and that each
adapter raises a clear `ProviderUnavailableError` when its credentials
are missing — they do not make real network calls.
"""

from __future__ import annotations

import pytest

from unread.ai.providers import ProviderUnavailableError
from unread.ai.vision_provider import (
    AnthropicVisionProvider,
    GoogleVisionProvider,
    LocalVisionProvider,
    OpenAIVisionProvider,
    OpenRouterVisionProvider,
    make_vision_provider,
)
from unread.config import Settings


def _settings_with_keys(**keys) -> Settings:
    s = Settings()
    if "openai" in keys:
        s.openai.api_key = keys["openai"]
    if "openrouter" in keys:
        s.openrouter.api_key = keys["openrouter"]
    if "anthropic" in keys:
        s.anthropic.api_key = keys["anthropic"]
    if "google" in keys:
        s.google.api_key = keys["google"]
    return s


def test_factory_dispatches_each_provider():
    """Each provider name maps to its dedicated adapter class."""
    s = _settings_with_keys(openai="sk-test", openrouter="or-test")
    assert isinstance(make_vision_provider("openai", s), OpenAIVisionProvider)
    assert isinstance(make_vision_provider("openrouter", s), OpenRouterVisionProvider)
    assert isinstance(make_vision_provider("local", s), LocalVisionProvider)


def test_factory_dispatches_anthropic_with_key():
    s = _settings_with_keys(anthropic="sk-ant-test")
    assert isinstance(make_vision_provider("anthropic", s), AnthropicVisionProvider)


def test_factory_dispatches_google_with_key():
    s = _settings_with_keys(google="AIza-test")
    assert isinstance(make_vision_provider("google", s), GoogleVisionProvider)


def test_factory_unknown_provider_raises():
    s = _settings_with_keys(openai="sk-test")
    with pytest.raises(ProviderUnavailableError):
        make_vision_provider("not-a-provider", s)


def test_openai_adapter_requires_key():
    s = _settings_with_keys()  # no keys
    with pytest.raises(ProviderUnavailableError):
        OpenAIVisionProvider(s)


def test_openrouter_adapter_requires_key():
    s = _settings_with_keys(openai="sk-test")
    with pytest.raises(ProviderUnavailableError):
        OpenRouterVisionProvider(s)


def test_anthropic_adapter_requires_key():
    s = _settings_with_keys()
    with pytest.raises(ProviderUnavailableError):
        AnthropicVisionProvider(s)


def test_google_adapter_requires_key():
    s = _settings_with_keys()
    with pytest.raises(ProviderUnavailableError):
        GoogleVisionProvider(s)


def test_local_adapter_constructs_without_key():
    """Local server has a placeholder API key by default; the adapter
    should construct with no real key set."""
    s = _settings_with_keys()
    # Should NOT raise.
    LocalVisionProvider(s)


def test_default_vision_models_are_set():
    """Each adapter exposes a `default_vision_model` for the slot
    resolver to fall back on when `ai.vision_model` is empty."""
    assert OpenAIVisionProvider.default_vision_model
    assert OpenRouterVisionProvider.default_vision_model
    assert LocalVisionProvider.default_vision_model
    assert AnthropicVisionProvider.default_vision_model
    assert GoogleVisionProvider.default_vision_model
