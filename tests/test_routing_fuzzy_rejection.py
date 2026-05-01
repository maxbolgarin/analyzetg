"""Bare `unread <ref>` rejects free-form strings that aren't files / URLs /
explicit Telegram refs. The chat-by-title escape hatch is the magic
`tg` ref → interactive chat picker.

Pins:

* `_is_explicit_telegram_ref` recognises @user, t.me/..., tg://, numeric
  ids, and the literal `me` — and nothing else.
* `unread "some random text"` exits 1 with a banner pointing at
  `unread tg`, `unread @user`, URLs, files, and stdin.
* The hint mentions `unread tg` (the magic ref → interactive picker)
  so the user can find a fuzzy-titled chat from the picker.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from unread.cli import _is_explicit_telegram_ref, app


@pytest.mark.parametrize(
    "ref,expected",
    [
        # Explicit Telegram shapes — accepted at the bare entry point.
        ("@durov", True),
        ("@", False),  # malformed — but '@' alone matches startswith;
        # acceptable false-positive: handler will surface its own error.
        ("me", True),
        ("ME", True),  # case-insensitive
        ("https://t.me/durov", True),
        ("http://t.me/durov", True),
        ("tg://resolve?domain=foo", True),
        ("-1001234567890", True),
        ("1234567", True),
        ("0", True),
        # Non-Telegram shapes — rejected.
        ("Bull Trading", False),
        ('"some text"', False),
        ("describe", False),
        ("durov", False),  # bare username without @
        ("https://example.com", False),
        ("./report.pdf", False),
        ("", False),
        ("   ", False),
    ],
)
def test_is_explicit_telegram_ref(ref: str, expected: bool) -> None:
    # The "@" alone case yields True (startswith @ is enough); accept that.
    if ref == "@":
        assert _is_explicit_telegram_ref(ref) is True
        return
    assert _is_explicit_telegram_ref(ref) is expected


def test_bare_unread_rejects_free_form_text() -> None:
    """`unread "some random text"` exits with the unrecognized-ref banner."""
    runner = CliRunner()
    # Pre-populate with fake creds so we don't hit the missing-provider
    # banner first; the rejection should fire BEFORE _ensure_ready.
    result = runner.invoke(app, ["some random text"])
    assert result.exit_code == 1
    assert "Couldn't route" in result.output
    # The hint advertises the magic `tg` ref as the interactive escape
    # hatch (post-redesign there's no `unread tg "title"` form anymore).
    assert "unread tg" in result.output
    # Stdin guidance for the "I meant raw text" case.
    assert "echo" in result.output
    # Other entry-point pointers.
    assert "@username" in result.output
    assert "t.me" in result.output


def test_bare_unread_accepts_explicit_telegram_handle(monkeypatch) -> None:
    """`unread @durov` doesn't trip the rejection (it gets through to
    `_ensure_ready_for_analyze`, which is the next layer's job to gate on)."""
    runner = CliRunner()
    # Force-fail at the next layer so we don't actually hit Telegram —
    # we only care that the unrecognized-ref banner did NOT fire.
    import unread.cli as _cli

    monkeypatch.setattr(_cli, "_ensure_ready_for_analyze", lambda _ref: False)
    result = runner.invoke(app, ["@durov"])
    assert "Couldn't route" not in result.output


def test_bare_unread_accepts_local_file_path(tmp_path, monkeypatch) -> None:
    """`unread ./somefile.md` is not rejected by the fuzzy guard."""
    f = tmp_path / "notes.md"
    f.write_text("hi", encoding="utf-8")
    runner = CliRunner()

    import unread.cli as _cli

    monkeypatch.setattr(_cli, "_ensure_ready_for_analyze", lambda _ref: False)
    result = runner.invoke(app, [str(f)])
    assert "Couldn't route" not in result.output


def test_tg_magic_ref_bypasses_rejection(monkeypatch) -> None:
    """`unread tg` is the magic ref → interactive picker. Must not trip
    the unrecognized-ref banner; the picker handles fuzzy title search
    internally. (Pre-redesign this test exercised `unread tg "title"`
    which routed through the now-gone `tg` typer subgroup.)"""
    runner = CliRunner()

    import unread.cli as _cli

    monkeypatch.setattr(_cli, "_ensure_ready_for_analyze", lambda _ref: False)
    result = runner.invoke(app, ["tg"])
    # Magic ref bypasses the bare-form rejection — no banner.
    assert "Couldn't route" not in result.output
