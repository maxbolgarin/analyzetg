"""Cost estimation for OpenAI chat and audio usage.

Prices live in config.toml; if a model isn't listed we log a warning and
return None (cost) so the rest of the pipeline still completes.
"""

from __future__ import annotations

from analyzetg.config import Settings, get_settings
from analyzetg.util.logging import get_logger

log = get_logger(__name__)


def chat_cost(
    model: str,
    prompt_tokens: int | None,
    cached_tokens: int | None,
    completion_tokens: int | None,
    *,
    settings: Settings | None = None,
) -> float | None:
    s = settings or get_settings()
    row = s.pricing.chat.get(model)
    if row is None:
        log.warning("pricing.chat.unknown_model", model=model)
        return None
    p = int(prompt_tokens or 0)
    c = int(cached_tokens or 0)
    o = int(completion_tokens or 0)
    # Prompt total = (new) input + cached input. OpenAI reports p as total prompt incl cached.
    fresh = max(p - c, 0)
    return round(
        (fresh / 1_000_000) * row.input
        + (c / 1_000_000) * row.cached_input
        + (o / 1_000_000) * row.output,
        6,
    )


def audio_cost(
    model: str, seconds: int | None, *, settings: Settings | None = None
) -> float | None:
    s = settings or get_settings()
    per_min = s.pricing.audio.get(model)
    if per_min is None:
        log.warning("pricing.audio.unknown_model", model=model)
        return None
    sec = int(seconds or 0)
    return round((sec / 60.0) * per_min, 6)
