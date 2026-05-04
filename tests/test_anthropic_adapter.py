"""Real-translation tests for the Anthropic provider adapter.

Pre-prod gap: `tests/test_ai_providers.py` only checked dispatch
(`make_chat_provider("anthropic")` → AnthropicProvider). The actual
translation logic — `_split_system_and_messages`, the `messages.create`
kwargs, finish-reason mapping, usage parsing, the SDK-retry-disabled +
own-loop retry — was dead-untested.

These cases stub `anthropic.AsyncAnthropic.messages.create` and assert
on the captured kwargs / returned ChatResult.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unread.ai.anthropic_provider import AnthropicProvider, _split_system_and_messages
from unread.config import Settings

# ---- _split_system_and_messages ------------------------------------------


def test_split_system_extracts_single_system_message():
    sys, rest = _split_system_and_messages(
        [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == "you are helpful"
    assert rest == [{"role": "user", "content": "hi"}]


def test_split_system_concatenates_multiple_system_chunks():
    """Defensive: handle the rare case where two system messages reach the adapter."""
    sys, rest = _split_system_and_messages(
        [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == "first\n\nsecond"
    assert rest == [{"role": "user", "content": "hi"}]


def test_split_system_returns_empty_string_when_no_system():
    sys, rest = _split_system_and_messages([{"role": "user", "content": "hi"}])
    assert sys == ""
    assert rest == [{"role": "user", "content": "hi"}]


def test_split_system_drops_empty_chunks():
    """Empty system content shouldn't slip through as `\\n\\n`."""
    sys, _ = _split_system_and_messages(
        [
            {"role": "system", "content": ""},
            {"role": "system", "content": "real one"},
        ]
    )
    assert sys == "real one"


# ---- adapter chat() round-trip --------------------------------------------


def _settings() -> Settings:
    s = Settings()
    s.ai.provider = "anthropic"
    s.anthropic.api_key = "sk-ant-fake"
    return s


class _FakeUsage:
    def __init__(self, in_tok: int = 100, out_tok: int = 50, cached: int = 10) -> None:
        self.input_tokens = in_tok
        self.output_tokens = out_tok
        self.cache_read_input_tokens = cached


class _FakeBlock:
    def __init__(self, kind: str, text: str) -> None:
        self.type = kind
        self.text = text


class _FakeResponse:
    def __init__(
        self,
        *,
        text_blocks: list[str],
        stop_reason: str = "end_turn",
        usage: _FakeUsage | None = None,
    ) -> None:
        self.content = [_FakeBlock("text", t) for t in text_blocks]
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()


class _FakeMessages:
    def __init__(self, response: _FakeResponse, captured: dict) -> None:
        self._response = response
        self._captured = captured

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self._captured.setdefault("calls", []).append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse, captured: dict) -> None:
        self.messages = _FakeMessages(response, captured)


def _provider_with(response: _FakeResponse, captured: dict | None = None) -> tuple[AnthropicProvider, dict]:
    captured = captured if captured is not None else {}
    p = AnthropicProvider(_settings())
    p._client = _FakeClient(response, captured)  # type: ignore[attr-defined]
    return p, captured


async def test_chat_forwards_max_tokens_and_temperature():
    captured: dict = {}
    p, _ = _provider_with(_FakeResponse(text_blocks=["ok"]), captured)
    await p.chat(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        max_tokens=4096,
        temperature=0.2,
    )
    call = captured["calls"][0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 4096
    assert call["temperature"] == 0.2
    assert call["system"] == "you are helpful"
    assert call["messages"] == [{"role": "user", "content": "hi"}]


async def test_chat_fills_empty_messages_with_space_placeholder():
    """Anthropic 400s on empty messages content. The defensive branch
    inserts a single-space user message."""
    captured: dict = {}
    p, _ = _provider_with(_FakeResponse(text_blocks=["ok"]), captured)
    await p.chat(
        model="claude-haiku-4-5",
        messages=[{"role": "system", "content": "sys"}],  # no user message
        max_tokens=64,
        temperature=0.0,
    )
    msgs = captured["calls"][0]["messages"]
    assert msgs == [{"role": "user", "content": " "}]


async def test_chat_concatenates_multiple_text_blocks():
    """Anthropic returns content as a list of blocks; the adapter
    concatenates the text-typed ones into a single string."""
    p, _ = _provider_with(_FakeResponse(text_blocks=["part one ", "part two"]))
    result = await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.text == "part one part two"


async def test_chat_finish_reason_max_tokens_marks_truncated():
    """`stop_reason == "max_tokens"` becomes `truncated=True` in the unified
    ChatResult so the orchestrator can route it through the truncation-retry
    path."""
    p, _ = _provider_with(_FakeResponse(text_blocks=["x"], stop_reason="max_tokens"))
    result = await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.truncated is True


async def test_chat_finish_reason_end_turn_is_not_truncated():
    p, _ = _provider_with(_FakeResponse(text_blocks=["x"], stop_reason="end_turn"))
    result = await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.truncated is False


async def test_chat_parses_usage_including_cached_tokens():
    p, _ = _provider_with(
        _FakeResponse(
            text_blocks=["ok"],
            usage=_FakeUsage(in_tok=1234, out_tok=567, cached=890),
        )
    )
    result = await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.prompt_tokens == 1234
    assert result.completion_tokens == 567
    assert result.cached_tokens == 890


async def test_adapter_disables_sdk_retries():
    """`max_retries=0` on AsyncAnthropic — we own the retry loop so the
    user sees a yellow status line instead of silent SDK retries."""
    p = AnthropicProvider(_settings())
    # Inspecting the real client we built in __init__ — the SDK
    # exposes the value as `max_retries` on the instance.
    assert getattr(p._client, "max_retries", None) == 0  # type: ignore[attr-defined]


async def test_chat_retries_on_connection_error_then_succeeds(monkeypatch):
    """APIConnectionError on the first attempt → adapter retries with
    backoff; second attempt returns successfully."""
    from anthropic import APIConnectionError

    response_after_retry = _FakeResponse(text_blocks=["finally ok"])
    captured: dict = {"attempts": 0}

    class _RetryingMessages:
        async def create(self, **_kw):
            captured["attempts"] += 1
            if captured["attempts"] == 1:
                # APIConnectionError takes a `request` kwarg only —
                # easier to construct than RateLimitError which
                # demands a full httpx response.
                raise APIConnectionError(message="connection refused", request=None)
            return response_after_retry

    p = AnthropicProvider(_settings())
    p._client = SimpleNamespace(messages=_RetryingMessages())  # type: ignore[attr-defined]

    # Pin sleep so the test doesn't actually wait 1.5s.
    sleeps: list[float] = []

    async def _no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    result = await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.text == "finally ok"
    assert captured["attempts"] == 2
    assert sleeps  # we asked sleep at least once


async def test_chat_propagates_non_retryable_errors():
    """A generic ValueError (stand-in for any non-retryable provider
    error) propagates instead of being silently retried."""

    class _Boom:
        async def create(self, **_kw):
            raise ValueError("simulated programmer error")

    p = AnthropicProvider(_settings())
    p._client = SimpleNamespace(messages=_Boom())  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="simulated programmer error"):
        await p.chat(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            temperature=0.0,
        )
