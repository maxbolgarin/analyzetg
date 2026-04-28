"""Pointer-file resolution for `unread_home()`.

Pins three invariants:
  - `UNREAD_HOME` env var beats every other source.
  - `~/.unread/install.toml` with `home = "..."` redirects there.
  - Missing / corrupt pointers fall through silently to the default.

Tests run with `monkeypatch.setenv("HOME", tmp_path)` so we never touch
the developer's real home directory or the conftest-managed
`UNREAD_HOME` location.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_paths(tmp_path: Path, monkeypatch):
    """Hand back a freshly-imported `unread.core.paths` rooted at `tmp_path`.

    Patches `HOME` so `Path.home()` returns the temp dir, drops any
    `UNREAD_HOME` override (so the pointer-file branch can run), and
    reloads the module to clear any cached imports.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNREAD_HOME", raising=False)
    from unread.core import paths

    importlib.reload(paths)
    return paths, tmp_path


def test_default_when_no_pointer_or_env(fresh_paths) -> None:
    paths, home = fresh_paths
    assert paths.unread_home() == home / ".unread"


def test_env_override_beats_pointer(fresh_paths, monkeypatch) -> None:
    paths, home = fresh_paths
    paths.write_install_pointer(home / "redirected")
    monkeypatch.setenv("UNREAD_HOME", str(home / "from-env"))
    assert paths.unread_home() == home / "from-env"


def test_pointer_redirects(fresh_paths) -> None:
    paths, home = fresh_paths
    target = home / "elsewhere"
    paths.write_install_pointer(target)
    # `write_install_pointer` resolves the path; `unread_home` returns it as-is.
    assert paths.unread_home() == target.resolve()


def test_pointer_with_empty_home_falls_through_to_default(fresh_paths) -> None:
    paths, home = fresh_paths
    # The default-marker write: `home = ""`. Presence of the file means
    # "setup has been done", but the actual install lives at the default
    # `~/.unread/`.
    paths.write_install_pointer(None)
    assert paths.unread_home() == home / ".unread"


def test_corrupt_pointer_silently_falls_back(fresh_paths) -> None:
    paths, home = fresh_paths
    # Write garbage into the pointer file. `unread_home()` must not crash.
    pointer = paths.install_pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("definitely [[[ not toml", encoding="utf-8")
    assert paths.unread_home() == home / ".unread"


def test_install_pointer_path_is_under_home_not_unread_home(fresh_paths) -> None:
    """The pointer must live at `~/.unread/install.toml` regardless of where the
    actual install lives — otherwise we couldn't read it BEFORE resolving
    `unread_home()`."""
    paths, home = fresh_paths
    paths.write_install_pointer(home / "elsewhere")
    assert paths.install_pointer_path() == home / ".unread" / "install.toml"
    assert paths.install_pointer_path().is_file()
