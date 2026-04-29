"""Report header rendering.

The header is prepended to every saved report so future-you can tell
what chat / period / model / cost produced the file without digging
through logs. The body of the LLM answer is unchanged.
"""

from __future__ import annotations

from datetime import datetime

from unread.analyzer.commands import (
    _fmt_cost_precise,
    _fmt_period_header,
    _render_report_header,
)
from unread.analyzer.pipeline import AnalysisResult


def _result(**overrides) -> AnalysisResult:
    base: dict = {
        "preset": "summary",
        "model": "gpt-5.4",
        "chat_id": -100_123,
        "thread_id": 0,
        "msg_count": 26,
        "chunk_count": 1,
        "batch_hashes": ["deadbeef"],
        "final_result": "ignored",
        "total_cost_usd": 0.0383,
        "cache_hits": 0,
        "cache_misses": 1,
        "prompt_version": "v1",
        "filter_model": "gpt-5.4-nano",
        "period": (None, None),
        "enrich_kinds": ["voice", "videonote"],
        "enrich_cost_usd": 0.0167,
        "enrich_summary": "voice: 4; videonote: 2 — $0.0167",
        "raw_msg_count": 31,
    }
    base.update(overrides)
    return AnalysisResult(**base)


def test_header_has_core_fields():
    h = _render_report_header(_result(), title="UNION 3.0 | WORK GROUP")
    assert "**Chat:** UNION 3.0 | WORK GROUP" in h
    assert "**Preset:** `summary` (v=v1)" in h
    assert "**Model:** `gpt-5.4`" in h
    assert "**Messages analyzed:** 26" in h
    assert "from 31 raw" in h  # loss accounting


def test_header_combines_both_costs():
    h = _render_report_header(_result(), title="x")
    # Total + split.
    assert "**Cost:**" in h
    assert "analysis" in h and "enrichment" in h
    # Totals up to analysis + enrich (0.0383 + 0.0167 = 0.055).
    assert "$0.055" in h or "$0.0550" in h


def test_header_no_enrichment_shows_single_cost():
    h = _render_report_header(
        _result(enrich_kinds=[], enrich_cost_usd=0.0, enrich_summary=""),
        title="x",
    )
    assert "analysis" not in h.split("**Cost:**")[1].split("\n")[0]


def test_header_omits_thread_when_zero():
    h = _render_report_header(_result(thread_id=0), title="x")
    assert "**Thread:**" not in h


def test_header_includes_thread_when_set():
    h = _render_report_header(_result(thread_id=2), title="x")
    assert "**Thread:** 2" in h


def test_header_shows_map_model_only_with_reduce():
    # Single-chunk run — map model shouldn't appear since there's no map phase.
    h = _render_report_header(_result(chunk_count=1, filter_model="gpt-5.4-nano"), title="x")
    assert "for map phase" not in h

    # Multi-chunk → map model is noted.
    h2 = _render_report_header(_result(chunk_count=4), title="x")
    assert "+ `gpt-5.4-nano` for map phase" in h2


def test_header_labels_unread_when_period_is_none():
    h = _render_report_header(_result(period=(None, None)), title="x")
    assert "unread / full history" in h


def test_header_renders_concrete_period():
    h = _render_report_header(
        _result(period=(datetime(2026, 4, 20, 9, 0), datetime(2026, 4, 24, 11, 30))),
        title="x",
    )
    assert "2026-04-20 09:00" in h and "2026-04-24 11:30" in h


def test_header_ends_with_blank_line_before_body():
    # _print_and_write concatenates header + body, so the blank line is
    # load-bearing — Markdown collapses adjacent content otherwise.
    h = _render_report_header(_result(), title="x")
    assert h.endswith("\n")


def test_fmt_cost_precise_scales_precision():
    assert _fmt_cost_precise(0.0) == "$0"
    assert _fmt_cost_precise(0.0005) == "< $0.001"
    assert _fmt_cost_precise(0.0045) == "$0.0045"
    assert _fmt_cost_precise(0.023) == "$0.023"
    assert _fmt_cost_precise(1.5) == "$1.50"


def test_fmt_period_none():
    assert "no date filter" in _fmt_period_header(None)
    assert "no date filter" in _fmt_period_header((None, None))


def test_fmt_period_concrete():
    got = _fmt_period_header((datetime(2026, 4, 20), datetime(2026, 4, 24)))
    assert "2026-04-20" in got and "2026-04-24" in got


def test_breakdown_omitted_for_text_only_run():
    # No media, no links → no Breakdown line. Showing "Breakdown: text 5"
    # alone would be noise — the message count above already says so.
    h = _render_report_header(
        _result(media_counts={"text": 5}, link_count=0),
        title="x",
    )
    assert "Breakdown" not in h


def test_breakdown_present_when_media_or_links_exist():
    h = _render_report_header(
        _result(media_counts={"text": 3, "voice": 2, "photo": 1}, link_count=4),
        title="x",
    )
    assert "**Breakdown:**" in h
    assert "text 3" in h and "voice 2" in h and "photo 1" in h
    assert "4 with links" in h


def test_breakdown_only_links_no_media():
    # A pure-text chat where some messages have URLs still gets the line —
    # link count alone is informative.
    h = _render_report_header(
        _result(media_counts={"text": 5}, link_count=2),
        title="x",
    )
    assert "**Breakdown:**" in h
    assert "2 with links" in h


def test_breakdown_kind_order_stable():
    # Order is text → voice → videonote → video → photo → doc, regardless
    # of dict insertion order — so the header reads consistently.
    h = _render_report_header(
        _result(
            media_counts={"doc": 1, "voice": 2, "text": 3, "photo": 1},
            link_count=0,
        ),
        title="x",
    )
    breakdown = next(line for line in h.splitlines() if "Breakdown" in line)
    assert breakdown.index("text") < breakdown.index("voice")
    assert breakdown.index("voice") < breakdown.index("photo")
    assert breakdown.index("photo") < breakdown.index("doc")
