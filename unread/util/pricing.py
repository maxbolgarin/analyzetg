"""Cost estimation for OpenAI chat and audio usage.

Prices live in config.toml first; for any model missing there we fall
back to the curated catalog in `unread.ai.models` (refreshed 2026-05-01)
so picking a brand-new catalog model from `unread settings` doesn't
require also editing config.toml. A model unknown to both paths logs a
warning and returns None — the pipeline still completes.
"""

from __future__ import annotations

from unread.ai.models import find_model
from unread.config import ChatPricing, Settings, get_settings
from unread.util.logging import get_logger

log = get_logger(__name__)


def chat_pricing_for(model: str, settings: Settings) -> ChatPricing | None:
    """Look up `model` in user pricing, falling back to the curated catalog."""
    row = settings.pricing.chat.get(model)
    if row is not None:
        return row
    info = find_model(model)
    if info is None or info.role == "audio":
        return None
    return ChatPricing(input=info.input_price, cached_input=info.cached_price, output=info.output_price)


def chat_cost(
    model: str,
    prompt_tokens: int | None,
    cached_tokens: int | None,
    completion_tokens: int | None,
    *,
    settings: Settings | None = None,
) -> float | None:
    s = settings or get_settings()
    row = chat_pricing_for(model, s)
    if row is None:
        log.warning("pricing.chat.unknown_model", model=model)
        return None
    p = int(prompt_tokens or 0)
    c = int(cached_tokens or 0)
    o = int(completion_tokens or 0)
    # Prompt total = (new) input + cached input. OpenAI reports p as total prompt incl cached.
    fresh = max(p - c, 0)
    return round(
        (fresh / 1_000_000) * row.input + (c / 1_000_000) * row.cached_input + (o / 1_000_000) * row.output,
        6,
    )


def audio_cost(model: str, seconds: int | None, *, settings: Settings | None = None) -> float | None:
    s = settings or get_settings()
    per_min = s.pricing.audio.get(model)
    if per_min is None:
        info = find_model(model)
        if info is not None and info.role == "audio":
            per_min = info.input_price
    if per_min is None:
        log.warning("pricing.audio.unknown_model", model=model)
        return None
    sec = int(seconds or 0)
    return round((sec / 60.0) * per_min, 6)
