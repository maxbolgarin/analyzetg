"""Double-Esc cancel for the interactive wizard.

`_bind_escape` exits the current questionary prompt on a single Esc
with the caller's `value` (typically `BACK`). Pressing Esc twice within
`_DOUBLE_ESC_WINDOW_S` instead exits with `None` (full cancel) — the
wizard's main loop already unwinds on `None`, so users get a fast
"get me out" shortcut without mashing Esc through every step.

prompt_toolkit's key handler is hard to drive in a unit test (needs an
event loop, terminal, focus tracking), so the timing decision lives in
a small pure helper `_is_double_esc(now, last_esc_at, window)` that we
exercise here. Integration confidence comes from manual wizard runs.
"""

from __future__ import annotations

from unread.interactive import _DOUBLE_ESC_WINDOW_S, _is_double_esc


def test_first_esc_with_initial_zero_timestamp_is_single():
    """The very first Esc on a fresh process has `last_esc_at == 0.0`.
    Without the strict `0 <` guard, `now - 0 <= window` would always
    pass and the first ever Esc would be mis-classified as a double."""
    # `now` is whatever monotonic returns — large positive number.
    assert _is_double_esc(now=12345.0, last_esc_at=0.0, window=_DOUBLE_ESC_WINDOW_S) is False


def test_second_esc_inside_window_is_double():
    """Two Esc presses with delta inside the window → double."""
    last = 100.0
    inside = last + _DOUBLE_ESC_WINDOW_S - 0.01
    assert _is_double_esc(now=inside, last_esc_at=last, window=_DOUBLE_ESC_WINDOW_S) is True


def test_second_esc_at_exactly_window_boundary_is_double():
    """Inclusive upper bound — equality with the window counts as
    double so a clock running at exactly the boundary doesn't reject."""
    last = 100.0
    boundary = last + _DOUBLE_ESC_WINDOW_S
    assert _is_double_esc(now=boundary, last_esc_at=last, window=_DOUBLE_ESC_WINDOW_S) is True


def test_second_esc_outside_window_is_single():
    """Past the window → just another single Esc (deliberate back-step)."""
    last = 100.0
    outside = last + _DOUBLE_ESC_WINDOW_S + 0.01
    assert _is_double_esc(now=outside, last_esc_at=last, window=_DOUBLE_ESC_WINDOW_S) is False


def test_same_timestamp_is_single():
    """Identical timestamps (delta == 0) reject — the strict `0 <`
    rules out the bootstrap case where `last_esc_at` hasn't been
    written yet, and incidentally prevents a single Esc from being
    classified as its own double if a clock returned the same value."""
    assert _is_double_esc(now=100.0, last_esc_at=100.0, window=_DOUBLE_ESC_WINDOW_S) is False


def test_window_is_positive_and_short_enough_for_double_tap():
    """Sanity: the configured window is in the right ballpark — long
    enough for a deliberate double tap (≥ ~250ms), short enough not
    to trip on slow back-stepping (≤ ~1s)."""
    assert 0.25 <= _DOUBLE_ESC_WINDOW_S <= 1.0
