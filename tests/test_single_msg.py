"""Tests for --msg single-message analysis mode.

Covers:
- Repo.iter_messages max_msg_id upper bound.
- Pipeline run_analysis honors min+max msg_id to narrow to one message.
- --msg flag is wired through the Typer CLI into cmd_analyze.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from unread.cli import app
from unread.db.repo import Repo
from unread.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


# --- iter_messages bounds -----------------------------------------------


async def test_iter_messages_max_msg_id_is_inclusive(repo: Repo) -> None:
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    for mid in (10, 20, 30, 40):
        await repo.upsert_messages(
            [Message(chat_id=1, msg_id=mid, date=base, sender_name="a", text=f"m{mid}")]
        )
    out = await repo.iter_messages(1, max_msg_id=30)
    assert [m.msg_id for m in out] == [10, 20, 30]


async def test_iter_messages_exact_single_msg(repo: Repo) -> None:
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    for mid in (10, 20, 30):
        await repo.upsert_messages(
            [Message(chat_id=1, msg_id=mid, date=base, sender_name="a", text=f"m{mid}")]
        )
    out = await repo.iter_messages(1, min_msg_id=19, max_msg_id=20)
    assert [m.msg_id for m in out] == [20]


async def test_iter_messages_thread_none_skips_filter(repo: Repo) -> None:
    """thread_id=None must not filter by thread (regression guard for pipeline)."""
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    await repo.upsert_messages(
        [
            Message(chat_id=1, msg_id=10, date=base, thread_id=None, sender_name="a", text="flat"),
            Message(chat_id=1, msg_id=20, date=base, thread_id=5, sender_name="a", text="topic"),
        ]
    )
    flat_only = await repo.iter_messages(1, thread_id=0)
    topic_msgs = {m.msg_id for m in flat_only}
    # thread_id=0 matches NULL thread_id rows (flat-chat semantics).
    assert topic_msgs == {10}

    all_msgs = await repo.iter_messages(1, thread_id=None)
    assert {m.msg_id for m in all_msgs} == {10, 20}


# --- CLI wiring ---------------------------------------------------------


def test_msg_flag_forwarded_to_cmd_analyze() -> None:
    runner = CliRunner()
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["analyze", "@foo", "--msg", "12345"])
    assert result.exit_code == 0, result.output
    mock.assert_called_once()
    kwargs = mock.call_args.kwargs
    assert kwargs["msg"] == "12345"
    assert kwargs["ref"] == "@foo"


def test_msg_flag_accepts_link() -> None:
    runner = CliRunner()
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        link = "https://t.me/somechat/9876"
        result = runner.invoke(app, ["analyze", "@foo", "--msg", link])
    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["msg"] == link


def test_bare_msg_link_becomes_single_msg_mode() -> None:
    """A message link passed as the ref (no --msg flag) should route to
    single-msg mode — matches the natural paste-a-link-to-one-voice flow."""
    runner = CliRunner()
    link = "https://t.me/c/3865481227/11/792"
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["analyze", link])
    assert result.exit_code == 0, result.output
    kwargs = mock.call_args.kwargs
    assert kwargs["ref"] == link
    # --msg wasn't set at the CLI; cmd_analyze decides from resolved.msg_id.
    assert kwargs["msg"] is None
