"""Small-but-easy-to-break CLI helper functions."""

from __future__ import annotations

import pytest

from analyzetg.cli import _fmt_bytes, _parse_duration_days

# --- _parse_duration_days -----------------------------------------------


def test_parse_duration_days_suffix() -> None:
    assert _parse_duration_days("30d") == 30
    assert _parse_duration_days("1d") == 1
    assert _parse_duration_days("365d") == 365


def test_parse_duration_weeks() -> None:
    assert _parse_duration_days("1w") == 7
    assert _parse_duration_days("2w") == 14


def test_parse_duration_bare_int() -> None:
    assert _parse_duration_days("42") == 42


def test_parse_duration_case_insensitive_and_trimmed() -> None:
    assert _parse_duration_days(" 90D ") == 90
    assert _parse_duration_days("\t4W\n") == 28


def test_parse_duration_invalid_raises() -> None:
    with pytest.raises(ValueError):
        _parse_duration_days("tomorrow")


# --- _fmt_bytes ---------------------------------------------------------


def test_fmt_bytes_under_1kib() -> None:
    assert _fmt_bytes(0) == "0 B"
    assert _fmt_bytes(512) == "512 B"
    assert _fmt_bytes(1023) == "1023 B"


def test_fmt_bytes_kib() -> None:
    assert _fmt_bytes(1024) == "1.0 KiB"
    # 1500 B == 1.46 KiB; one-decimal formatting.
    assert _fmt_bytes(1500).endswith("KiB")


def test_fmt_bytes_mib() -> None:
    assert _fmt_bytes(1024 * 1024) == "1.0 MiB"


def test_fmt_bytes_gib_ceiling() -> None:
    # Values above GiB threshold still print in GiB (no TiB unit defined).
    result = _fmt_bytes(5 * 1024 * 1024 * 1024)
    assert result.endswith("GiB")
    assert "5.0" in result
