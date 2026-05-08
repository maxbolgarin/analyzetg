"""Tests for `_should_use_plain_citations` terminal detection."""

from __future__ import annotations

import pytest

from unread.util.report_render import _should_use_plain_citations


def test_force_plain_true_always_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert _should_use_plain_citations(force_plain=True) is True


@pytest.mark.parametrize(
    "term_program",
    ["iTerm.app", "WezTerm", "kitty", "ghostty", "Tabby", "Hyper"],
)
def test_known_good_terminals_keep_styled_links(monkeypatch: pytest.MonkeyPatch, term_program: str) -> None:
    monkeypatch.setenv("TERM_PROGRAM", term_program)
    assert _should_use_plain_citations(force_plain=False) is False


@pytest.mark.parametrize(
    "term_program",
    ["vscode", "Apple_Terminal", "alacritty", "", "JetBrains-JediTerm"],
)
def test_unknown_terminals_fall_back_to_plain(monkeypatch: pytest.MonkeyPatch, term_program: str) -> None:
    monkeypatch.setenv("TERM_PROGRAM", term_program)
    assert _should_use_plain_citations(force_plain=False) is True


def test_unset_term_program_falls_back_to_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert _should_use_plain_citations(force_plain=False) is True


def test_force_plain_overrides_known_good(monkeypatch: pytest.MonkeyPatch) -> None:
    """User who explicitly sets the flag gets plain even in iTerm2."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert _should_use_plain_citations(force_plain=True) is True
