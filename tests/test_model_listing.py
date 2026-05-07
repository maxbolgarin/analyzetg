"""Live model-list fetching: cache + role filter behaviour.

Network calls are stubbed via monkeypatching the per-provider fetch
helpers — we test the dispatch + cache + role filter logic, not the
upstream APIs themselves.
"""

from __future__ import annotations

import pytest

from unread.ai import model_listing
from unread.config import Settings


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with empty fetch + verify caches."""
    model_listing.clear_cache()
    model_listing.clear_verified_cache()
    yield
    model_listing.clear_cache()
    model_listing.clear_verified_cache()


def test_role_filter_audio_strict():
    """Audio role only accepts ids with whisper / transcribe in the name."""
    raw = ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini", "claude-haiku"]
    out = model_listing._filter_for_role("audio", raw)
    assert "whisper-1" in out
    assert "gpt-4o-transcribe" in out
    assert "gpt-4o-mini" not in out
    assert "claude-haiku" not in out


def test_role_filter_chat_drops_audio_and_embed():
    raw = [
        "gpt-4o-mini",
        "whisper-1",
        "text-embedding-3-small",
        "claude-sonnet-4-6",
    ]
    out = model_listing._filter_for_role("chat", raw)
    assert "gpt-4o-mini" in out
    assert "claude-sonnet-4-6" in out
    assert "whisper-1" not in out
    assert "text-embedding-3-small" not in out


def test_clear_cache_drops_specific_provider(monkeypatch):
    """`clear_cache(provider)` drops every role for that provider."""
    model_listing._FETCHED_CACHE[("openai", "chat")] = ["gpt-4o-mini"]
    model_listing._FETCHED_CACHE[("openai", "audio")] = ["whisper-1"]
    model_listing._FETCHED_CACHE[("anthropic", "chat")] = ["claude-haiku-4-5"]

    model_listing.clear_cache("openai")
    assert ("openai", "chat") not in model_listing._FETCHED_CACHE
    assert ("openai", "audio") not in model_listing._FETCHED_CACHE
    assert ("anthropic", "chat") in model_listing._FETCHED_CACHE


def test_clear_cache_drops_specific_provider_and_role():
    model_listing._FETCHED_CACHE[("openai", "chat")] = ["gpt-4o-mini"]
    model_listing._FETCHED_CACHE[("openai", "audio")] = ["whisper-1"]

    model_listing.clear_cache("openai", "audio")
    assert ("openai", "chat") in model_listing._FETCHED_CACHE
    assert ("openai", "audio") not in model_listing._FETCHED_CACHE


def test_is_cached_reports_state():
    assert not model_listing.is_cached("openai", "chat")
    model_listing._FETCHED_CACHE[("openai", "chat")] = ["gpt-4o-mini"]
    assert model_listing.is_cached("openai", "chat")


def test_cached_models_returns_copy():
    """Mutating the returned list must not poison the cache."""
    model_listing._FETCHED_CACHE[("openai", "chat")] = ["gpt-4o-mini"]
    out = model_listing.cached_models("openai", "chat")
    out.append("smuggled")
    assert model_listing._FETCHED_CACHE[("openai", "chat")] == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_fetch_models_caches_result(monkeypatch):
    """A successful fetch populates the cache; the next call is a hit."""

    async def fake_openai_compat(provider, settings):
        return ["gpt-4o-mini", "whisper-1", "gpt-4o"]

    monkeypatch.setattr(model_listing, "_fetch_openai_compat", fake_openai_compat)

    s = Settings()
    out1 = await model_listing.fetch_models("openai", "chat", s)
    assert "gpt-4o-mini" in out1
    assert "whisper-1" not in out1  # role filter strips audio
    assert model_listing.is_cached("openai", "chat")

    # Second call hits the cache; we'd notice if `fake_openai_compat`
    # ran again because the cache key was the same.
    monkeypatch.setattr(
        model_listing, "_fetch_openai_compat", lambda *a, **kw: pytest.fail("cache miss")
    )
    out2 = await model_listing.fetch_models("openai", "chat", s)
    assert out2 == out1


@pytest.mark.asyncio
async def test_fetch_models_unknown_provider_returns_empty():
    s = Settings()
    out = await model_listing.fetch_models("not-a-provider", "chat", s)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_models_no_anthropic_key_returns_empty():
    """Without a key, the Anthropic fetcher returns [] and the cache stores []."""
    s = Settings()
    s.anthropic.api_key = ""
    out = await model_listing.fetch_models("anthropic", "chat", s)
    assert out == []


@pytest.mark.asyncio
async def test_verify_provider_no_key():
    """Verifying a keyed provider with no key returns (False, 'no API key')."""
    s = Settings()
    s.openai.api_key = ""
    ok, msg = await model_listing.verify_provider("openai", s)
    assert ok is False
    assert "no API key" in msg


@pytest.mark.asyncio
async def test_verify_provider_caches_result(monkeypatch):
    """Successful smoke test caches; second call returns from cache."""

    async def fake_verify(name, settings):
        return True, ""

    monkeypatch.setattr(model_listing, "_verify_uncached", fake_verify)

    s = Settings()
    ok1, _ = await model_listing.verify_provider("openai", s)
    assert ok1
    monkeypatch.setattr(
        model_listing,
        "_verify_uncached",
        lambda *a, **kw: pytest.fail("cache miss"),
    )
    ok2, _ = await model_listing.verify_provider("openai", s)
    assert ok2


@pytest.mark.asyncio
async def test_verify_provider_clear_cache_re_runs(monkeypatch):
    """`clear_verified_cache(provider)` makes the next call re-test."""
    calls = {"n": 0}

    async def fake_verify(name, settings):
        calls["n"] += 1
        return True, ""

    monkeypatch.setattr(model_listing, "_verify_uncached", fake_verify)

    s = Settings()
    await model_listing.verify_provider("openai", s)
    await model_listing.verify_provider("openai", s)
    assert calls["n"] == 1  # second hit cache

    model_listing.clear_verified_cache("openai")
    await model_listing.verify_provider("openai", s)
    assert calls["n"] == 2  # re-tested after clear


@pytest.mark.asyncio
async def test_verify_provider_unknown_returns_failure():
    s = Settings()
    ok, msg = await model_listing.verify_provider("not-a-provider", s)
    assert ok is False
    assert "unknown provider" in msg.lower() or "not-a-provider" in msg


@pytest.mark.asyncio
async def test_verify_provider_swallows_exceptions(monkeypatch):
    """An adapter that raises is converted to (False, type+message)."""

    async def boom(name, settings):
        raise ConnectionError("server gone")

    monkeypatch.setattr(model_listing, "_verify_uncached", boom)

    s = Settings()
    ok, msg = await model_listing.verify_provider("openai", s)
    assert ok is False
    assert "ConnectionError" in msg or "server gone" in msg


def test_clear_verified_cache_drops_provider():
    model_listing._VERIFIED_CACHE["openai"] = (True, "")
    model_listing._VERIFIED_CACHE["anthropic"] = (True, "")
    model_listing.clear_verified_cache("openai")
    assert "openai" not in model_listing._VERIFIED_CACHE
    assert "anthropic" in model_listing._VERIFIED_CACHE


def test_clear_verified_cache_drops_all():
    model_listing._VERIFIED_CACHE["openai"] = (True, "")
    model_listing._VERIFIED_CACHE["anthropic"] = (False, "auth")
    model_listing.clear_verified_cache()
    assert model_listing._VERIFIED_CACHE == {}
