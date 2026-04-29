"""`_is_file_with_timeout` short-circuits on stalled filesystems.

A wedged NFS / SMB mount makes `Path.is_file()` block on `stat()` with
no exception. We probe in a daemon thread and treat a timeout as "not a
file" so `unread <bare-token>` keeps responding even when cwd contains
a stalled mount.
"""

from __future__ import annotations

import time
from pathlib import Path

from unread.cli import _is_file_with_timeout


def test_real_file_returns_true_quickly():
    t0 = time.perf_counter()
    assert _is_file_with_timeout(Path("README.md"), 0.2) is True
    assert time.perf_counter() - t0 < 0.1, "real-file probe was too slow"


def test_missing_file_returns_false_quickly():
    t0 = time.perf_counter()
    assert _is_file_with_timeout(Path("/nonexistent/no_such_file.pdf"), 0.2) is False
    assert time.perf_counter() - t0 < 0.1


class _SlowPath:
    """Mimics a Path whose `is_file()` hangs on a wedged mount."""

    def is_file(self) -> bool:
        time.sleep(2.0)
        return True


def test_stalled_probe_times_out():
    """Probe gives up after the configured timeout instead of blocking forever."""
    t0 = time.perf_counter()
    result = _is_file_with_timeout(_SlowPath(), timeout_sec=0.2)
    elapsed = time.perf_counter() - t0
    assert result is False, "should return False on timeout"
    assert elapsed < 0.5, f"probe took {elapsed:.2f}s — should give up at ~0.2s"
