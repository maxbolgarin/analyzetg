"""Coverage for the OpenAI adapter's `temperature`-omission rule.

OpenAI's reasoning-class models (gpt-5 family, o-series, including the
mini / nano variants) reject any ``temperature != 1`` with an HTTP 400.
The adapter must drop ``temperature`` from the request kwargs for those
models. Non-reasoning models (gpt-4o, gpt-4o-mini, etc.) keep the
configured value.

The classification is driven by :class:`unread.ai.models.ModelInfo.reasoning`
with a name-shape heuristic fallback. Both paths are exercised here.
"""

from __future__ import annotations

from typing import Any

import pytest

from unread.ai.openai_provider import OpenAIProvider
from unread.config import Settings


class _CapturedRequest:
    """Records every kwargs dict passed to `chat.completions.create`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


class _FakeChoice:
    def __init__(self) -> None:
        self.message = type("M", (), {"content": "ok"})()
        self.finish_reason = "stop"


class _FakePromptDetails:
    cached_tokens = 0


class _FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50
    prompt_tokens_details = _FakePromptDetails()


class _FakeResponse:
    def __init__(self) -> None:
        self.choices = [_FakeChoice()]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, captured: _CapturedRequest) -> None:
        self._captured = captured

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self._captured.calls.append(kwargs)
        return _FakeResponse()


class _FakeChat:
    def __init__(self, captured: _CapturedRequest) -> None:
        self.completions = _FakeCompletions(captured)


class _FakeAsyncOpenAI:
    def __init__(self, captured: _CapturedRequest) -> None:
        self.chat = _FakeChat(captured)


def _settings_with_openai_key() -> Settings:
    s = Settings()
    s.ai.provider = "openai"
    s.openai.api_key = "sk-fake"
    return s


@pytest.mark.parametrize(
    "model",
    [
        # Catalog entries with `reasoning=True`.
        "gpt-5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.5",
        # OpenRouter alias still routes to the underlying reasoning endpoint.
        "openai/gpt-5.4-mini",
        # Pure heuristic fallback (not in the catalog).
        "o3-mini",
        "o4-mini",
    ],
)
async def test_temperature_omitted_for_reasoning_models(model: str) -> None:
    """Reasoning models receive the request without a `temperature` key."""
    captured = _CapturedRequest()
    s = _settings_with_openai_key()
    provider = OpenAIProvider(s)
    provider._client = _FakeAsyncOpenAI(captured)  # type: ignore[attr-defined]

    await provider.chat(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=200,
        temperature=0.2,
    )
    assert "temperature" not in captured.calls[0], (
        f"{model}: temperature should be omitted for reasoning models"
    )


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini"])
async def test_temperature_forwarded_for_non_reasoning_models(model: str) -> None:
    """Non-reasoning models still get the configured temperature."""
    captured = _CapturedRequest()
    s = _settings_with_openai_key()
    provider = OpenAIProvider(s)
    provider._client = _FakeAsyncOpenAI(captured)  # type: ignore[attr-defined]

    await provider.chat(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=200,
        temperature=0.2,
    )
    assert captured.calls[0].get("temperature") == 0.2, (
        f"{model}: temperature should pass through unchanged for non-reasoning models"
    )
