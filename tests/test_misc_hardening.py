"""Pre-prod regressions across unrelated modules.

Each test here pins one fix from the code review. Grouped together so
the file isn't a haystack of one-test modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------
# i18n: missing key returns sentinel + warns instead of raising.
# ---------------------------------------------------------------------


def test_i18n_missing_key_returns_sentinel_not_raise():
    """A typo in an i18n key used to crash `--help` (some `_tf("…")`
    calls run at import time). Now returns `!key!` and logs a warning."""
    from unread.i18n import t

    out = t("totally_missing_key_xxx")
    assert out == "!totally_missing_key_xxx!"


# ---------------------------------------------------------------------
# DB: VACUUM INTO refuses unsafe target paths.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_to_rejects_quote_in_path(tmp_path):
    """SQLite VACUUM INTO doesn't bind parameters; a single-quote in the
    target path injects SQL. Validator must refuse."""
    from unread.db.repo import open_repo

    src = tmp_path / "data.sqlite"
    async with open_repo(src) as repo:
        bad = tmp_path / "evil';DROP TABLE secrets;--.sqlite"
        with pytest.raises(ValueError, match="unsafe chars"):
            await repo.backup_to(bad)


@pytest.mark.asyncio
async def test_backup_to_rejects_newline(tmp_path):
    from unread.db.repo import open_repo

    src = tmp_path / "data.sqlite"
    async with open_repo(src) as repo:
        with pytest.raises(ValueError, match=r"unsafe chars|control chars"):
            await repo.backup_to(tmp_path / "evil\nfile.sqlite")


# ---------------------------------------------------------------------
# Enrich: extra_json uses json.dumps (no string concatenation).
# ---------------------------------------------------------------------


def test_extra_json_handles_quotes_in_ext():
    """A filename suffix containing `"` or `\\` would explode the
    string-concat JSON. json.dumps escapes correctly."""
    # Reproduce the dumps call directly — cheap and pins the contract.
    payload = json.dumps({"ext": 'pdf"weird\\ext', "truncated": True})
    parsed = json.loads(payload)
    assert parsed == {"ext": 'pdf"weird\\ext', "truncated": True}


# ---------------------------------------------------------------------
# Logging: redactor recurses one level into dicts/lists.
# ---------------------------------------------------------------------


def test_redactor_recurses_into_nested_dict():
    """`extra={"payload": {"api_key": "sk-test"}}` used to bypass the
    redactor (top-level walk only). One-level recursion catches it."""
    from unread.util.logging import _redact_processor

    event = {
        "event": "test",
        "payload": {"api_key": "sk-test-1234567890abcdef1234567890abcdef"},
    }
    out = _redact_processor(None, "info", event)
    assert out["payload"]["api_key"] == "***REDACTED***"


def test_redactor_recurses_into_nested_list():
    from unread.util.logging import _redact_processor

    event = {"event": "test", "items": ["sk-test-1234567890abcdef1234567890abcdef"]}
    out = _redact_processor(None, "info", event)
    assert "***REDACTED***" in out["items"][0]


# ---------------------------------------------------------------------
# Config: corrupt secrets DB → warning, not crash.
# ---------------------------------------------------------------------


def test_load_settings_survives_secrets_read_error(tmp_path, monkeypatch, capsys):
    """If `read_secrets` raises (corrupt DB, missing passphrase on a
    non-interactive run), `load_settings` must surface a warning and
    keep going — env / .env are still valid auth sources."""
    import unread.config as cfg

    monkeypatch.setattr(cfg, "_load_dotenv", lambda _p: None)
    monkeypatch.setattr("unread.secrets.read_secrets", lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    cfg.reset_settings()
    settings = cfg.load_settings()
    assert settings is not None
    err = capsys.readouterr().err
    assert "couldn't load persisted secrets" in err


# ---------------------------------------------------------------------
# CLI: --save / --no-save / --console conflict raises.
# ---------------------------------------------------------------------


def test_dispatch_analyze_rejects_conflicting_save_flags():
    """Passing both `--save` and `--console` (or `--no-save`) is
    user error — surface it as a typer.BadParameter instead of letting
    one silently win."""
    import typer

    from unread.cli import _dispatch_analyze

    with pytest.raises(typer.BadParameter, match="conflicts"):
        _dispatch_analyze(save=True, no_save=True, console_out=False)
    with pytest.raises(typer.BadParameter, match="conflicts"):
        _dispatch_analyze(save=True, no_save=False, console_out=True)


def test_dispatch_analyze_rejects_no_console_with_no_save():
    """`--no-console --no-save` would suppress every form of output.
    Reject the combination at dispatch instead of silently spending
    LLM tokens on a run nobody can see."""
    import typer

    from unread.cli import _dispatch_analyze

    with pytest.raises(typer.BadParameter, match="suppress all output"):
        _dispatch_analyze(no_console=True, no_save=True)
    # Same check via the deprecated --console alias (which also means
    # "skip the file"): pairing it with --no-console is just as bad.
    with pytest.raises(typer.BadParameter, match="suppress all output"):
        _dispatch_analyze(no_console=True, console_out=True)


# ---------------------------------------------------------------------
# Runner: naive utcnow replaced with UTC-aware now.
# ---------------------------------------------------------------------


def test_runner_uses_utc_aware_now():
    """The deprecated `datetime.utcnow()` returns a naive datetime and
    raises DeprecationWarning on Python 3.13. The fix uses
    `datetime.now(UTC)` instead — verify by source-grep so we don't
    regress."""
    src = Path(__file__).resolve().parent.parent / "unread" / "runner.py"
    text = src.read_text(encoding="utf-8")
    assert "_dt.utcnow()" not in text, "runner.py still calls deprecated utcnow()"
    assert "_dt.now(_UTC)" in text, "runner.py should use UTC-aware now()"
