"""`unread -` (stdin) caps input at 100 MB to prevent OOM.

A user piping a multi-GB file used to consume all available memory.
The fix in `unread/files/commands.py:_read_stdin_bytes` reads at most
`_MAX_STDIN_BYTES + 1` bytes to detect overflow without buffering the
tail, then truncates to the cap and returns a `truncated=True` flag
that callers must surface in the synthetic message metadata.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from unread.files.commands import _MAX_STDIN_BYTES, _read_stdin_bytes


def _patch_stdin_with_payload(payload: bytes) -> object:
    """Build a `sys.stdin.buffer` substitute whose `read(n)` honours the limit."""
    fake_buffer = io.BytesIO(payload)
    fake_stdin = MagicMock()
    fake_stdin.buffer = fake_buffer
    return fake_stdin


def test_small_stdin_returns_full_payload_not_truncated() -> None:
    payload = b"hello world\n" * 10
    with patch("unread.files.commands.sys") as fake_sys:
        fake_sys.stdin = _patch_stdin_with_payload(payload)
        data, truncated = _read_stdin_bytes()
    assert data == payload
    assert truncated is False


def test_oversize_stdin_truncates_and_flags() -> None:
    """A payload that's just one byte over the cap must trigger truncation."""
    payload = b"x" * (_MAX_STDIN_BYTES + 100)  # Comfortably over the cap
    with patch("unread.files.commands.sys") as fake_sys:
        fake_sys.stdin = _patch_stdin_with_payload(payload)
        data, truncated = _read_stdin_bytes()
    assert truncated is True
    assert len(data) == _MAX_STDIN_BYTES


def test_exact_cap_stdin_not_truncated() -> None:
    """A payload that exactly equals the cap must NOT be flagged truncated.

    Otherwise we'd cry wolf on legitimate inputs that happen to land at
    the boundary.
    """
    payload = b"x" * _MAX_STDIN_BYTES
    with patch("unread.files.commands.sys") as fake_sys:
        fake_sys.stdin = _patch_stdin_with_payload(payload)
        data, truncated = _read_stdin_bytes()
    assert truncated is False
    assert len(data) == _MAX_STDIN_BYTES
