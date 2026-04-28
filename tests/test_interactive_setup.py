"""Wizard pipeline behavior in `unread.tg.commands.cmd_init`.

Each test isolates `UNREAD_HOME` and `HOME` to a `tmp_path`, drops env
creds, and patches `typer.prompt` / `typer.confirm` to drive the
wizard end-to-end. Telethon auth is mocked so we don't hit the network.

The four scenarios in the plan:
  - Default folder + OpenAI provided + Telegram declined.
  - Default folder + OpenAI skipped (Enter) + Telegram provided.
  - install.toml present + creds present → wizard short-circuits past every step.
  - User picks "Exit" → no install.toml is written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Sandbox `HOME` and `UNREAD_HOME`; clear env-supplied creds."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "unread"))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from unread.config import reset_settings

    reset_settings()
    return tmp_path


@pytest.fixture
def mock_telethon():
    """Patch out Telethon's network calls; auth always succeeds."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(return_value=True)
    fake_client.start = AsyncMock()
    with (
        patch("unread.tg.commands.build_client", return_value=fake_client),
        patch("openai.AsyncOpenAI"),  # smoke test no-op
    ):
        yield fake_client


def _seed_pointer_at_default(home: Path) -> None:
    """Pre-write install.toml so the folder step is skipped."""
    pointer_dir = home / "fakehome" / ".unread"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    (pointer_dir / "install.toml").write_text('home = ""\n', encoding="utf-8")


def _read_secrets(home: Path) -> dict[str, str]:
    """Return rows from the data DB's `secrets` table, or {} if missing."""
    import sqlite3

    db = home / "unread" / "storage" / "data.sqlite"
    if not db.is_file():
        return {}
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("SELECT key, value FROM secrets")
        return dict(cur.fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def test_wizard_default_folder_openai_only(isolated_home: Path, mock_telethon) -> None:
    """Default folder; user picks OpenAI provider + gives key; declines Telegram."""
    from unread.tg.commands import cmd_init

    # Folder=1 (default), provider=1 (openai), OpenAI key=sk-..., Telegram=n
    prompt_inputs = iter(["1", "1", "sk-test"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=False),
    ):
        asyncio.run(cmd_init())

    # install.toml exists with empty home (= default).
    pointer = isolated_home / "fakehome" / ".unread" / "install.toml"
    assert pointer.is_file()
    assert 'home = ""' in pointer.read_text(encoding="utf-8")

    # Only OpenAI persisted.
    secrets = _read_secrets(isolated_home)
    assert secrets == {"openai.api_key": "sk-test"}

    # Telethon not invoked (no creds, no auth).
    mock_telethon.connect.assert_not_called()


def test_wizard_skip_openai_take_telegram(isolated_home: Path, mock_telethon) -> None:
    """OpenAI key skipped (Enter); Telegram provided + auth runs."""
    from unread.tg.commands import cmd_init

    # Folder=1, provider=1 (openai), OpenAI key=<empty>, api_id, api_hash
    prompt_inputs = iter(["1", "1", "", "12345", "abcdef"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=True),  # yes to Telegram setup
    ):
        asyncio.run(cmd_init())

    secrets = _read_secrets(isolated_home)
    assert secrets == {"telegram.api_id": "12345", "telegram.api_hash": "abcdef"}

    mock_telethon.connect.assert_awaited()


def test_wizard_anthropic_provider(isolated_home: Path, mock_telethon) -> None:
    """Provider=Anthropic → key lands in `anthropic.api_key`, not `openai.api_key`."""
    from unread.tg.commands import cmd_init

    # Folder=1, provider=3 (anthropic), key=sk-ant, Telegram=n
    prompt_inputs = iter(["1", "3", "sk-ant-fake"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=False),
    ):
        asyncio.run(cmd_init())

    secrets = _read_secrets(isolated_home)
    assert secrets == {"anthropic.api_key": "sk-ant-fake"}

    # Provider choice persists in app_settings, not secrets.
    import sqlite3

    db = isolated_home / "unread" / "storage" / "data.sqlite"
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key='ai.provider'")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "anthropic"


def test_wizard_local_provider_no_key(isolated_home: Path, mock_telethon) -> None:
    """Provider=Local → no API key prompt, `local.base_url` may be customized."""
    from unread.tg.commands import cmd_init

    # Folder=1, provider=5 (local), base_url=<empty: keep default>, Telegram=n
    prompt_inputs = iter(["1", "5", ""])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=False),
    ):
        asyncio.run(cmd_init())

    # No secrets row (local doesn't need a key).
    assert _read_secrets(isolated_home) == {}

    # ai.provider = "local" persisted.
    import sqlite3

    db = isolated_home / "unread" / "storage" / "data.sqlite"
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key='ai.provider'")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "local"


def test_wizard_exit_writes_nothing(isolated_home: Path, mock_telethon) -> None:
    """Picking 'Exit' at the folder step bails out cleanly."""
    from unread.tg.commands import cmd_init

    with patch("typer.prompt", return_value="4"):
        asyncio.run(cmd_init())

    pointer = isolated_home / "fakehome" / ".unread" / "install.toml"
    assert not pointer.is_file()
    assert _read_secrets(isolated_home) == {}
    mock_telethon.connect.assert_not_called()


def test_wizard_short_circuits_when_already_configured(
    isolated_home: Path, mock_telethon, monkeypatch
) -> None:
    """install.toml + populated env → no folder/OpenAI/Telegram prompt fires.

    Only Telethon auth runs. We assert by mocking `typer.prompt` to
    raise on any unexpected call.
    """
    _seed_pointer_at_default(isolated_home)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-set")
    monkeypatch.setenv("TELEGRAM_API_ID", "9999")
    monkeypatch.setenv("TELEGRAM_API_HASH", "envhash")
    from unread.config import reset_settings

    reset_settings()

    from unread.tg.commands import cmd_init

    boom = MagicMock(side_effect=AssertionError("wizard should not prompt"))
    confirm_boom = MagicMock(side_effect=AssertionError("wizard should not confirm"))
    with (
        patch("typer.prompt", boom),
        patch("typer.confirm", confirm_boom),
    ):
        asyncio.run(cmd_init())

    boom.assert_not_called()
    confirm_boom.assert_not_called()
    # Telethon auth still runs (it's the only step that fires post-init).
    mock_telethon.connect.assert_awaited()
