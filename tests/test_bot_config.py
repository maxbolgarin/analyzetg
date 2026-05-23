"""Bot config: env-var loading, secrets overlay, allowlist semantics."""

from __future__ import annotations

from unread.config import load_settings, reset_settings


def test_bot_env_vars_overlay_settings(monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_TOKEN", "1234:abcd")
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "42")
    monkeypatch.setenv("UNREAD_BOT_CONCURRENCY", "5")
    monkeypatch.setenv("UNREAD_BOT_MAX_FILE_MB", "250")
    monkeypatch.setenv("UNREAD_BOT_DEFAULT_PRESET", "detailed")
    reset_settings()
    try:
        s = load_settings()
        assert s.bot.token == "1234:abcd"
        assert s.bot.owner_id == 42
        assert s.bot.concurrency == 5
        assert s.bot.max_file_mb == 250
        assert s.bot.default_preset == "detailed"
    finally:
        reset_settings()


def test_bot_owner_id_must_be_int(monkeypatch):
    import pytest

    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "not-a-number")
    reset_settings()
    try:
        with pytest.raises(ValueError, match="UNREAD_BOT_OWNER_ID"):
            load_settings()
    finally:
        reset_settings()


def test_bot_token_is_in_secret_allowlist():
    """telegram.bot_token must be in SECRET_KEYS so `put_secrets` accepts it."""
    from unread.db._keys import SECRET_KEYS

    assert "telegram.bot_token" in SECRET_KEYS


def test_botcfg_defaults_are_safe():
    """Default-constructed BotCfg refuses to start (owner_id=0, empty token)."""
    from unread.config import BotCfg

    cfg = BotCfg()
    assert cfg.token == ""
    assert cfg.owner_id == 0
    assert cfg.concurrency == 2
    assert cfg.max_file_mb == 100
    assert cfg.default_preset == ""


def test_botcfg_rejects_unknown_keys():
    """Strict-mode: typos in [bot] config.toml are loud errors."""
    import pytest
    from pydantic import ValidationError

    from unread.config import BotCfg

    with pytest.raises(ValidationError):
        BotCfg(toekn="oops")  # type: ignore[call-arg]


def test_cmd_bot_run_refuses_when_no_owner_and_no_session(monkeypatch, tmp_path):
    """No env-var owner AND no session file → refuse to start.

    The bot has no safe allowlist in this state — would otherwise
    trust-on-first-use whoever messaged first. Exits with status 1
    before any Telethon connection is opened.
    """
    import asyncio

    import pytest
    import typer

    from unread.bot.commands import cmd_bot_run

    # chdir to an empty tmp dir so the bot's CWD `.env.bot`
    # auto-discovery doesn't pick up the repo's real `.env.bot` (which
    # would populate owner_id and let the gate pass).
    monkeypatch.chdir(tmp_path)
    # Clean creds so other gates pass; force the no-owner / no-session
    # state explicitly.
    monkeypatch.setenv("UNREAD_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("UNREAD_BOT_OWNER_ID", raising=False)
    monkeypatch.delenv("UNREAD_BOT_ENV_FILE", raising=False)
    # Point telegram.session_path at a guaranteed-missing file.
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "fresh"))

    from unread.config import reset_settings

    reset_settings()
    try:
        with pytest.raises(typer.Exit) as excinfo:
            asyncio.run(cmd_bot_run())
        assert excinfo.value.exit_code == 1
    finally:
        reset_settings()


def test_env_bot_file_overlays_dotenv(monkeypatch, tmp_path):
    """Values in `.env.bot` flow into Settings via the dotenv overlay.

    The `.env.bot` overlay is loaded ON TOP of `~/.unread/.env` and
    is consulted by the same `_env()` helper as the rest of the
    settings chain, so `UNREAD_BOT_TOKEN=...` in `.env.bot` ends up
    in `settings.bot.token` without any per-key plumbing.
    """
    home = tmp_path / "fresh-home"
    home.mkdir()
    (home / ".env.bot").write_text(
        "UNREAD_BOT_TOKEN=from-env-bot-file\nUNREAD_BOT_OWNER_ID=4242\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNREAD_HOME", str(home))
    monkeypatch.delenv("UNREAD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("UNREAD_BOT_OWNER_ID", raising=False)
    monkeypatch.delenv("UNREAD_BOT_ENV_FILE", raising=False)
    # chmod 600 so _load_dotenv accepts the file (it refuses group/other-readable).
    (home / ".env.bot").chmod(0o600)

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert s.bot.token == "from-env-bot-file"
        assert s.bot.owner_id == 4242
    finally:
        reset_settings()


def test_env_bot_file_explicit_path_wins(monkeypatch, tmp_path):
    """`UNREAD_BOT_ENV_FILE` overrides the canonical / CWD lookups."""
    home = tmp_path / "h"
    home.mkdir()
    (home / ".env.bot").write_text("UNREAD_BOT_TOKEN=from-canonical\n", encoding="utf-8")
    (home / ".env.bot").chmod(0o600)
    elsewhere = tmp_path / "side.env"
    elsewhere.write_text("UNREAD_BOT_TOKEN=from-explicit\n", encoding="utf-8")
    elsewhere.chmod(0o600)

    monkeypatch.setenv("UNREAD_HOME", str(home))
    monkeypatch.setenv("UNREAD_BOT_ENV_FILE", str(elsewhere))
    monkeypatch.delenv("UNREAD_BOT_TOKEN", raising=False)

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert s.bot.token == "from-explicit"
    finally:
        reset_settings()


def test_shell_env_still_beats_env_bot_file(monkeypatch, tmp_path):
    """Shell env always wins — `docker run -e` overrides a mounted file."""
    home = tmp_path / "h"
    home.mkdir()
    (home / ".env.bot").write_text("UNREAD_BOT_TOKEN=from-file\n", encoding="utf-8")
    (home / ".env.bot").chmod(0o600)

    monkeypatch.setenv("UNREAD_HOME", str(home))
    monkeypatch.setenv("UNREAD_BOT_TOKEN", "from-shell-env")
    monkeypatch.delenv("UNREAD_BOT_ENV_FILE", raising=False)

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert s.bot.token == "from-shell-env"
    finally:
        reset_settings()


def test_cmd_bot_run_accepts_env_owner_id_without_session(monkeypatch, tmp_path):
    """Env-var owner_id is enough to get past the startup gate.

    Doesn't actually start the bot — we just need the function to
    progress past the gate and into BotApp construction. We assert by
    monkeypatching BotApp to raise a sentinel so we don't open any
    network connections.
    """
    import asyncio

    import pytest

    from unread.bot import commands as bot_commands

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNREAD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "9999")
    monkeypatch.delenv("UNREAD_BOT_ENV_FILE", raising=False)
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "fresh"))

    class _Sentinel(Exception):
        pass

    class _FakeBotApp:
        def __init__(self, settings):
            raise _Sentinel("reached BotApp")

    monkeypatch.setattr("unread.bot.app.BotApp", _FakeBotApp)
    from unread.config import reset_settings

    reset_settings()
    try:
        with pytest.raises(_Sentinel):
            asyncio.run(bot_commands.cmd_bot_run())
    finally:
        reset_settings()
