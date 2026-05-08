"""Live model-list fetching from each provider's `/models` endpoint.

The hardcoded catalog in :mod:`unread.ai.models` is curated and ages
fast (every release of every provider adds models). This module hits
the upstream API for whoever has a key configured, classifies the
returned IDs by role using lightweight name patterns, and returns a
fresh list of model strings.

Per-process cache so a single `unread settings` session doesn't burn
five API calls when the user steps through four slots; the settings
UI exposes a "🔄 Reload from API" row that calls
:func:`clear_cache` to force a refresh.

All fetchers degrade silently to an empty list on error (no key, SDK
not installed, network failure). Caller falls back to the curated
catalog so the picker always shows something.
"""

from __future__ import annotations

from unread.util.logging import get_logger

log = get_logger(__name__)


# Per-process cache. Key: (provider, role). Value: list of model IDs.
# Module-scoped so a fresh `unread settings` invocation starts empty
# (sufficient freshness for an interactive session); the explicit
# reload action drops entries on demand.
_FETCHED_CACHE: dict[tuple[str, str], list[str]] = {}


def clear_cache(provider: str = "", role: str = "") -> None:
    """Drop fetched-list cache entries.

    `clear_cache()` empties the whole cache; `clear_cache("openai")`
    drops every role for one provider; `clear_cache("openai", "audio")`
    drops one specific (provider, role) entry. The settings UI calls
    this from its "Reload from API" action so the next picker render
    re-fetches.
    """
    if not provider:
        _FETCHED_CACHE.clear()
        return
    keys_to_drop = [(p, r) for (p, r) in _FETCHED_CACHE if p == provider and (not role or r == role)]
    for k in keys_to_drop:
        _FETCHED_CACHE.pop(k, None)


def is_cached(provider: str, role: str) -> bool:
    """True iff a previous `fetch_models` call populated this slot.

    Lets the UI render a different label ("Reload" vs. "Fetch") so the
    user knows whether they're hitting a fresh endpoint or warming
    the cache.
    """
    return (provider, role) in _FETCHED_CACHE


# ----------------------------- Role classification ---------------------------


def _is_audio(model_id: str) -> bool:
    """Audio (Whisper-shape) is the only role we filter strictly.

    A non-audio id slipped into the audio picker would 4xx at call
    time, so we constrain by name pattern: `whisper`, `transcribe`,
    or `tts` in the bare id.
    """
    name = model_id.rsplit("/", 1)[-1].lower()
    return any(token in name for token in ("whisper", "transcribe"))


def _is_embedding(model_id: str) -> bool:
    name = model_id.rsplit("/", 1)[-1].lower()
    return "embedding" in name or name.startswith("text-embedding")


# ----------------------------- Per-provider fetchers -------------------------


async def _fetch_openai_compat(provider: str, settings) -> list[str]:  # type: ignore[no-untyped-def]
    """Fetch models via the OpenAI-shape `/v1/models` endpoint.

    Covers openai, openrouter, and local — all three speak the same
    SDK shape. Returns the raw model ID list; role filtering happens
    at the caller. Empty list on any failure.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return []

    if provider == "openai":
        if not settings.openai.api_key:
            return []
        kwargs = {"api_key": settings.openai.api_key, "timeout": settings.openai.request_timeout_sec}
        if settings.ai.base_url:
            kwargs["base_url"] = settings.ai.base_url
    elif provider == "openrouter":
        if not settings.openrouter.api_key:
            return []
        from unread.ai.openai_provider import OPENROUTER_APP_HEADERS

        kwargs = {
            "api_key": settings.openrouter.api_key,
            "base_url": settings.ai.base_url or settings.openrouter.base_url,
            "timeout": settings.openai.request_timeout_sec,
            "default_headers": OPENROUTER_APP_HEADERS,
        }
    elif provider == "local":
        kwargs = {
            "api_key": settings.local.api_key or "local-no-key",
            "base_url": settings.ai.base_url or settings.local.base_url,
            "timeout": settings.openai.request_timeout_sec,
        }
    else:
        return []

    try:
        client = AsyncOpenAI(**kwargs)
        ids: list[str] = []
        async for model in client.models.list():
            mid = getattr(model, "id", "") or ""
            if mid:
                ids.append(mid)
        return ids
    except Exception as e:
        log.debug("model_listing.fetch_failed", provider=provider, err=str(e)[:200])
        return []


async def _fetch_anthropic(settings) -> list[str]:  # type: ignore[no-untyped-def]
    """Fetch models via Anthropic SDK's `models.list`."""
    if not settings.anthropic.api_key:
        return []
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return []
    try:
        client = AsyncAnthropic(
            api_key=settings.anthropic.api_key,
            timeout=settings.openai.request_timeout_sec,
            max_retries=0,
        )
        page = await client.models.list()
        ids: list[str] = []
        # Anthropic's list returns a paginated object with `.data`. The
        # first page has the recent models; older revisions are on
        # follow-up pages, but those are rarely useful here.
        data = getattr(page, "data", None) or []
        for m in data:
            mid = getattr(m, "id", "") or ""
            if mid:
                ids.append(mid)
        return ids
    except Exception as e:
        log.debug("model_listing.fetch_failed", provider="anthropic", err=str(e)[:200])
        return []


async def _fetch_google(settings) -> list[str]:  # type: ignore[no-untyped-def]
    """Fetch models via google-genai's `aio.models.list`."""
    if not settings.google.api_key:
        return []
    try:
        from google import genai
    except ImportError:
        return []
    try:
        client = genai.Client(api_key=settings.google.api_key)
        ids: list[str] = []
        # `aio.models.list()` returns an async iterator of `Model`
        # objects. Each `.name` is `models/<id>`; strip the prefix
        # so we surface the bare id (matches how Anthropic / OpenAI
        # ids look in the picker).
        async for m in await client.aio.models.list():
            full = getattr(m, "name", "") or ""
            mid = full.split("/", 1)[-1] if full else ""
            # Filter to Gemini chat-class models — `embedding` and
            # `aqa` entries appear too and aren't useful here.
            if mid and ("gemini" in mid.lower() or "imagen" in mid.lower()):
                ids.append(mid)
        return ids
    except Exception as e:
        log.debug("model_listing.fetch_failed", provider="google", err=str(e)[:200])
        return []


# ----------------------------- Public entry point ----------------------------


def _filter_for_role(role: str, ids: list[str]) -> list[str]:
    """Apply the role filter to a raw fetched list.

    `audio` is strict (Whisper-shape patterns only). `chat` / `filter`
    / `vision` are permissive — we drop only embedding entries,
    because the user knows what they want and the picker still
    shows the curated catalog alongside.
    """
    if role == "audio":
        return [m for m in ids if _is_audio(m)]
    return [m for m in ids if not _is_embedding(m) and not _is_audio(m)]


async def fetch_models(provider: str, role: str, settings) -> list[str]:  # type: ignore[no-untyped-def]
    """Return live model IDs for `(provider, role)`. Cached per-process.

    Returns an empty list on any failure; caller falls back to the
    curated catalog. Role filtering is name-pattern based — strict
    for audio, permissive for everything else.
    """
    name = (provider or "").strip().lower()
    cache_key = (name, role)
    if cache_key in _FETCHED_CACHE:
        return _FETCHED_CACHE[cache_key]
    if name in {"openai", "openrouter", "local"}:
        raw = await _fetch_openai_compat(name, settings)
    elif name == "anthropic":
        raw = await _fetch_anthropic(settings)
    elif name == "google":
        raw = await _fetch_google(settings)
    else:
        raw = []
    filtered = _filter_for_role(role, raw)
    # Stable sort on the id so reorderings between fetches don't
    # shuffle the picker rows in confusing ways. The catalog rows
    # always render first in the UI; this is just for the appended
    # "fetched-only" tail.
    filtered.sort()
    _FETCHED_CACHE[cache_key] = filtered
    return filtered


def cached_models(provider: str, role: str) -> list[str]:
    """Synchronous accessor for whatever's already in the cache.

    Used by the picker to render the augmented model list without
    awaiting. Returns an empty list when no `fetch_models` has run.
    """
    return list(_FETCHED_CACHE.get((provider.strip().lower(), role), []))


# ----------------------------- Smoke verification ----------------------------


# Cache of verification results so the settings UI doesn't burn one
# `models.list()` per slot edit when the user picks the same provider
# four times. Keyed by provider; cleared on demand from the UI when
# credentials change.
_VERIFIED_CACHE: dict[str, tuple[bool, str]] = {}


def clear_verified_cache(provider: str = "") -> None:
    """Drop verification-cache entries.

    Called from the settings UI immediately after the user changes a
    key or URL, so the next `verify_provider` call re-tests instead of
    returning a stale "✓ ok" from before the edit.
    """
    if not provider:
        _VERIFIED_CACHE.clear()
        return
    _VERIFIED_CACHE.pop(provider.strip().lower(), None)


async def verify_provider(provider: str, settings) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Smoke-test that `provider` is reachable + authenticated.

    Hits each provider's lightest "free" endpoint — `models.list()` for
    every adapter we have — to verify the SDK can construct, the key
    works, and the network reaches the upstream host. No tokens burned.

    Returns `(ok, message)`:
    - `(True, "")` on success.
    - `(False, reason)` on failure (no key, auth refused, connection
      refused, timeout, SDK not installed, …). Caller renders `reason`
      to the user; UI uses it to decide whether to prompt for a key /
      fix the URL / continue anyway.
    """
    name = (provider or "").strip().lower()
    if name in _VERIFIED_CACHE:
        return _VERIFIED_CACHE[name]
    try:
        result = await _verify_uncached(name, settings)
    except Exception as e:  # pragma: no cover — defensive belt
        result = (False, f"{type(e).__name__}: {str(e)[:200]}")
    _VERIFIED_CACHE[name] = result
    return result


async def _verify_uncached(name: str, settings) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Per-provider smoke test. Returns `(ok, error)`. Never raises."""
    if name in {"openai", "openrouter", "local"}:
        return await _verify_openai_compat(name, settings)
    if name == "anthropic":
        return await _verify_anthropic(settings)
    if name == "google":
        return await _verify_google(settings)
    return False, f"unknown provider: {name!r}"


async def _verify_openai_compat(name: str, settings) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return False, "openai SDK not installed"

    if name == "openai":
        if not settings.openai.api_key:
            return False, "no API key"
        kwargs = {
            "api_key": settings.openai.api_key,
            "timeout": min(10.0, settings.openai.request_timeout_sec),
        }
        if settings.ai.base_url:
            kwargs["base_url"] = settings.ai.base_url
    elif name == "openrouter":
        if not settings.openrouter.api_key:
            return False, "no API key"
        from unread.ai.openai_provider import OPENROUTER_APP_HEADERS

        kwargs = {
            "api_key": settings.openrouter.api_key,
            "base_url": settings.ai.base_url or settings.openrouter.base_url,
            "timeout": min(10.0, settings.openai.request_timeout_sec),
            "default_headers": OPENROUTER_APP_HEADERS,
        }
    else:  # local
        kwargs = {
            "api_key": settings.local.api_key or "local-no-key",
            "base_url": settings.ai.base_url or settings.local.base_url,
            "timeout": min(5.0, settings.openai.request_timeout_sec),
        }

    try:
        client = AsyncOpenAI(**kwargs)
        # `models.list()` returns an async paginator; consuming the
        # first page is enough to validate auth + connectivity. We
        # don't iterate past it — Local servers can have hundreds of
        # entries and we only care that the call succeeded.
        page = await client.models.list()
        # Probe the first page eagerly so a deferred error surfaces
        # here rather than on iteration. `data` is the standard list.
        _ = getattr(page, "data", None)
        return True, ""
    except Exception as e:
        msg = str(e)
        if hasattr(e, "status_code") and getattr(e, "status_code", 0) == 401:
            return False, "auth failed (invalid API key)"
        return False, msg[:200] or type(e).__name__


async def _verify_anthropic(settings) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    if not settings.anthropic.api_key:
        return False, "no API key"
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return False, "anthropic SDK not installed"
    try:
        client = AsyncAnthropic(
            api_key=settings.anthropic.api_key,
            timeout=min(10.0, settings.openai.request_timeout_sec),
            max_retries=0,
        )
        await client.models.list()
        return True, ""
    except Exception as e:
        if hasattr(e, "status_code") and getattr(e, "status_code", 0) == 401:
            return False, "auth failed (invalid API key)"
        return False, str(e)[:200] or type(e).__name__


async def _verify_google(settings) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    if not settings.google.api_key:
        return False, "no API key"
    try:
        from google import genai
    except ImportError:
        return False, "google-genai SDK not installed"
    try:
        client = genai.Client(api_key=settings.google.api_key)
        # `aio.models.list()` returns an async iterator; pulling one
        # entry is enough to confirm auth + connectivity.
        async for _m in await client.aio.models.list():
            break
        return True, ""
    except Exception as e:
        return False, str(e)[:200] or type(e).__name__
