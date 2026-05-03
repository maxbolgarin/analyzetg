"""Pre-prod review: secret env vars must not leak into unrelated
subprocesses (ffmpeg, fdesetup, package manager).
"""

from __future__ import annotations

import os

from unread.util.subprocess_env import clean_subprocess_env


def test_clean_env_strips_known_secret_names(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaTest")
    monkeypatch.setenv("UNREAD_PASSPHRASE", "supersecret")
    monkeypatch.setenv("TELEGRAM_API_HASH", "fakehash")
    env = clean_subprocess_env()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "UNREAD_PASSPHRASE" not in env
    assert "TELEGRAM_API_HASH" not in env


def test_clean_env_keeps_other_vars(monkeypatch):
    """Non-secret vars (PATH, LANG, etc.) survive — the helper only
    drops known-secret names so subprocesses still find their tools."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = clean_subprocess_env()
    assert env.get("PATH") == os.environ["PATH"]


def test_clean_env_extra_drop(monkeypatch):
    """Caller can blocklist additional names without editing the helper."""
    monkeypatch.setenv("CUSTOM_SECRET", "x")
    env = clean_subprocess_env(extra_drop=frozenset({"CUSTOM_SECRET"}))
    assert "CUSTOM_SECRET" not in env
