"""Security defenses on `_load_dotenv`.

The loader is a hostile-input boundary on shared hosts: an attacker who
can swap `~/.unread/.env` for a symlink, or who can read it because of
loose permissions, can either redirect the loader to attacker-controlled
content or exfiltrate the user's API keys. These tests pin the
defenses added in the pre-prod review.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from unread.config import _load_dotenv


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    # Each test sets keys that begin with TEST_DOTENV_; clean them
    # explicitly to guarantee no leakage between tests in this module.
    for k in list(os.environ):
        if k.startswith("TEST_DOTENV_"):
            monkeypatch.delenv(k, raising=False)


def _write_env(tmp_path: Path, content: str, mode: int = 0o600) -> Path:
    p = tmp_path / ".env"
    p.write_text(content)
    p.chmod(mode)
    return p


def test_load_dotenv_loads_well_formed_file(tmp_path):
    # Pre-prod: loader returns a dict and never mutates os.environ.
    p = _write_env(tmp_path, "TEST_DOTENV_KEY=value\n")
    out = _load_dotenv(p)
    assert out.get("TEST_DOTENV_KEY") == "value"
    assert "TEST_DOTENV_KEY" not in os.environ


def test_load_dotenv_strips_crlf(tmp_path):
    # CRLF-saved files leave a trailing \r in API keys → 401, then any
    # downstream traceback prints the value. Loader must strip it.
    p = _write_env(tmp_path, 'TEST_DOTENV_KEY="abc"\r\n')
    out = _load_dotenv(p)
    assert out.get("TEST_DOTENV_KEY") == "abc"


def test_load_dotenv_strips_unquoted_crlf(tmp_path):
    p = _write_env(tmp_path, "TEST_DOTENV_KEY=abc\r\n")
    out = _load_dotenv(p)
    assert out.get("TEST_DOTENV_KEY") == "abc"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_load_dotenv_refuses_world_readable(tmp_path, capsys):
    """A 0644 .env is the user's secret leaking to other local users.
    Loader refuses and warns instead of silently consuming."""
    p = _write_env(tmp_path, "TEST_DOTENV_KEY=value\n", mode=0o644)
    _load_dotenv(p)
    assert "TEST_DOTENV_KEY" not in os.environ
    err = capsys.readouterr().err
    assert "chmod 600" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_load_dotenv_refuses_group_readable(tmp_path, capsys):
    p = _write_env(tmp_path, "TEST_DOTENV_KEY=value\n", mode=0o640)
    _load_dotenv(p)
    assert "TEST_DOTENV_KEY" not in os.environ
    assert "chmod 600" in capsys.readouterr().err


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="O_NOFOLLOW not available on this platform",
)
def test_load_dotenv_refuses_symlink(tmp_path, capsys):
    """A symlink swap on a shared host can redirect reads to attacker-
    controlled content. With O_NOFOLLOW the open errors with ELOOP and
    we surface it instead of following blindly."""
    real = tmp_path / "real.env"
    real.write_text("TEST_DOTENV_KEY=secret\n")
    real.chmod(0o600)
    link = tmp_path / ".env"
    link.symlink_to(real)
    _load_dotenv(link)
    assert "TEST_DOTENV_KEY" not in os.environ
    assert "refusing to load" in capsys.readouterr().err


def test_load_dotenv_silent_when_file_absent(tmp_path):
    # No file → no warning, no environ change. Existing callers depend
    # on this for "user deleted .env after `unread init`".
    _load_dotenv(tmp_path / "nonexistent.env")
    assert "TEST_DOTENV_KEY" not in os.environ
