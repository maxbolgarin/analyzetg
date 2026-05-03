"""Pre-prod review batch 4 — assorted hardening pins.

Each test pins one fix from the second wave of code-review work.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------
# AI: 403 classifier distinguishes auth vs content-policy / quota.
# ---------------------------------------------------------------------


def test_403_on_anthropic_is_not_auth_when_class_is_generic():
    """Anthropic returns 403 for *content-policy refusals* — telling
    the user "your key is bad" is the wrong remediation. Only an
    explicit PermissionDeniedError shape is auth."""
    from unread.analyzer.openai_client import _is_auth_error

    class GenericAnthropicError(Exception):
        status_code = 403

    assert _is_auth_error("anthropic", GenericAnthropicError("blocked")) is False


def test_403_on_anthropic_with_permission_denied_class_is_auth():
    from unread.analyzer.openai_client import _is_auth_error

    class PermissionDeniedError(Exception):
        status_code = 403

    assert _is_auth_error("anthropic", PermissionDeniedError("denied")) is True


def test_403_on_openai_is_auth():
    """OpenAI 403 = bad key (PermissionDeniedError). Stays as auth."""
    from unread.analyzer.openai_client import _is_auth_error

    class PermissionDeniedError(Exception):
        status_code = 403

    assert _is_auth_error("openai", PermissionDeniedError("denied")) is True


def test_401_is_always_auth():
    from unread.analyzer.openai_client import _is_auth_error

    class Whatever(Exception):
        status_code = 401

    assert _is_auth_error("anthropic", Whatever()) is True
    assert _is_auth_error("google", Whatever()) is True
    assert _is_auth_error("openai", Whatever()) is True


# ---------------------------------------------------------------------
# Chunker: oversized message warns once and proceeds.
# ---------------------------------------------------------------------


def test_chunker_warns_once_on_message_exceeds_budget(capsys):
    """A single message larger than the chunk body budget gets a
    one-shot warning so the operator sees it, instead of debugging a
    mysterious "prompt is too long" 4xx from the provider later.

    structlog writes via PrintLogger to stdout, so capsys captures it
    even though pytest's caplog only sees stdlib logging records.
    """
    from unread.analyzer.chunker import build_chunks
    from unread.models import Message

    msgs = [
        Message(
            chat_id=-1,
            msg_id=42,
            date=datetime(2026, 4, 24, 12, 0),
            text="x" * 50_000,
            sender_name="A",
        ),
    ]
    with patch("unread.analyzer.chunker.count_tokens", return_value=10_000):
        chunks = build_chunks(
            msgs,
            model="gpt-4o-mini",
            system_prompt="",
            user_overhead="",
            output_budget=0,
            safety_margin=0,
            max_chunk_input_tokens=2000,
        )
    assert chunks, "chunker should still emit a chunk so caller sees the failure surface"
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "message_exceeds_budget" in out


# ---------------------------------------------------------------------
# Files: extract_text refuses oversize files.
# ---------------------------------------------------------------------


def test_extract_text_refuses_oversize(tmp_path: Path, monkeypatch):
    """A 1+ GB log file must raise instead of being slurped into RAM."""
    import unread.files.extractors as ex

    f = tmp_path / "big.log"
    f.write_bytes(b"x")
    # Patch the cap down so we don't have to write 100 MB in a test.
    monkeypatch.setattr(ex, "_MAX_EXTRACT_BYTES", 0)
    with pytest.raises(ValueError, match="too large"):
        ex.extract_text(f)


def test_extract_text_under_cap_works(tmp_path: Path):
    """Sanity: a normal small file still passes the cap and decodes."""
    from unread.files.extractors import extract_text

    f = tmp_path / "small.txt"
    f.write_text("hello\n", encoding="utf-8")
    result = extract_text(f)
    assert result.text == "hello\n"


# ---------------------------------------------------------------------
# Diagnostics: build_bug_report survives `typer.Exit(1)` from doctor.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_bug_report_survives_typer_exit(monkeypatch):
    """A failing doctor (typer.Exit / click.exceptions.Exit) is the
    most useful bug report. The bundle must complete regardless."""
    import typer

    from unread import diagnostics

    async def fake_doctor():
        raise typer.Exit(1)

    monkeypatch.setattr("unread.tg.commands.cmd_doctor", fake_doctor)
    out = await diagnostics.build_bug_report()
    assert isinstance(out, str)
    assert len(out) > 0


# ---------------------------------------------------------------------
# Mark-read: deterministic across re-runs.
# ---------------------------------------------------------------------


def test_mark_read_uses_repo_max_id_not_pool_max():
    """Pre-prod review: previously max(prior_pool.msg_id) made the
    marker drift across re-runs because the pool composition depended
    on retrieval scoring. Now uses repo.get_max_msg_id deterministically.
    Verify by source-grep so a future change to the comment doesn't
    silently regress the behavior."""
    src = Path(__file__).resolve().parent.parent / "unread" / "ask" / "commands.py"
    text = src.read_text(encoding="utf-8")
    # The deterministic call path:
    assert "max_id = await repo.get_max_msg_id(target_chat" in text
    # The old pool-max line should be gone:
    assert "max_id = max((m.msg_id for m, _ in prior_pool)" not in text


# ---------------------------------------------------------------------
# Crypto: _persist_upgrade is atomic (single transaction).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_upgrade_writes_salt_and_secrets_atomically(tmp_path: Path):
    """All three writes (salt, ciphertext, backend flag) commit
    together. Verify by running the helper end-to-end and checking
    the resulting DB has all three rows."""
    from unread.security.commands import _persist_upgrade

    db = tmp_path / "data.sqlite"
    await _persist_upgrade(
        db,
        salt=b"\x09" * 16,
        encrypted={"openai.api_key": "$u2$" + "A" * 64},
        target_backend="passphrase",
    )

    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
        assert rows.get("security.kdf_salt"), "salt should be persisted"
        assert rows.get("secrets.backend") == "passphrase", "backend should be flipped"
        secret_rows = dict(conn.execute("SELECT key, value FROM secrets").fetchall())
        assert secret_rows.get("openai.api_key", "").startswith("$u2$")
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Anthropic adapter: SDK retries off, max_retries=0.
# ---------------------------------------------------------------------


def test_anthropic_provider_disables_sdk_retries(monkeypatch):
    """Pre-prod review: SDK retries were opaque to the user. The
    adapter constructor now passes `max_retries=0` so we own the
    retry loop and can surface the yellow status line."""
    from unread.ai.anthropic_provider import AnthropicProvider

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = SimpleNamespace(create=lambda **_kw: None)

    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeAnthropic)
    settings = SimpleNamespace(
        anthropic=SimpleNamespace(api_key="sk-ant-test"),
        openai=SimpleNamespace(request_timeout_sec=60, max_retries=3),
    )
    p = AnthropicProvider(settings)
    assert p._client.kwargs["max_retries"] == 0


# ---------------------------------------------------------------------
# Presets: prompt-injection clause present in base prompts.
# ---------------------------------------------------------------------


def test_base_prompts_contain_injection_clause():
    """Both the EN and RU base prompts must instruct the model to
    treat message bodies as untrusted data, not as instructions."""
    repo_root = Path(__file__).resolve().parent.parent
    en = (repo_root / "presets" / "en" / "_base.md").read_text(encoding="utf-8").lower()
    ru = (repo_root / "presets" / "ru" / "_base.md").read_text(encoding="utf-8").lower()
    assert "untrusted" in en
    assert "недовер" in ru  # "недоверенные" / "недоверенный"
