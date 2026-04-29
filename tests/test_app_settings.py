"""Persistent app_settings table + open_repo overlay."""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.config import get_settings, reset_settings
from unread.db.repo import Repo, open_repo


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_app_settings_round_trip(repo: Repo) -> None:
    assert await repo.get_app_setting("locale.language") is None
    await repo.set_app_setting("locale.language", "ru")
    assert await repo.get_app_setting("locale.language") == "ru"
    rows = await repo.get_all_app_settings()
    assert rows == {"locale.language": "ru"}


async def test_app_settings_delete(repo: Repo) -> None:
    await repo.set_app_setting("locale.language", "ru")
    assert await repo.delete_app_setting("locale.language") is True
    assert await repo.get_app_setting("locale.language") is None
    # Deleting a missing key is not an error; returns False.
    assert await repo.delete_app_setting("locale.language") is False


async def test_app_settings_clear_all(repo: Repo) -> None:
    await repo.set_app_setting("locale.language", "ru")
    await repo.set_app_setting("openai.audio_language", "ru")
    n = await repo.clear_all_app_settings()
    assert n == 2
    assert await repo.get_all_app_settings() == {}


async def test_app_settings_overrides_apply_via_open_repo(tmp_path: Path) -> None:
    """`open_repo` must apply DB-stored overrides to the live settings
    singleton so any command opening a repo gets them automatically."""
    db = tmp_path / "t.sqlite"
    setup = await Repo.open(db)
    await setup.set_app_setting("locale.language", "ru")
    await setup.set_app_setting("locale.content_language", "ru")
    await setup.close()

    reset_settings()
    pre = get_settings()
    assert pre.locale.language == "en"  # config default
    assert pre.locale.content_language == ""

    async with open_repo(db):
        s = get_settings()
        assert s.locale.language == "ru"
        assert s.locale.content_language == "ru"
    reset_settings()


async def test_app_settings_audio_empty_string_means_autodetect(tmp_path: Path) -> None:
    """Saving `openai.audio_language` as an empty string must apply as
    `None` on the live settings (Whisper autodetect contract)."""
    db = tmp_path / "t.sqlite"
    setup = await Repo.open(db)
    await setup.set_app_setting("openai.audio_language", "")
    await setup.close()

    reset_settings()
    async with open_repo(db):
        s = get_settings()
        assert s.openai.audio_language is None
    reset_settings()


async def test_app_settings_plain_citations_overlay(tmp_path: Path) -> None:
    """`analyze.plain_citations` round-trips through the app_settings
    overlay so users on terminals without OSC 8 can persist the flag
    once via `unread settings set ...` instead of passing it every run."""
    db = tmp_path / "t.sqlite"
    setup = await Repo.open(db)
    await setup.set_app_setting("analyze.plain_citations", "1")
    await setup.close()

    reset_settings()
    pre = get_settings()
    assert pre.analyze.plain_citations is False  # config default

    async with open_repo(db):
        s = get_settings()
        assert s.analyze.plain_citations is True
    reset_settings()


async def test_app_settings_unknown_keys_ignored_by_overlay(tmp_path: Path) -> None:
    """Allow-list keeps the overlay tight: keys outside `_OVERRIDE_KEYS`
    are stored but don't mutate live settings."""
    db = tmp_path / "t.sqlite"
    setup = await Repo.open(db)
    await setup.set_app_setting("not.a.real.key", "whatever")
    await setup.close()

    reset_settings()
    async with open_repo(db):
        s = get_settings()
        # Default unchanged.
        assert s.locale.language == "en"
    reset_settings()
