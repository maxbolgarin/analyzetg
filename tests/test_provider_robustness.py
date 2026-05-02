"""Provider-robustness fixes from the pre-prod review.

Covers:
  * `_is_reasoning_model` predicate that drives temperature-omission
    on OpenAI's reasoning-class models (gpt-5 family + o-series).
  * Anthropic's empty-`messages` defensive fallback uses a non-empty
    content block (Anthropic 400s on `""`).
  * `_safe_filename_component` is exercised in test_download_media.py;
    this file pins the AI-side robustness.
"""

from __future__ import annotations

import pytest

from unread.ai.openai_provider import _is_reasoning_model


def test_reasoning_model_predicate_matches_o_series():
    assert _is_reasoning_model("o1")
    assert _is_reasoning_model("o1-mini")
    assert _is_reasoning_model("o3")
    assert _is_reasoning_model("o3-mini")
    assert _is_reasoning_model("o4-mini")


def test_reasoning_model_predicate_matches_gpt5_family():
    # The catalog default (`gpt-5.4-mini`) is a reasoning model — the
    # original bug: temperature was unconditionally forwarded and the
    # model 400'd because reasoning models reject any value != 1.
    assert _is_reasoning_model("gpt-5")
    assert _is_reasoning_model("gpt-5.4")
    assert _is_reasoning_model("gpt-5.4-mini")
    assert _is_reasoning_model("gpt-5.5")


def test_reasoning_model_predicate_handles_openrouter_prefix():
    """OpenRouter ids look like `openai/gpt-5.4`; predicate must match
    after stripping the vendor prefix."""
    assert _is_reasoning_model("openai/gpt-5.4-mini")
    assert _is_reasoning_model("openai/o3-mini")


def test_reasoning_model_predicate_excludes_non_reasoning():
    assert not _is_reasoning_model("gpt-4o")
    assert not _is_reasoning_model("gpt-4o-mini")
    assert not _is_reasoning_model("claude-sonnet-4-6")
    assert not _is_reasoning_model("gemini-2.5-flash")


@pytest.mark.asyncio
async def test_anthropic_empty_messages_uses_non_empty_placeholder():
    """Anthropic 400s if `content` is "". The defensive fallback must
    inject a non-empty placeholder (we use a single space)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from unread.ai.anthropic_provider import AnthropicProvider

    settings = SimpleNamespace(
        anthropic=SimpleNamespace(api_key="sk-ant-test"),
        openai=SimpleNamespace(request_timeout_sec=60, max_retries=3),
    )
    p = AnthropicProvider.__new__(AnthropicProvider)
    p._settings = settings
    fake_msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
        stop_reason="end_turn",
    )
    p._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg)))

    # Pass only a system message — formatter shouldn't ever do this in
    # practice, but the fallback must produce a non-empty user content
    # so Anthropic accepts it.
    await p.chat(
        model="claude-sonnet-4-6",
        messages=[{"role": "system", "content": "S"}],
        max_tokens=100,
        temperature=0.2,
    )
    call_kwargs = p._client.messages.create.await_args.kwargs
    assert call_kwargs["messages"], "Anthropic call must have at least one message"
    for msg in call_kwargs["messages"]:
        assert msg.get("content"), f"empty content would 400: {msg}"
