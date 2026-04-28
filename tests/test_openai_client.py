"""Tests for `unread.analyzer.openai_client`.

Covers regressions in:
- `build_messages` ordering (prompt-caching hygiene: system → static → dynamic)
- `chat_complete` automatic retry on the provider's `truncated` flag with
  doubled `max_tokens`, capped at `_MAX_RETRY_TOKENS`.
- Truncation flag propagation (used to skip the analysis cache).

We stub the active provider (a `ChatProvider`) so no real network calls
are made. The orchestrator behavior under test is provider-agnostic —
swapping any of the five real adapters here would yield the same result.
"""

from __future__ import annotations

from typing import Any

import pytest

from unread.ai import ChatResult
from unread.analyzer import openai_client
from unread.analyzer.openai_client import build_messages, chat_complete

# --- build_messages -----------------------------------------------------


def test_build_messages_order_system_static_dynamic() -> None:
    msgs = build_messages("SYS", "STATIC", "DYN")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "SYS"
    assert msgs[1]["role"] == "user"
    # Static context must precede dynamic messages — required for prompt
    # caching to hit (the stable prefix must come first).
    content = msgs[1]["content"]
    assert content.index("STATIC") < content.index("DYN")


def test_build_messages_strips_outer_whitespace() -> None:
    msgs = build_messages("sys", "  static  \n", "\n  dynamic")
    assert msgs[1]["content"].startswith("static")
    assert msgs[1]["content"].endswith("dynamic")


# --- chat_complete retry on truncation ----------------------------------


class _FakeRepo:
    """Minimal repo stub — chat_complete only calls `log_usage`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log_usage(self, **kw: Any) -> None:
        self.calls.append(kw)


class _FakeProvider:
    """ChatProvider stand-in that hands out scripted `ChatResult`s.

    Tracks every call's `max_tokens` so the retry assertions can
    confirm the doubling / clamping behavior.
    """

    name = "fake"
    default_chat_model = "fake-chat"
    default_filter_model = "fake-filter"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        self.calls.append({"model": model, "max_tokens": max_tokens, "temperature": temperature})
        if not self._results:
            raise AssertionError("FakeProvider ran out of scripted results")
        return self._results.pop(0)


def _mk_result(text: str, truncated: bool, prompt: int = 100, completion: int = 50) -> ChatResult:
    return ChatResult(
        text=text,
        prompt_tokens=prompt,
        cached_tokens=0,
        completion_tokens=completion,
        cost_usd=None,
        truncated=truncated,
    )


async def test_chat_complete_no_retry_when_finish_stop() -> None:
    repo = _FakeRepo()
    provider = _FakeProvider([_mk_result("all good", truncated=False)])

    res = await chat_complete(
        provider,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    assert res.text == "all good"
    assert res.truncated is False
    # Exactly one provider call, exactly one usage log entry — no retry.
    assert len(provider.calls) == 1
    assert len(repo.calls) == 1


async def test_chat_complete_retries_once_on_length() -> None:
    """First call truncates; retry with doubled budget succeeds."""
    repo = _FakeRepo()
    provider = _FakeProvider(
        [
            _mk_result("partial…", truncated=True),
            _mk_result("full response", truncated=False),
        ]
    )

    res = await chat_complete(
        provider,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    # First call used 1000, retry doubled to 2000.
    assert [c["max_tokens"] for c in provider.calls] == [1000, 2000]
    assert res.text == "full response"
    assert res.truncated is False
    # Both calls logged.
    assert len(repo.calls) == 2
    # Retry call has context marker so usage_log can distinguish them.
    assert repo.calls[1]["context"].get("retry_of_truncated") is True


async def test_chat_complete_retry_also_truncates_surfaces_flag() -> None:
    """If retry ALSO truncates, result still carries truncated=True."""
    repo = _FakeRepo()
    provider = _FakeProvider(
        [
            _mk_result("still cut off", truncated=True),
            _mk_result("still cut off", truncated=True),
        ]
    )

    res = await chat_complete(
        provider,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    assert res.truncated is True
    assert len(provider.calls) == 2
    assert len(repo.calls) == 2


async def test_chat_complete_no_retry_when_already_at_cap() -> None:
    """At the retry ceiling we don't re-call — avoids infinite loop / waste."""
    repo = _FakeRepo()
    provider = _FakeProvider([_mk_result("partial", truncated=True)])

    res = await chat_complete(
        provider,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=openai_client._MAX_RETRY_TOKENS,  # already at ceiling
    )
    assert len(provider.calls) == 1  # no retry
    assert res.truncated is True


async def test_chat_complete_retry_caps_at_max() -> None:
    """Doubled budget is clamped to `_MAX_RETRY_TOKENS`, not doubled past it."""
    repo = _FakeRepo()
    provider = _FakeProvider(
        [
            _mk_result("partial", truncated=True),
            _mk_result("done", truncated=False),
        ]
    )

    # Start just below the cap so doubling would exceed it.
    below_cap = openai_client._MAX_RETRY_TOKENS - 1000
    await chat_complete(
        provider,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=below_cap,
    )
    seen = [c["max_tokens"] for c in provider.calls]
    assert seen[0] == below_cap
    # Retry is clamped to _MAX_RETRY_TOKENS (not below_cap * 2).
    assert seen[1] == openai_client._MAX_RETRY_TOKENS


# --- regression: usage log includes provider name ----------------------


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "google"])
async def test_chat_complete_logs_provider_name(provider_name: str) -> None:
    """Multi-provider installs need usage rows tagged with the active provider."""
    repo = _FakeRepo()
    provider = _FakeProvider([_mk_result("ok", truncated=False)])
    provider.name = provider_name  # overwrite the default "fake"

    await chat_complete(
        provider,
        repo=repo,
        model="m",
        messages=build_messages("s", "s", "d"),
        max_tokens=100,
    )
    assert repo.calls[0]["context"]["provider"] == provider_name
