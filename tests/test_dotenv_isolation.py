"""`.env` values do NOT leak into `os.environ` or subprocess inheritance.

Pre-prod review: `_load_dotenv` used to mutate `os.environ` directly,
which meant every subsequent `subprocess.run` (ffmpeg, fdesetup,
package manager) inherited the user's API keys via the child env. The
fix returns a dict from `_load_dotenv` that `load_settings` consumes
internally — the values flow into the typed `Settings` but never into
the process environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from unread.config import _load_dotenv, dotenv_value, load_settings, reset_settings
from unread.util.fsmode import secret_write_text


def _write_env(path: Path, body: str) -> None:
    secret_write_text(path, body)


def test_load_dotenv_returns_dict_does_not_mutate_environ(tmp_path, monkeypatch):
    env_path = tmp_path / "test.env"
    _write_env(
        env_path,
        "FAKE_KEY=fake-value-1\nOTHER_FAKE=other-value-2\n# comment line\nQUOTED='single-quoted'\n",
    )
    # Make sure nothing pre-set leaks into the assertion.
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.delenv("OTHER_FAKE", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    out = _load_dotenv(env_path)

    assert out == {
        "FAKE_KEY": "fake-value-1",
        "OTHER_FAKE": "other-value-2",
        "QUOTED": "single-quoted",
    }
    # Critical assertion: nothing leaked into the process environment.
    assert "FAKE_KEY" not in os.environ
    assert "OTHER_FAKE" not in os.environ
    assert "QUOTED" not in os.environ


def test_load_settings_overlays_dotenv_into_settings_not_environ(tmp_path, monkeypatch):
    """A `.env` with provider keys flows into `Settings.<provider>.api_key`
    without ever touching `os.environ`."""
    home = tmp_path / "home"
    home.mkdir()
    env_path = home / ".env"
    _write_env(
        env_path,
        "OPENAI_API_KEY=sk-from-dotenv-only\n"
        "ANTHROPIC_API_KEY=sk-ant-from-dotenv-only\n"
        "GOOGLE_API_KEY=AI-from-dotenv-only\n"
        "OPENROUTER_API_KEY=sk-or-from-dotenv-only\n",
    )
    monkeypatch.setenv("UNREAD_HOME", str(home))
    # Wipe any process-env keys so the .env values are the only source.
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    reset_settings()
    settings = load_settings()

    # The typed settings carry the values…
    assert settings.openai.api_key == "sk-from-dotenv-only"
    assert settings.anthropic.api_key == "sk-ant-from-dotenv-only"
    assert settings.google.api_key == "AI-from-dotenv-only"
    assert settings.openrouter.api_key == "sk-or-from-dotenv-only"
    # …but `os.environ` does NOT — the subprocess inheritance vector
    # is closed.
    assert "OPENAI_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "GOOGLE_API_KEY" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ


def test_dotenv_value_helper_returns_overlay_lookups(tmp_path, monkeypatch):
    """`dotenv_value()` exposes the cached overlay for callers that
    can't or shouldn't go through Settings (e.g. the passphrase reader)."""
    home = tmp_path / "home"
    home.mkdir()
    env_path = home / ".env"
    _write_env(env_path, "UNREAD_PASSPHRASE=secret-from-dotenv-2\n")
    monkeypatch.setenv("UNREAD_HOME", str(home))
    monkeypatch.delenv("UNREAD_PASSPHRASE", raising=False)

    reset_settings()
    load_settings()  # populates the overlay

    assert dotenv_value("UNREAD_PASSPHRASE") == "secret-from-dotenv-2"
    assert dotenv_value("NON_EXISTENT_KEY") is None
    assert "UNREAD_PASSPHRASE" not in os.environ


def test_shell_env_wins_over_dotenv(tmp_path, monkeypatch):
    """Real shell env always wins over the .env overlay."""
    home = tmp_path / "home"
    home.mkdir()
    env_path = home / ".env"
    _write_env(env_path, "OPENAI_API_KEY=sk-from-dotenv\n")
    monkeypatch.setenv("UNREAD_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell-wins")

    reset_settings()
    settings = load_settings()
    assert settings.openai.api_key == "sk-from-shell-wins"
