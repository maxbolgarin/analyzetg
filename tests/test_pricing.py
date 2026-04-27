"""Regression tests for `atg.util.pricing`: chat_cost / audio_cost math.

These cost numbers drive `atg stats` and the usage_log audit trail, so an
off-by-one-million here is expensive to miss. We build a `Settings` object
by hand to avoid depending on the repo's config.toml.
"""

from __future__ import annotations

import pytest

from atg.config import ChatPricing, PricingCfg, Settings
from atg.util.pricing import audio_cost, chat_cost


@pytest.fixture
def pricing_settings() -> Settings:
    s = Settings()
    s.pricing = PricingCfg(
        chat={
            "gpt-5.4": ChatPricing(input=2.50, cached_input=0.25, output=15.00),
            "gpt-5.4-nano": ChatPricing(input=0.20, cached_input=0.02, output=1.25),
        },
        audio={"whisper-1": 0.006, "gpt-4o-mini-transcribe": 0.003},
    )
    return s


def test_chat_cost_basic_math(pricing_settings: Settings) -> None:
    # 1M fresh input tokens * $2.50 + 500k output * $15 = 2.50 + 7.50 = 10.00
    cost = chat_cost(
        "gpt-5.4",
        prompt_tokens=1_000_000,
        cached_tokens=0,
        completion_tokens=500_000,
        settings=pricing_settings,
    )
    assert cost == pytest.approx(10.00)


def test_chat_cost_cached_is_cheaper(pricing_settings: Settings) -> None:
    # OpenAI reports prompt_tokens as TOTAL (incl. cached). chat_cost subtracts
    # cached from prompt before charging at the fresh rate.
    # 400k cached * $0.25 + 600k fresh * $2.50 + 0 output = 0.10 + 1.50 = 1.60
    cost = chat_cost(
        "gpt-5.4",
        prompt_tokens=1_000_000,
        cached_tokens=400_000,
        completion_tokens=0,
        settings=pricing_settings,
    )
    assert cost == pytest.approx(1.60)


def test_chat_cost_zero_tokens(pricing_settings: Settings) -> None:
    assert chat_cost("gpt-5.4", 0, 0, 0, settings=pricing_settings) == 0.0


def test_chat_cost_handles_none_tokens(pricing_settings: Settings) -> None:
    # None is treated as 0 — the OpenAI client sometimes omits fields.
    assert chat_cost("gpt-5.4", None, None, None, settings=pricing_settings) == 0.0


def test_chat_cost_unknown_model_returns_none(pricing_settings: Settings) -> None:
    # Unknown model must not crash — stats just shows blank cost for that row.
    assert chat_cost("gpt-imaginary", 100, 0, 100, settings=pricing_settings) is None


def test_chat_cost_cached_exceeds_prompt_clamps_to_zero(pricing_settings: Settings) -> None:
    # Defensive: if cached_tokens > prompt_tokens (shouldn't happen but),
    # the "fresh" bucket clamps at 0 rather than going negative.
    cost = chat_cost(
        "gpt-5.4",
        prompt_tokens=100,
        cached_tokens=1000,
        completion_tokens=0,
        settings=pricing_settings,
    )
    # 1000 cached * $0.25 / 1M = 0.00025
    assert cost == pytest.approx(0.00025)


def test_audio_cost_per_minute(pricing_settings: Settings) -> None:
    # 60 s @ $0.006/min = $0.006
    assert audio_cost("whisper-1", 60, settings=pricing_settings) == pytest.approx(0.006)
    # 30 s = half that
    assert audio_cost("whisper-1", 30, settings=pricing_settings) == pytest.approx(0.003)


def test_audio_cost_unknown_model_returns_none(pricing_settings: Settings) -> None:
    assert audio_cost("whisper-imaginary", 120, settings=pricing_settings) is None


def test_audio_cost_zero_seconds(pricing_settings: Settings) -> None:
    assert audio_cost("whisper-1", 0, settings=pricing_settings) == 0.0
    assert audio_cost("whisper-1", None, settings=pricing_settings) == 0.0
