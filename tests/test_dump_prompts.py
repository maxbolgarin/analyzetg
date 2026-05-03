"""`unread.dump.prompts.pick_dump_mode` — TTY guard + select wrapping."""

from __future__ import annotations

from unittest.mock import patch

from unread.dump.prompts import pick_dump_mode


def test_returns_none_in_non_tty() -> None:
    """Non-TTY runs must NOT prompt — caller turns the None into an error."""
    with patch("sys.stdin.isatty", return_value=False):
        assert pick_dump_mode("website", yes=False) is None
        assert pick_dump_mode("youtube", yes=False) is None


def test_yes_skips_prompt_even_on_tty() -> None:
    """`--yes` means non-interactive: the picker must not prompt."""
    with patch("sys.stdin.isatty", return_value=True):
        assert pick_dump_mode("website", yes=True) is None
        assert pick_dump_mode("youtube", yes=True) is None


def test_returns_select_choice_for_website() -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.util.prompt.select", return_value="full") as sel,
    ):
        out = pick_dump_mode("website", yes=False)
    assert out == "full"
    sel.assert_called_once()
    # Choice values seen by select() must be the website set.
    kwargs = sel.call_args.kwargs
    values = [c.value for c in kwargs["choices"]]
    assert values == ["text", "full"]


def test_returns_select_choice_for_youtube() -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.util.prompt.select", return_value="audio") as sel,
    ):
        out = pick_dump_mode("youtube", yes=False)
    assert out == "audio"
    kwargs = sel.call_args.kwargs
    values = [c.value for c in kwargs["choices"]]
    assert values == ["transcript", "audio", "video"]


def test_keyboard_interrupt_returns_none() -> None:
    """User pressing Esc / Ctrl-C in the picker → caller errors gracefully."""
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.util.prompt.select", side_effect=KeyboardInterrupt),
    ):
        assert pick_dump_mode("website", yes=False) is None
