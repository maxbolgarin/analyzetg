"""Trust gate on `ai.base_url` overrides.

A user (or typo) setting ``ai.base_url`` to ``api.openai.com.attacker.tld``
would silently exfiltrate the OpenAI key. The provider construction now
refuses untrusted hosts unless ``ai.base_url_trusted = true`` is set.
"""

from __future__ import annotations

import pytest

from unread.ai import make_chat_provider
from unread.ai.providers import ProviderUnavailableError
from unread.config import get_settings, reset_settings


@pytest.fixture(autouse=True)
def _isolate_settings():
    reset_settings()
    yield
    reset_settings()


def _set(s, **kwargs):
    """Apply field updates onto the live AI cfg block."""
    for k, v in kwargs.items():
        setattr(s.ai, k, v)


def test_openai_default_base_url_is_trusted():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = ""  # default — uses api.openai.com
    p = make_chat_provider(s)
    assert p.name == "openai"


def test_openai_typosquat_host_rejected():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = "https://api.openai.com.attacker.tld/v1"
    s.ai.base_url_trusted = False
    with pytest.raises(ProviderUnavailableError) as ei:
        make_chat_provider(s)
    msg = str(ei.value)
    assert "api.openai.com.attacker.tld" in msg
    assert "ai.base_url_trusted" in msg


def test_openai_explicit_opt_in_allows_arbitrary_host():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = "https://my-corp-proxy.example.com/v1"
    s.ai.base_url_trusted = True
    p = make_chat_provider(s)
    assert p.name == "openai"


def test_openai_localhost_always_allowed():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = "http://localhost:8080/v1"
    # No opt-in needed — localhost is by definition not an external
    # exfiltration path.
    s.ai.base_url_trusted = False
    p = make_chat_provider(s)
    assert p.name == "openai"


def test_openai_subdomain_of_trusted_host_accepted():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = "https://eu.api.openai.com/v1"
    p = make_chat_provider(s)
    assert p.name == "openai"


def test_openrouter_typosquat_rejected():
    s = get_settings()
    s.openrouter.api_key = "rk-test"
    s.ai.provider = "openrouter"
    s.ai.base_url = "https://openrouter.ai.attacker.tld/v1"
    with pytest.raises(ProviderUnavailableError):
        make_chat_provider(s)


def test_local_provider_accepts_arbitrary_host():
    """Local is by design pointing at user infra — no allowlist applies."""
    s = get_settings()
    s.ai.provider = "local"
    s.ai.base_url = "http://192.168.1.42:11434/v1"
    p = make_chat_provider(s)
    assert p.name == "local"


def test_unparseable_base_url_rejected():
    s = get_settings()
    s.openai.api_key = "sk-test"
    s.ai.provider = "openai"
    s.ai.base_url = "not-a-url"
    with pytest.raises(ProviderUnavailableError):
        make_chat_provider(s)
