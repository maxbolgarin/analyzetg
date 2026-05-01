"""Status banner must distinguish "session file exists" from "session authorized".

Regression: Telethon writes the SQLiteSession file on first
``client.connect()`` (DC info, server addresses, port — well before the
user completes login). The ``auth_key`` row is what proves
authorization. The status banner used to declare "session linked"
purely on file existence, which contradicted ``unread login``'s
"Credentials are saved but the session isn't authorized" reality
check on the same install.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from rich.console import Console


def _create_telethon_session_file(path: Path, *, auth_key: bytes | None) -> None:
    """Mimic Telethon's SQLiteSession schema. ``auth_key=None`` ⇒ pre-login state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "dc_id INTEGER PRIMARY KEY, "
            "server_address TEXT, port INTEGER, "
            "auth_key BLOB, takeout_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (2, "149.154.167.50", 443, auth_key, None),
        )
        conn.commit()
    finally:
        conn.close()


def _capture_status_banner(monkeypatch) -> str:
    """Render ``_print_config_status`` to a string without touching stdout."""
    import unread.cli as cli_mod

    buf = io.StringIO()
    monkeypatch.setattr(cli_mod, "console", Console(file=buf, width=120, force_terminal=False))
    cli_mod._print_config_status()
    return buf.getvalue()


def _resolve_session_file_path() -> Path:
    """Telethon appends ``.session`` when the configured path doesn't end with it."""
    from unread.config import reset_settings
    from unread.core.paths import default_session_path

    reset_settings()
    sess = default_session_path()
    return sess.with_name(sess.name + ".session")


def test_unauthorized_session_file_does_not_show_session_linked(monkeypatch) -> None:
    """File present, ``auth_key`` NULL → must not claim ``session linked``."""
    real_session = _resolve_session_file_path()
    _create_telethon_session_file(real_session, auth_key=None)
    try:
        out = _capture_status_banner(monkeypatch)
    finally:
        real_session.unlink(missing_ok=True)

    assert "session linked" not in out, (
        f"Banner falsely reported 'session linked' for a session file with NULL auth_key. Output:\n{out}"
    )


def test_authorized_session_file_shows_session_linked(monkeypatch) -> None:
    """File present, ``auth_key`` populated → ``session linked``."""
    real_session = _resolve_session_file_path()
    _create_telethon_session_file(real_session, auth_key=b"\x01" * 256)
    try:
        out = _capture_status_banner(monkeypatch)
    finally:
        real_session.unlink(missing_ok=True)

    assert "session linked" in out, f"Expected 'session linked' in:\n{out}"


def test_no_session_file_does_not_show_session_linked(monkeypatch) -> None:
    """File absent → must not claim ``session linked`` either."""
    real_session = _resolve_session_file_path()
    real_session.unlink(missing_ok=True)
    out = _capture_status_banner(monkeypatch)
    assert "session linked" not in out
