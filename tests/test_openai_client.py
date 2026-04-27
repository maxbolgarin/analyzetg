"""Tests for `atg.analyzer.openai_client`.

Covers regressions in:
- `build_messages` ordering (prompt-caching hygiene: system → static → dynamic)
- `chat_complete` automatic retry on `finish_reason == "length"` with
  doubled `max_tokens`, capped at `_MAX_RETRY_TOKENS`.
- Truncation flag propagation (used to skip the analysis cache).

We stub out `_completion` so no real OpenAI calls are made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from atg.analyzer import openai_client
from atg.analyzer.openai_client import build_messages, chat_complete

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


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def prompt_tokens_details(self) -> Any:
        class _D:
            cached_tokens = 0

        return _D()


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str


@dataclass
class _FakeResp:
    choices: list[_FakeChoice]
    usage: _FakeUsage


class _FakeRepo:
    """Minimal repo stub — chat_complete only calls `log_usage`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log_usage(self, **kw: Any) -> None:
        self.calls.append(kw)


def _mk_resp(text: str, finish_reason: str, prompt: int = 100, completion: int = 50) -> _FakeResp:
    return _FakeResp(
        choices=[_FakeChoice(_FakeMessage(text), finish_reason)],
        usage=_FakeUsage(prompt_tokens=prompt, completion_tokens=completion),
    )


async def test_chat_complete_no_retry_when_finish_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()

    async def fake_completion(oai, model, messages, max_tokens, temperature):
        return _mk_resp("all good", "stop")

    monkeypatch.setattr(openai_client, "_completion", fake_completion)
    res = await chat_complete(
        oai=None,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    assert res.text == "all good"
    assert res.truncated is False
    # Exactly one usage log entry — no retry.
    assert len(repo.calls) == 1


async def test_chat_complete_retries_once_on_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call truncates; retry with doubled budget succeeds."""
    repo = _FakeRepo()
    seen_max_tokens: list[int] = []

    async def fake_completion(oai, model, messages, max_tokens, temperature):
        seen_max_tokens.append(max_tokens)
        if len(seen_max_tokens) == 1:
            return _mk_resp("partial…", "length")  # truncated
        return _mk_resp("full response", "stop")

    monkeypatch.setattr(openai_client, "_completion", fake_completion)
    res = await chat_complete(
        oai=None,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    # First call used 1000, retry doubled to 2000.
    assert seen_max_tokens == [1000, 2000]
    assert res.text == "full response"
    assert res.truncated is False
    # Both calls logged.
    assert len(repo.calls) == 2
    # Retry call has context marker so usage_log can distinguish them.
    assert repo.calls[1]["context"].get("retry_of_truncated") is True


async def test_chat_complete_retry_also_truncates_surfaces_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """If retry ALSO truncates, result still carries truncated=True."""
    repo = _FakeRepo()

    async def fake_completion(oai, model, messages, max_tokens, temperature):
        return _mk_resp("still cut off", "length")

    monkeypatch.setattr(openai_client, "_completion", fake_completion)
    res = await chat_complete(
        oai=None,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=1000,
    )
    assert res.truncated is True
    # Retried once → two calls.
    assert len(repo.calls) == 2


async def test_chat_complete_no_retry_when_already_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the retry ceiling we don't re-call — avoids infinite loop / waste."""
    repo = _FakeRepo()
    calls = 0

    async def fake_completion(oai, model, messages, max_tokens, temperature):
        nonlocal calls
        calls += 1
        return _mk_resp("partial", "length")

    monkeypatch.setattr(openai_client, "_completion", fake_completion)
    res = await chat_complete(
        oai=None,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=openai_client._MAX_RETRY_TOKENS,  # already at ceiling
    )
    assert calls == 1  # no retry
    assert res.truncated is True


async def test_chat_complete_retry_caps_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doubled budget is clamped to `_MAX_RETRY_TOKENS`, not doubled past it."""
    repo = _FakeRepo()
    seen: list[int] = []

    async def fake_completion(oai, model, messages, max_tokens, temperature):
        seen.append(max_tokens)
        if len(seen) == 1:
            return _mk_resp("partial", "length")
        return _mk_resp("done", "stop")

    monkeypatch.setattr(openai_client, "_completion", fake_completion)
    # Start just below the cap so doubling would exceed it.
    below_cap = openai_client._MAX_RETRY_TOKENS - 1000
    await chat_complete(
        oai=None,
        repo=repo,
        model="gpt-5.4",
        messages=build_messages("s", "s", "d"),
        max_tokens=below_cap,
    )
    assert seen[0] == below_cap
    # Retry is clamped to _MAX_RETRY_TOKENS (not below_cap * 2).
    assert seen[1] == openai_client._MAX_RETRY_TOKENS
