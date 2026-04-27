"""Tests for per-subscription `atg run` settings and the runner's
resolve helpers.

Covers:
- Subscription round-trips preset / period / enrich_kinds / mark_read /
  post_to through SQLite (additive migration + upsert + list).
- runner._resolve_period: override > stored > default.
- runner._resolve_enrich: --no-enrich > --enrich-all > --enrich CSV >
  stored; empty stored string means "explicitly disabled".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atg.db.repo import Repo
from atg.models import Subscription
from atg.runner import _resolve_enrich, _resolve_period


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_subscription_round_trips_run_settings(repo: Repo) -> None:
    """upsert + list returns the same preset/period/enrich/mark_read/post_to."""
    sub = Subscription(
        chat_id=-100123,
        thread_id=0,
        title="My Channel",
        source_kind="channel",
        preset="action_items",
        period="last7",
        enrich_kinds="voice,link",
        mark_read=False,
        post_to="me",
    )
    await repo.upsert_subscription(sub)
    out = await repo.list_subscriptions(enabled_only=False)
    assert len(out) == 1
    got = out[0]
    assert got.preset == "action_items"
    assert got.period == "last7"
    assert got.enrich_kinds == "voice,link"
    assert got.mark_read is False
    assert got.post_to == "me"


async def test_subscription_defaults_when_unset(repo: Repo) -> None:
    """Constructing without the new fields yields the documented defaults
    after a round-trip — important for older subs that pre-date the
    feature (additive migration sets DEFAULT 'summary' / 'unread' / 1)."""
    sub = Subscription(chat_id=-100456, thread_id=0, title="X", source_kind="chat")
    await repo.upsert_subscription(sub)
    got = (await repo.list_subscriptions())[0]
    assert got.preset == "summary"
    assert got.period == "unread"
    assert got.enrich_kinds is None  # NULL = "use config defaults"
    assert got.mark_read is True
    assert got.post_to is None


def test_resolve_period_override_beats_stored() -> None:
    assert _resolve_period("unread", "last30") == "last30"
    assert _resolve_period("last7", None) == "last7"
    # Empty stored AND no override → fall back to "unread".
    assert _resolve_period("", None) == "unread"


def test_resolve_enrich_precedence() -> None:
    """Order: --no-enrich > --enrich-all > --enrich > stored > config defaults."""
    # Override --no-enrich wins over everything.
    out = _resolve_enrich("voice,link", override="image", override_all=True, override_none=True)
    assert out == {"enrich": None, "enrich_all": False, "no_enrich": True}

    # --enrich-all wins over a stored CSV.
    out = _resolve_enrich("voice,link", override=None, override_all=True, override_none=False)
    assert out["enrich_all"] is True
    assert out["no_enrich"] is False

    # CLI --enrich CSV wins over stored.
    out = _resolve_enrich("voice,link", override="image", override_all=False, override_none=False)
    assert out["enrich"] == "image"

    # Stored CSV used when no override.
    out = _resolve_enrich("voice,link", override=None, override_all=False, override_none=False)
    assert out["enrich"] == "voice,link"

    # Empty-string stored = explicitly off, regardless of CLI silence.
    out = _resolve_enrich("", override=None, override_all=False, override_none=False)
    assert out == {"enrich": None, "enrich_all": False, "no_enrich": True}

    # NULL stored + no override = use config defaults (cmd_analyze sees
    # all-False flags and reads `[enrich]` from settings).
    out = _resolve_enrich(None, override=None, override_all=False, override_none=False)
    assert out == {"enrich": None, "enrich_all": False, "no_enrich": False}
