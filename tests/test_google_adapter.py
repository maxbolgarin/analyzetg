"""Real-translation tests for the Google (genai) provider adapter.

The safety-block path is covered by `test_google_safety.py`. This file
covers the other translation surfaces — message conversion (system
extraction, role mapping, tracked content shape), finish-reason
mapping (`MAX_TOKENS` → truncated), usage parsing including cached
input tokens, and the retry loop on transient errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unread.ai.google_provider import GoogleProvider, _convert_messages
from unread.config import Settings

# ---- _convert_messages ---------------------------------------------------


def test_convert_messages_extracts_single_system():
    sys, contents = _convert_messages(
        [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == "you are helpful"
    assert len(contents) == 1
    assert contents[0].role == "user"
    # The Part text is reachable via .parts[0].text
    assert contents[0].parts[0].text == "hi"


def test_convert_messages_concatenates_multiple_system():
    sys, _ = _convert_messages(
        [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == "first\n\nsecond"


def test_convert_messages_renames_assistant_to_model():
    """Gemini uses `model` instead of `assistant`."""
    _, contents = _convert_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "and then?"},
        ]
    )
    roles = [c.role for c in contents]
    assert roles == ["user", "model", "user"]


def test_convert_messages_skips_empty_system():
    sys, _ = _convert_messages(
        [
            {"role": "system", "content": ""},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == ""


# ---- adapter chat() round-trip --------------------------------------------


def _settings() -> Settings:
    s = Settings()
    s.ai.provider = "google"
    s.google.api_key = "g-fake"
    return s


class _FakeUsageMetadata:
    def __init__(self, prompt: int = 100, completion: int = 50, cached: int = 0) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = completion
        self.cached_content_token_count = cached


class _FakeCandidate:
    def __init__(self, finish_reason: str = "STOP") -> None:
        self.finish_reason = finish_reason
        self.safety_ratings = []


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str,
        finish_reason: str = "STOP",
        usage: _FakeUsageMetadata | None = None,
    ) -> None:
        self._text = text
        self.candidates = [_FakeCandidate(finish_reason=finish_reason)]
        self.usage_metadata = usage or _FakeUsageMetadata()

    @property
    def text(self) -> str:
        return self._text


class _FakeGenaiModels:
    def __init__(self, response: _FakeResponse, captured: dict) -> None:
        self._response = response
        self._captured = captured

    async def generate_content(self, **kwargs: Any) -> _FakeResponse:
        self._captured.setdefault("calls", []).append(kwargs)
        return self._response


def _provider_with(response: _FakeResponse, captured: dict | None = None) -> tuple[GoogleProvider, dict]:
    captured = captured if captured is not None else {}
    p = GoogleProvider(_settings())
    p._client = SimpleNamespace(  # type: ignore[attr-defined]
        aio=SimpleNamespace(models=_FakeGenaiModels(response, captured))
    )
    return p, captured


async def test_chat_round_trip_returns_text_and_usage():
    p, captured = _provider_with(
        _FakeResponse(
            text="hello back",
            usage=_FakeUsageMetadata(prompt=200, completion=80, cached=15),
        )
    )
    result = await p.chat(
        model="gemini-2.5-flash",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "ping"},
        ],
        max_tokens=512,
        temperature=0.1,
    )
    assert result.text == "hello back"
    assert result.prompt_tokens == 200
    assert result.completion_tokens == 80
    assert result.cached_tokens == 15
    assert result.truncated is False  # finish_reason="STOP"
    # Forwarded model + config
    call = captured["calls"][0]
    assert call["model"] == "gemini-2.5-flash"
    cfg = call["config"]
    assert cfg.max_output_tokens == 512
    assert cfg.temperature == 0.1
    assert cfg.system_instruction == "be terse"


async def test_chat_max_tokens_finish_reason_marks_truncated():
    """`finish_reason == "MAX_TOKENS"` → truncated=True so the
    orchestrator can route through the truncation-retry path."""
    p, _ = _provider_with(_FakeResponse(text="cut", finish_reason="MAX_TOKENS"))
    result = await p.chat(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.truncated is True


async def test_chat_handles_attribute_error_on_text_as_empty():
    """A malformed response (no .text accessor) shouldn't crash the
    map-reduce — the adapter treats it as empty content and lets the
    orchestrator decide what to do."""
    bad_resp = SimpleNamespace(
        candidates=[_FakeCandidate(finish_reason="STOP")],
        usage_metadata=_FakeUsageMetadata(),
    )
    # No `text` property — getattr raises AttributeError.
    p, _ = _provider_with(bad_resp)  # type: ignore[arg-type]
    result = await p.chat(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.text == ""


async def test_chat_retries_on_transient_5xx(monkeypatch):
    """A 503 from genai retries with backoff; second attempt succeeds."""
    from google.genai import errors as genai_errors

    captured: dict = {"attempts": 0}
    response_after_retry = _FakeResponse(text="ok now")

    class _RetryingModels:
        async def generate_content(self, **_kw):
            captured["attempts"] += 1
            if captured["attempts"] == 1:
                # APIError accepts (code, response_json, response)
                raise genai_errors.APIError(503, {"error": {"message": "unavailable"}}, None)
            return response_after_retry

    p = GoogleProvider(_settings())
    p._client = SimpleNamespace(  # type: ignore[attr-defined]
        aio=SimpleNamespace(models=_RetryingModels())
    )

    sleeps: list[float] = []

    async def _no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    result = await p.chat(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.0,
    )
    assert result.text == "ok now"
    assert captured["attempts"] == 2
    assert sleeps  # backoff fired


async def test_chat_propagates_4xx_other_than_429():
    """A 400 error is user-actionable — no retry."""
    from google.genai import errors as genai_errors

    class _Boom:
        async def generate_content(self, **_kw):
            raise genai_errors.APIError(400, {"error": {"message": "bad request"}}, None)

    p = GoogleProvider(_settings())
    p._client = SimpleNamespace(  # type: ignore[attr-defined]
        aio=SimpleNamespace(models=_Boom())
    )
    with pytest.raises(genai_errors.APIError):
        await p.chat(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            temperature=0.0,
        )
