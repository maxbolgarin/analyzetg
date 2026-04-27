"""Regression tests for the truncation-banner path.

When a result hits the model's `output_budget_tokens` limit (finish_reason
== "length"), `_with_truncation_banner` prepends a visible warning to the
saved report so the user notices the partial output instead of thinking
they got a complete analysis.
"""

from __future__ import annotations

from unread.analyzer.commands import _with_truncation_banner
from unread.analyzer.pipeline import AnalysisResult


def _mk_result(text: str, truncated: bool, preset: str = "summary") -> AnalysisResult:
    return AnalysisResult(
        preset=preset,
        model="gpt-5.4",
        chat_id=1,
        thread_id=0,
        msg_count=10,
        chunk_count=1,
        batch_hashes=["a"],
        final_result=text,
        total_cost_usd=0.05,
        cache_hits=0,
        cache_misses=1,
        truncated=truncated,
    )


def test_no_banner_when_not_truncated() -> None:
    r = _mk_result("complete report", truncated=False)
    assert _with_truncation_banner(r) == "complete report"


def test_banner_prepended_when_truncated() -> None:
    r = _mk_result("partial mid-word…", truncated=True)
    out = _with_truncation_banner(r)
    assert out.startswith(">")  # Markdown blockquote warning
    assert "truncated" in out.lower()
    # Original content preserved at the end.
    assert out.endswith("partial mid-word…")


def test_banner_names_the_preset_file_to_edit() -> None:
    r = _mk_result("partial", truncated=True, preset="digest")
    out = _with_truncation_banner(r)
    # Banner must reference the preset file path so the user knows where to
    # go. Per-language presets live under presets/<lang>/<name>.md; the
    # banner uses a placeholder so it works regardless of locale.
    assert "presets/<lang>/digest.md" in out
    assert "output_budget_tokens" in out


def test_banner_tolerates_missing_attribute() -> None:
    """Safety net: if somehow a plain dict/stub is passed in (e.g. from a
    legacy caller), the helper must not crash — it just returns final_result."""

    class _Fake:
        final_result = "fallback text"

    # No `truncated` attr → getattr default False → no banner prepended.
    assert _with_truncation_banner(_Fake()) == "fallback text"
