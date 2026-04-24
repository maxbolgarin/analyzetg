"""Cost formatter for the wizard confirm step.

The old `${v:.2f}` rendered everything under half a cent as `$0.00`,
which told users "this is free" when it wasn't. The new formatter
scales precision to magnitude.
"""

from analyzetg.interactive import _extra_enrich_kinds, _fmt_cost, _fmt_cost_range


def test_fmt_cost_none():
    assert _fmt_cost(None) == "—"


def test_fmt_cost_zero():
    assert _fmt_cost(0.0) == "$0"
    assert _fmt_cost(-0.01) == "$0"


def test_fmt_cost_sub_millicent():
    assert _fmt_cost(0.0005) == "< $0.001"


def test_fmt_cost_sub_cent_shows_four_decimals():
    assert _fmt_cost(0.0045) == "$0.0045"


def test_fmt_cost_sub_dollar_shows_three_decimals():
    assert _fmt_cost(0.023) == "$0.023"
    assert _fmt_cost(0.999) == "$0.999"


def test_fmt_cost_dollar_plus_shows_two_decimals():
    assert _fmt_cost(1.234) == "$1.23"
    assert _fmt_cost(12.5) == "$12.50"


def test_fmt_cost_range_collapses_when_equal():
    # Single-pass analysis (no reduce) produces lo == hi; render as one number.
    assert _fmt_cost_range(0.023, 0.023) == "$0.023"


def test_fmt_cost_range_shows_two_when_different():
    # Both values < $1 → both get 3-decimal precision.
    assert _fmt_cost_range(0.01, 0.05) == "$0.010–$0.050"


def test_fmt_cost_range_handles_none():
    assert _fmt_cost_range(None, None) == "—"
    # One-sided range falls back to a single number.
    assert _fmt_cost_range(0.02, None) == "$0.020"
    assert _fmt_cost_range(None, 0.02) == "$0.020"


def test_extra_enrich_kinds_filters_defaults():
    # voice/videonote are "expected" — the notice should only fire for extras.
    assert _extra_enrich_kinds(["voice", "videonote"]) == []
    assert _extra_enrich_kinds(["voice", "image"]) == ["image"]
    assert _extra_enrich_kinds(["image", "doc", "link"]) == ["image", "doc", "link"]


def test_extra_enrich_kinds_none_means_no_notice():
    # None = wizard skipped, use config defaults — no notice needed.
    assert _extra_enrich_kinds(None) == []
