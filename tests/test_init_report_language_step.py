"""Coverage for the `_run_report_language_step` wizard helper and the
status-panel Languages row added when the report-language picker
landed in the first-run init flow.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Sandbox `UNREAD_HOME` so the wizard writes to a tmp DB. Force the
    interactive-mode predicate to True since pytest runs without a TTY."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "unread"))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("unread.util.prompt._can_interact", lambda: True)
    from unread.config import reset_settings
    from unread.core.paths import ensure_unread_home, storage_dir

    reset_settings()
    ensure_unread_home()
    storage_dir().mkdir(parents=True, exist_ok=True)
    return tmp_path


def _read_app_setting(home: Path, key: str) -> str | None:
    db = home / "unread" / "storage" / "data.sqlite"
    if not db.is_file():
        return None
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _run_report_language_step
# ---------------------------------------------------------------------------


def test_step_skips_when_already_set(isolated_home: Path, monkeypatch) -> None:
    """If `locale.report_language` is already non-empty, the step is a no-op."""
    from unread.config import get_settings, reset_settings
    from unread.tg.commands import _run_report_language_step

    s = get_settings()
    s.locale.report_language = "ja"

    # If the picker were called, the test would fail (no stdin).
    def _boom(*_a, **_kw):
        raise AssertionError("select should not be called when a value is already set")

    monkeypatch.setattr("unread.util.prompt.select", _boom)
    monkeypatch.setattr("unread.util.prompt.ask_text", _boom)

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is False
    assert _read_app_setting(isolated_home, "locale.report_language") is None
    reset_settings()


def test_step_skips_when_persisted_in_db_after_reset(isolated_home: Path, monkeypatch) -> None:
    """A persisted `locale.report_language` should be respected even after
    `reset_settings()` wipes the in-memory singleton — exactly what
    `cmd_init` does between steps. Repro for the bug where re-running
    `unread init` re-prompted for the report language even though the
    user had already picked one on a prior run.
    """
    from unread.config import reset_settings
    from unread.core.paths import default_data_path
    from unread.db.repo import open_repo
    from unread.tg.commands import _run_report_language_step

    async def _persist():
        async with open_repo(default_data_path()) as repo:
            await repo.set_app_setting("locale.report_language", "ru")

    asyncio.run(_persist())

    # Simulate `cmd_init`'s mid-wizard refresh — drop the singleton so the
    # next `get_settings()` reload exercises the same code path the
    # wizard hits between steps.
    reset_settings()

    def _boom(*_a, **_kw):
        raise AssertionError("picker should not fire when `locale.report_language` is already in the DB")

    monkeypatch.setattr("unread.util.prompt.select", _boom)
    monkeypatch.setattr("unread.util.prompt.ask_text", _boom)

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is False
    assert _read_app_setting(isolated_home, "locale.report_language") == "ru"
    reset_settings()


def test_step_persists_picked_popular_code(isolated_home: Path, monkeypatch) -> None:
    """User picks 'pt' from the popular shortlist; value lands in app_settings."""
    from unread.config import reset_settings
    from unread.tg.commands import _run_report_language_step

    monkeypatch.setattr("unread.util.prompt.select", lambda *a, **k: "pt")
    monkeypatch.setattr("unread.util.prompt.ask_text", lambda *a, **k: "")

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is True
    assert _read_app_setting(isolated_home, "locale.report_language") == "pt"
    reset_settings()


def test_step_persists_custom_code(isolated_home: Path, monkeypatch) -> None:
    """User picks 'Custom code…' then types 'pt-BR' → normalized to 'pt'."""
    from unread.config import reset_settings
    from unread.tg.commands import _run_report_language_step

    monkeypatch.setattr("unread.util.prompt.select", lambda *a, **k: "__custom__")
    monkeypatch.setattr("unread.util.prompt.ask_text", lambda *a, **k: "pt-BR")

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is True
    assert _read_app_setting(isolated_home, "locale.report_language") == "pt"
    reset_settings()


def test_step_custom_code_rejects_then_skips(isolated_home: Path, monkeypatch) -> None:
    """First entry is garbage → re-prompt; second entry is empty → skip."""
    from unread.config import reset_settings
    from unread.tg.commands import _run_report_language_step

    answers = iter(["klingon", ""])

    monkeypatch.setattr("unread.util.prompt.select", lambda *a, **k: "__custom__")
    monkeypatch.setattr("unread.util.prompt.ask_text", lambda *a, **k: next(answers))

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is False
    assert _read_app_setting(isolated_home, "locale.report_language") is None
    reset_settings()


def test_step_skip_choice_does_not_persist(isolated_home: Path, monkeypatch) -> None:
    """User selects 'Skip' → no DB write, returns False."""
    from unread.config import reset_settings
    from unread.tg.commands import _run_report_language_step

    monkeypatch.setattr("unread.util.prompt.select", lambda *a, **k: "__skip__")

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is False
    assert _read_app_setting(isolated_home, "locale.report_language") is None
    reset_settings()


def test_step_handles_keyboard_interrupt(isolated_home: Path, monkeypatch) -> None:
    """Ctrl-C / Esc on the picker exits the step cleanly without persisting."""
    from unread.config import reset_settings
    from unread.tg.commands import _run_report_language_step

    def _raise_kbi(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("unread.util.prompt.select", _raise_kbi)

    persisted = asyncio.run(_run_report_language_step())
    assert persisted is False
    assert _read_app_setting(isolated_home, "locale.report_language") is None
    reset_settings()


# ---------------------------------------------------------------------------
# Status panel — Languages row
# ---------------------------------------------------------------------------


def _capture_status() -> str:
    """Run `_print_config_status` and return the captured console output."""
    from unread import cli as cli_module

    with cli_module.console.capture() as cap:
        cli_module._print_config_status()
    return cap.get()


def test_status_panel_shows_languages_row(isolated_home: Path, monkeypatch) -> None:
    """Languages row is always rendered; report falls back to UI when unset."""
    from unread.config import get_settings, reset_settings

    s = get_settings()
    s.locale.language = "en"
    s.locale.report_language = ""
    s.locale.content_language = ""

    out = _capture_status()
    assert "Languages:" in out
    assert "UI en" in out
    # Report falls back to UI language.
    assert "report en" in out
    # Source-language hint is hidden when unset.
    assert "source" not in out
    reset_settings()


def test_status_panel_shows_explicit_report_language(isolated_home: Path, monkeypatch) -> None:
    from unread.config import get_settings, reset_settings

    s = get_settings()
    s.locale.language = "en"
    s.locale.report_language = "pt"
    s.locale.content_language = ""

    out = _capture_status()
    assert "report pt" in out
    assert "UI en" in out
    reset_settings()


def test_status_panel_shows_source_language_when_set(isolated_home: Path, monkeypatch) -> None:
    from unread.config import get_settings, reset_settings

    s = get_settings()
    s.locale.language = "ru"
    s.locale.report_language = "ru"
    s.locale.content_language = "zh"

    out = _capture_status()
    assert "UI ru" in out
    assert "report ru" in out
    assert "source zh" in out
    reset_settings()
