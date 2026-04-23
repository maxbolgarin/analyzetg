"""Regression: when a bare word matches the username regex but isn't
a real @username on Telegram, resolve() must fall through to fuzzy rather
than crash with UsernameNotOccupiedError.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from analyzetg.db.repo import Repo
from analyzetg.tg import resolver as resolver_mod
from analyzetg.tg.resolver import resolve


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_unknown_username_falls_through_to_fuzzy(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ref that looks like a username but no such @ exists → fuzzy search."""
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError('No user has "union" as username'))

    fake_entity = SimpleNamespace(id=1234, title="Union Workspace", username=None)

    async def fake_iter_dialogs(limit=None):
        yield SimpleNamespace(entity=fake_entity)

    client.iter_dialogs = fake_iter_dialogs

    # Stub the Telethon-typed helpers so the fuzzy path can operate on our
    # SimpleNamespace entity without ripping in the whole Telethon type stack.
    monkeypatch.setattr(resolver_mod, "entity_id", lambda e: e.id)
    monkeypatch.setattr(resolver_mod, "entity_title", lambda e: e.title)
    monkeypatch.setattr(resolver_mod, "entity_username", lambda e: e.username)
    monkeypatch.setattr(resolver_mod, "_chat_kind", lambda e: "group")

    resolved = await resolve(client, repo, "union")
    assert resolved.title == "Union Workspace"
    assert resolved.chat_id == 1234
