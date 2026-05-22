"""RotatingFileHandler wired through `[logging] file_path`.

Verifies:

- When `settings.logging.file_path` is set, structlog events ALSO land
  in the file (terminal output is unaffected).
- The handler is a `RotatingFileHandler`; exceeding `file_max_bytes`
  produces a numbered backup (`<name>.1`).
- When `file_path` is None (default), no file is created — terminal-only.
- The redactor still scrubs secrets BEFORE the file writer sees them, so
  the file never accumulates plaintext API keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Pin `UNREAD_HOME` and reset the settings singleton for the test."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    from unread.config import reset_settings

    reset_settings()
    yield tmp_path
    # Always close the file handler after a test so the next one can
    # rebind `setup_logging` without inheriting our handler.
    from unread.util.logging import _close_file_handler

    _close_file_handler()
    reset_settings()


def _write_config_with_logging(home: Path, body: str) -> None:
    """Drop a `config.toml` with the supplied `[logging]` block in place."""
    cfg = home / "config.toml"
    cfg.write_text(body, encoding="utf-8")


def test_default_file_path_creates_no_file(isolated_home: Path) -> None:
    """No `[logging]` block → no file is created and `setup_logging` runs."""
    from unread.util.logging import get_logger, setup_logging

    setup_logging()
    log = get_logger("test")
    log.info("hello-no-file", marker="present")

    # Nothing in the storage dir except whatever else `setup_logging`
    # touched — assert specifically that no rotating log file appeared.
    storage = isolated_home / "storage"
    if storage.is_dir():
        for child in storage.iterdir():
            assert not child.name.endswith(".log"), f"unexpected log file: {child}"


def test_file_path_set_writes_event_to_file(isolated_home: Path) -> None:
    """When `[logging] file_path` is set, the rendered event lands in the file."""
    log_path = isolated_home / "storage" / "unread.log"
    _write_config_with_logging(
        isolated_home,
        f'[logging]\nfile_path = "{log_path}"\n',
    )

    from unread.util.logging import get_logger, setup_logging

    # Verbose so the INFO event below propagates through the level
    # filter to the file handler. Default `normal` is WARNING-only.
    setup_logging(mode="verbose")
    log = get_logger("test")
    log.info("hello-from-test", marker="abc-123-needle")

    assert log_path.is_file(), f"expected log file at {log_path}"
    contents = log_path.read_text(encoding="utf-8")
    assert "hello-from-test" in contents
    assert "abc-123-needle" in contents


def test_file_handler_rotates_when_size_exceeded(isolated_home: Path) -> None:
    """Exceeding `file_max_bytes` produces a `<name>.1` backup."""
    log_path = isolated_home / "storage" / "unread.log"
    _write_config_with_logging(
        isolated_home,
        (
            "[logging]\n"
            f'file_path = "{log_path}"\n'
            "file_max_bytes = 200\n"  # ~one event per file
            "file_backup_count = 2\n"
        ),
    )

    from unread.util.logging import get_logger, setup_logging

    setup_logging(mode="verbose")
    log = get_logger("test")
    # Each call writes a long line; with maxBytes=200 the second/third
    # event should force at least one rotation.
    payload = "x" * 200
    for i in range(6):
        log.info("rotate-probe", marker=f"event-{i}", payload=payload)

    backup = log_path.with_suffix(log_path.suffix + ".1")
    assert backup.is_file(), (
        f"expected rotated backup at {backup}; "
        f"actual children: {sorted(p.name for p in log_path.parent.iterdir())}"
    )


def test_file_handler_redacts_secrets(isolated_home: Path) -> None:
    """Secrets must be scrubbed by the redactor BEFORE hitting the file."""
    log_path = isolated_home / "storage" / "unread.log"
    _write_config_with_logging(
        isolated_home,
        f'[logging]\nfile_path = "{log_path}"\n',
    )

    from unread.util.logging import get_logger, setup_logging

    setup_logging()
    log = get_logger("test")
    fake_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    log.warning("oops-leaked", api_key=fake_key, body=f"prefix {fake_key} suffix")

    contents = log_path.read_text(encoding="utf-8")
    # Neither the keyed value nor the inline-shaped one should survive.
    assert fake_key not in contents
    assert "REDACTED" in contents


def test_file_handler_path_with_tilde(isolated_home: Path, monkeypatch) -> None:
    """`~`-prefixed paths expand under the user's home directory.

    Pydantic's ``Path`` field doesn't auto-expand `~` — the
    `_resolve_file_handler` helper must do it before calling
    `RotatingFileHandler`.
    """
    # `Path.expanduser()` consults `$HOME` on POSIX — point that at the
    # test's tmp so the expansion is contained.
    fake_home = isolated_home / "userhome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    _write_config_with_logging(
        isolated_home,
        '[logging]\nfile_path = "~/custom.log"\n',
    )

    from unread.util.logging import get_logger, setup_logging

    setup_logging(mode="verbose")
    log = get_logger("test")
    log.info("home-expansion", marker="tilde-ok")

    expected = fake_home / "custom.log"
    assert expected.is_file(), f"expected expanded path at {expected}"
    assert "tilde-ok" in expected.read_text(encoding="utf-8")
