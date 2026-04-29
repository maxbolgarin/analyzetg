"""`unread bug-report` redacts every secret-shaped value before printing.

The bundle is meant to be pasted into public GitHub issues — a single
leak (api_id, api_key, api_hash) makes the whole feature dangerous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.diagnostics import (
    build_bug_report,
    redact_config_file,
    redact_text,
)


def test_redact_text_replaces_known_shapes():
    """API-key prefixes are masked even when they appear in free text."""
    leak = (
        "OpenAI: sk-proj-abc123def456ghi789jkl012mno345 "
        "Anthropic: sk-ant-AAA111BBB222CCC333DDD444EEE555 "
        "Google: AIzaSyA1234567890abcdefghijklmnopqrstuvw "
        "TG hash: abcdef0123456789abcdef0123456789"
    )
    cleaned = redact_text(leak)
    assert "sk-proj-abc" not in cleaned
    assert "sk-ant-AAA" not in cleaned
    assert "AIzaSyA12" not in cleaned
    assert "abcdef0123456789abcdef0123456789" not in cleaned
    assert "redacted" in cleaned.lower()


def test_redact_config_file_masks_known_keys(tmp_path: Path):
    """`api_key` / `api_id` / `api_hash` lines have their value replaced."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[telegram]\n"
        "api_id = 12345678\n"
        'api_hash = "abcdef0123456789abcdef0123456789"\n'
        "\n"
        "[openai]\n"
        'api_key = "sk-proj-abc123def456ghi789"\n'
    )
    out = redact_config_file(cfg)
    assert "12345678" not in out
    assert "abcdef0123456789abcdef0123456789" not in out
    assert "sk-proj-abc" not in out
    # The keys themselves stay visible — only values redacted.
    assert "api_id" in out
    assert "api_hash" in out
    assert "api_key" in out


def test_redact_config_file_handles_missing(tmp_path: Path):
    """A non-existent path returns a placeholder line, not a traceback."""
    out = redact_config_file(tmp_path / "nope.toml")
    assert "not present" in out


@pytest.mark.asyncio
async def test_full_bundle_contains_no_secret_shapes(tmp_path: Path, monkeypatch):
    """End-to-end: build the full bundle on a fixture install and grep
    for any secret-shaped string. The bundle reads doctor + config +
    .env, so this is the strongest leak guard we can run offline."""
    install = tmp_path / "unread"
    install.mkdir()
    (install / ".env").write_text(
        'OPENAI_API_KEY="sk-proj-leakcheck111aaa222bbb333ccc444dddee"\n'
        "TELEGRAM_API_ID=99999999\n"
        "TELEGRAM_API_HASH=fedcba9876543210fedcba9876543210\n"
    )
    (install / "config.toml").write_text('[anthropic]\napi_key = "sk-ant-LEAKCHECKAAAAAAAAAAAAAAAAAAAAAA"\n')
    monkeypatch.setenv("UNREAD_HOME", str(install))
    # Force the singleton to re-resolve paths under the new HOME.
    from unread.config import reset_settings

    reset_settings()

    text = await build_bug_report()
    # No raw secret-shape leaks past the redactor.
    forbidden = (
        "sk-proj-leakcheck",
        "sk-ant-LEAKCHECK",
        "fedcba9876543210fedcba9876543210",
    )
    for pat in forbidden:
        assert pat not in text, f"secret leaked: {pat!r}"
    # Bundle should still mention the version + structural sections so
    # a recipient knows the redaction didn't blank the whole report.
    assert "unread version" in text
    assert "## doctor" in text
    assert "## config.toml (redacted)" in text
    assert "## .env (redacted)" in text
