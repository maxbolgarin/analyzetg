"""Coverage for the Google adapter's safety-blocked path.

Gemini sets ``finish_reason`` to ``SAFETY`` / ``RECITATION`` / ``OTHER``
when it refuses to emit content; in those cases the genai SDK *raises*
``ValueError`` on ``resp.text`` rather than returning empty. We convert
that bare exception into a typed
:class:`unread.ai.providers.ProviderSafetyBlockedError` carrying the
structured reason + safety_ratings so the orchestrator can render a
useful status instead of a generic crash.

Safety blocks aren't transient — the orchestrator must NOT retry on
this. The orchestrator-side test for the "no retry on safety" guarantee
lives in `test_openai_client.py`.
"""

from __future__ import annotations

import pytest

from unread.ai import ProviderSafetyBlockedError
from unread.ai.google_provider import GoogleProvider
from unread.config import Settings


class _FakeSafetyRating:
    """Mimics google.genai.types.SafetyRating shape."""

    def __init__(self, category: str, probability: str) -> None:
        self.category = category
        self.probability = probability


class _FakeCandidate:
    def __init__(self, finish_reason: str, ratings: list[_FakeSafetyRating]) -> None:
        self.finish_reason = finish_reason
        self.safety_ratings = ratings


class _FakeUsageMetadata:
    prompt_token_count = 100
    candidates_token_count = 0
    cached_content_token_count = 0


class _SafetyBlockedResponse:
    """Shape of a genai response after a safety refusal.

    `resp.text` raises ValueError("blocked"); `candidates[0].finish_reason`
    is ``SAFETY``; `safety_ratings` carries the per-category probability.
    """

    def __init__(self) -> None:
        self.candidates = [
            _FakeCandidate(
                finish_reason="SAFETY",
                ratings=[
                    _FakeSafetyRating("HARM_CATEGORY_HARASSMENT", "HIGH"),
                    _FakeSafetyRating("HARM_CATEGORY_DANGEROUS", "MEDIUM"),
                ],
            )
        ]
        self.usage_metadata = _FakeUsageMetadata()

    @property
    def text(self) -> str:
        raise ValueError("blocked")


class _FakeGenaiModels:
    """Stub for `genai.Client.aio.models` exposing `generate_content`."""

    def __init__(self, response) -> None:  # type: ignore[no-untyped-def]
        self._response = response

    async def generate_content(self, **_kw):  # type: ignore[no-untyped-def]
        return self._response


class _FakeGenaiAio:
    def __init__(self, response) -> None:  # type: ignore[no-untyped-def]
        self.models = _FakeGenaiModels(response)


class _FakeGenaiClient:
    def __init__(self, response) -> None:  # type: ignore[no-untyped-def]
        self.aio = _FakeGenaiAio(response)


def _build_settings_with_google_key() -> Settings:
    s = Settings()
    s.ai.provider = "google"
    s.google.api_key = "g-fake"
    return s


async def test_google_safety_block_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GoogleProvider.chat()` raises `ProviderSafetyBlockedError` on a
    safety-blocked response (rather than the bare `ValueError` from the
    SDK or a generic crash from a stripped `assert`)."""
    s = _build_settings_with_google_key()
    provider = GoogleProvider(s)
    # Swap the live SDK client for one that returns the safety-blocked
    # response on the first attempt.
    provider._client = _FakeGenaiClient(_SafetyBlockedResponse())  # type: ignore[attr-defined]

    with pytest.raises(ProviderSafetyBlockedError) as exc_info:
        await provider.chat(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "hi"},
            ],
            max_tokens=1024,
            temperature=0.2,
        )

    err = exc_info.value
    # Structured payload survives the conversion — the orchestrator can
    # render a meaningful status without re-walking the SDK's response.
    assert err.reason == "SAFETY"
    assert err.provider == "google"
    assert err.ratings  # non-empty
    cats = {c for c, _ in err.ratings}
    assert "HARM_CATEGORY_HARASSMENT" in cats
