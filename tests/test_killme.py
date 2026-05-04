"""Tests for `unread killme` — the irreversible self-uninstall.

Coverage:

* The plan detects install-dir contents, populated keychain slots, the
  cached encryption key, and a `uv tool` binary install.
* `--yes` skips the type-in prompt.
* Without `--yes` and without a TTY, the command refuses (so a runaway
  pipe can't trigger it).
* Typing the wrong word (anything other than ``killme``) cancels.
* On confirmation, the install dir, keychain entries, runtime key, and
  binary are all removed (binary uninstall is mocked).
* When the install pointer points at a custom path outside the default
  ``~/.unread/`` shell, both the custom dir AND the pointer file are
  removed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_install_home(tmp_path, monkeypatch):
    """Point UNREAD_HOME at a fresh tmp dir with the canonical layout.

    Also redirects `Path.home()` to the tmp tree so the install-pointer
    lookup at ``~/.unread/install.toml`` resolves under the test sandbox
    instead of the developer's real `~/.unread/`. Without this, plan
    construction picks up the developer's actual pointer file as a
    "separate target" because UNREAD_HOME and `Path.home()` disagree.
    """
    user_home = tmp_path / "user_home"
    user_home.mkdir()
    home = user_home / ".unread"
    storage = home / "storage"
    storage.mkdir(parents=True)
    (home / "reports").mkdir()
    (home / ".env").write_text("OPENAI_API_KEY=test\n")
    (home / "config.toml").write_text("[locale]\nlanguage = 'en'\n")
    (storage / "data.sqlite").write_bytes(b"\x00" * 1024)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("UNREAD_HOME", str(home))
    from unread.config import reset_settings

    reset_settings()
    return home


def test_build_plan_lists_home_keychain_runtime_and_binary(fresh_install_home):
    """Plan picks up the install dir, populated keychain slots, runtime key, and uv binary."""
    home = fresh_install_home
    runtime_dir = home / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "key").write_text("{}")

    from unread import killme as km

    with (
        patch.object(
            km,
            "_detect_binary_uninstall",
            return_value=("uv tool", ["/fake/uv", "tool", "uninstall", "unread"]),
        ),
        patch("unread.secrets_backend.keychain_available", return_value=True),
        patch(
            "unread.secrets_backend.keychain_read",
            side_effect=lambda key: "x" if key in {"openai.api_key", "telegram.api_id"} else None,
        ),
    ):
        plan = km._build_plan()

    assert plan.install_home == home.resolve()
    # Install lives at default `~/.unread/` style (UNREAD_HOME override) so
    # the install pointer file should NOT be flagged as a separate target.
    assert plan.install_pointer is None
    assert plan.install_home_size > 0
    assert plan.runtime_key_path is not None
    assert plan.runtime_key_path.name == "key"
    assert plan.binary_uninstall is not None
    assert plan.binary_uninstall[0] == "uv tool"
    assert plan.keychain_slots == ["openai.api_key", "telegram.api_id"]
    # All top-level entries should appear at least once.
    names = {p.name for p, _ in plan.home_entries}
    assert {".env", "config.toml", "reports", "storage", ".runtime"}.issubset(names)


def test_killme_yes_wipes_everything(fresh_install_home):
    """`--yes` deletes the install home, calls keychain_delete per slot, and runs uv uninstall."""
    home = fresh_install_home
    from unread import killme as km

    keychain_deletions: list[str] = []

    def fake_keychain_delete(key):
        keychain_deletions.append(key)
        return True

    completed = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with (
        patch.object(
            km,
            "_detect_binary_uninstall",
            return_value=("uv tool", ["/fake/uv", "tool", "uninstall", "unread"]),
        ),
        patch("unread.secrets_backend.keychain_available", return_value=True),
        patch(
            "unread.secrets_backend.keychain_read",
            side_effect=lambda key: "x" if key == "openai.api_key" else None,
        ),
        patch("unread.secrets_backend.keychain_delete", side_effect=fake_keychain_delete),
        patch("unread.killme.subprocess.run", return_value=completed) as fake_run,
    ):
        rc = km.cmd_killme(yes=True)

    assert rc == 0
    assert not home.exists(), "install dir should be gone"
    assert keychain_deletions == ["openai.api_key"]
    fake_run.assert_called_once()
    args, kwargs = fake_run.call_args
    assert args[0] == ["/fake/uv", "tool", "uninstall", "unread"]
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    # cwd is set to Path.home() because the install dir we just deleted
    # may have been the user's CWD; subprocess inherits the parent's
    # CWD and would fail with ENOENT before even running.
    assert "cwd" in kwargs


def test_killme_without_yes_refuses_in_non_tty(fresh_install_home):
    """Piped invocation without --yes must NOT delete anything."""
    home = fresh_install_home
    from unread import killme as km

    with (
        patch.object(km, "_detect_binary_uninstall", return_value=None),
        patch("unread.secrets_backend.keychain_available", return_value=False),
        patch("sys.stdin") as fake_stdin,
    ):
        fake_stdin.isatty.return_value = False
        rc = km.cmd_killme(yes=False)

    assert rc == 1
    assert home.exists(), "install dir must remain when confirmation refuses"


def test_killme_wrong_word_cancels(fresh_install_home):
    """Typing anything other than 'killme' cancels and preserves state."""
    home = fresh_install_home
    from unread import killme as km

    with (
        patch.object(km, "_detect_binary_uninstall", return_value=None),
        patch("unread.secrets_backend.keychain_available", return_value=False),
        patch("sys.stdin") as fake_stdin,
        patch("builtins.input", return_value="not the magic word"),
    ):
        fake_stdin.isatty.return_value = True
        rc = km.cmd_killme(yes=False)

    assert rc == 1
    assert home.exists()


def test_killme_correct_typed_word_proceeds(fresh_install_home):
    """Typing 'killme' (exact match) confirms and runs the deletion."""
    home = fresh_install_home
    from unread import killme as km

    completed = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with (
        patch.object(km, "_detect_binary_uninstall", return_value=None),
        patch("unread.secrets_backend.keychain_available", return_value=False),
        patch("sys.stdin") as fake_stdin,
        patch("builtins.input", return_value="killme"),
        patch("unread.killme.subprocess.run", return_value=completed),
    ):
        fake_stdin.isatty.return_value = True
        rc = km.cmd_killme(yes=False)

    assert rc == 0
    assert not home.exists()


def test_killme_removes_install_pointer_when_home_is_custom(tmp_path, monkeypatch):
    """A custom-path install (pointer at ~/.unread/install.toml → /custom/path) should
    delete BOTH the custom dir AND the pointer file at ~/.unread/install.toml."""
    fake_home = tmp_path / "user_home"
    (fake_home / ".unread").mkdir(parents=True)
    pointer = fake_home / ".unread" / "install.toml"

    custom_install = tmp_path / "custom_unread"
    (custom_install / "storage").mkdir(parents=True)
    (custom_install / "storage" / "data.sqlite").write_bytes(b"x")

    pointer.write_text(f'home = "{custom_install}"\n')

    # Simulate `Path.home()` returning our fake user home so
    # `install_pointer_path()` resolves to `fake_home/.unread/install.toml`.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Override UNREAD_HOME so `unread_home()` returns the custom path.
    # `unread_home()` checks UNREAD_HOME first, so this also overrides
    # the pointer-file path resolution. That's fine for the test — we
    # explicitly verify the pointer-file deletion below.
    monkeypatch.setenv("UNREAD_HOME", str(custom_install))

    from unread.config import reset_settings

    reset_settings()
    from unread import killme as km

    completed = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with (
        patch.object(km, "_detect_binary_uninstall", return_value=None),
        patch("unread.secrets_backend.keychain_available", return_value=False),
        patch("unread.killme.subprocess.run", return_value=completed),
    ):
        plan = km._build_plan()
        # Plan should flag the pointer separately because the install
        # home is NOT under `~/.unread/`.
        assert plan.install_pointer is not None
        assert plan.install_pointer == pointer

        rc = km.cmd_killme(yes=True)

    assert rc == 0
    assert not custom_install.exists(), "custom install dir should be gone"
    assert not pointer.exists(), "pointer file should be gone"


def test_detect_binary_uninstall_returns_none_without_uv(monkeypatch):
    """No `uv` on PATH → the binary-uninstall step is skipped."""
    from unread import killme as km

    monkeypatch.setattr(km.shutil, "which", lambda _: None)
    assert km._detect_binary_uninstall() is None


def test_detect_binary_uninstall_returns_none_when_unread_not_listed(monkeypatch):
    """`uv tool list` output without `unread` → skip the binary uninstall."""
    from unread import killme as km

    monkeypatch.setattr(km.shutil, "which", lambda name: "/fake/uv" if name == "uv" else None)

    completed = type("R", (), {"returncode": 0, "stdout": "ruff v0.1\n", "stderr": ""})()
    monkeypatch.setattr(km.subprocess, "run", lambda *a, **kw: completed)

    assert km._detect_binary_uninstall() is None


def test_detect_binary_uninstall_returns_argv_when_unread_listed(monkeypatch):
    """`uv tool list` output containing `unread` → return the right uninstall argv."""
    from unread import killme as km

    monkeypatch.setattr(km.shutil, "which", lambda name: "/fake/uv" if name == "uv" else None)

    completed = type("R", (), {"returncode": 0, "stdout": "unread v1.0.0\n", "stderr": ""})()
    monkeypatch.setattr(km.subprocess, "run", lambda *a, **kw: completed)

    result = km._detect_binary_uninstall()
    assert result == ("uv tool", ["/fake/uv", "tool", "uninstall", "unread"])


# ---------------------------------------------------------------------
# `_reject_unsafe_home` — refuse to rmtree dangerous paths.
# ---------------------------------------------------------------------


def test_reject_unsafe_home_rejects_filesystem_root():
    """A misconfigured `UNREAD_HOME=/` would, without this guard, take
    the entire root filesystem with it on `killme`."""
    from unread.killme import _reject_unsafe_home

    reason = _reject_unsafe_home(Path("/"))
    assert reason is not None
    assert isinstance(reason, str) and reason.strip(), "rejection reason must be a non-empty string"


def test_reject_unsafe_home_rejects_user_home(tmp_path, monkeypatch):
    """`UNREAD_HOME=$HOME` is a common misconfiguration."""
    from unread.killme import _reject_unsafe_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    reason = _reject_unsafe_home(tmp_path)
    assert reason is not None
    assert isinstance(reason, str) and reason.strip()


def test_reject_unsafe_home_rejects_system_dirs():
    """First-level POSIX system dirs are unrecoverable if wiped."""
    from unread.killme import _reject_unsafe_home

    for d in ("/usr", "/etc", "/var", "/home", "/Applications"):
        reason = _reject_unsafe_home(Path(d))
        assert reason is not None, d
        assert isinstance(reason, str) and reason.strip(), d


def test_reject_unsafe_home_rejects_top_level_dirs():
    """Anything with fewer than 3 path components is treated as too risky."""
    from unread.killme import _reject_unsafe_home

    # `/foo` is 2 parts ('/', 'foo') — refuse.
    reason = _reject_unsafe_home(Path("/foo"))
    assert reason is not None
    assert isinstance(reason, str) and reason.strip()


def test_reject_unsafe_home_accepts_nested_install(tmp_path):
    """A normal nested install dir under tmp passes the guard."""
    from unread.killme import _reject_unsafe_home

    nested = tmp_path / "user_home" / ".unread"
    nested.mkdir(parents=True)
    assert _reject_unsafe_home(nested) is None


def test_killme_refuses_when_home_is_unsafe(monkeypatch):
    """End-to-end: `cmd_killme` returns 1 and never invokes rmtree when
    the resolved install home is dangerous (e.g. `UNREAD_HOME=/`)."""
    from unittest.mock import patch

    from unread import killme as km
    from unread.core import paths as _paths

    monkeypatch.setattr(_paths, "unread_home", lambda: Path("/"))
    from unread.config import reset_settings

    reset_settings()

    with patch.object(km.shutil, "rmtree") as fake_rm:
        rc = km.cmd_killme(yes=True)

    assert rc == 1
    fake_rm.assert_not_called()
