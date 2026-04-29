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


def test_tg_init_skips_ai_provider_step(isolated_home: Path, mock_telethon) -> None:
    """`unread tg init` (scope='telegram_only') jumps straight from folder
    pick to the Telegram-credentials step — no provider menu, no key prompt."""
    from unread.tg.commands import cmd_init

    # Folder=1, then Telegram api_id=12345, api_hash=abcdef. Note: NO
    # provider-pick prompt, NO key prompt — those would be the 2nd/3rd
    # entries in `prompt_inputs` if the AI step fired. If it does fire
    # the iter raises StopIteration and the test fails loudly.
    prompt_inputs = iter(["1", "12345", "abcdef"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=True),  # yes to Telegram setup
    ):
        asyncio.run(cmd_init(scope="telegram_only"))

    secrets = _read_secrets(isolated_home)
    # Only Telegram creds were saved — AI step was skipped entirely.
    assert "openai.api_key" not in secrets
    assert secrets.get("telegram.api_id") == "12345"
    assert secrets.get("telegram.api_hash") == "abcdef"


def test_init_full_runs_ai_provider_step(isolated_home: Path, mock_telethon) -> None:
    """`unread init` (default scope='full') asks the provider step too."""
    from unread.tg.commands import cmd_init

    # Folder=1, provider=1 (openai), key=sk-test, Telegram=n
    prompt_inputs = iter(["1", "1", "sk-test"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=False),  # decline Telegram
    ):
        asyncio.run(cmd_init(scope="full"))

    secrets = _read_secrets(isolated_home)
    assert secrets == {"openai.api_key": "sk-test"}


def test_init_top_level_command_routes_to_full_scope(isolated_home: Path, mock_telethon) -> None:
    """`unread init` (the new top-level command) ↔ `cmd_init(scope='full')`."""
    from typer.testing import CliRunner

    from unread.cli import app

    runner = CliRunner()
    prompt_inputs = iter(["1", "1", "sk-via-cli"])
    with (
        patch("typer.prompt", side_effect=lambda *a, **kw: next(prompt_inputs)),
        patch("typer.confirm", return_value=False),
    ):
        result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert _read_secrets(isolated_home) == {"openai.api_key": "sk-via-cli"}


def test_wizard_short_circuits_when_already_configured(
    isolated_home: Path, mock_telethon, monkeypatch
) -> None:
    """install.toml + populated env → folder & Telegram-creds steps stay
    silent; the AI provider step now fires unconditionally on full-scope
    re-runs (the menu offers "Keep current" so the user can press Enter
    through it). Only Telethon auth runs after that, no Telegram-creds
    prompt because creds are already set.
    """
    _seed_pointer_at_default(isolated_home)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-set")
    monkeypatch.setenv("TELEGRAM_API_ID", "9999")
    monkeypatch.setenv("TELEGRAM_API_HASH", "envhash")
    from unread.config import reset_settings

    reset_settings()

    from unread.tg.commands import cmd_init

    # AI menu fallback (non-TTY) renders a numeric prompt; "1" picks the
    # "Keep current" row that was prepended because the active provider
    # already has a key. That's the only typer.prompt() call the wizard
    # should make. typer.confirm must not fire (Telegram creds are set).
    confirm_boom = MagicMock(side_effect=AssertionError("wizard should not confirm"))
    with (
        patch("typer.prompt", return_value="1"),
        patch("typer.confirm", confirm_boom),
    ):
        asyncio.run(cmd_init())

    confirm_boom.assert_not_called()
    # Telethon auth still runs (the only step that fires post-init).
    mock_telethon.connect.assert_awaited()
