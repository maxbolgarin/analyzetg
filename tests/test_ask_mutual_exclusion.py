"""Mutual-exclusion: <ref> vs --chat vs --folder vs --global, plus fail-fast
checks for flags that the wizard route can't honour."""

from __future__ import annotations

import pytest
import typer

from analyzetg.ask.commands import _validate_scope_args, cmd_ask


def test_ref_and_chat_both_set_raises():
    with pytest.raises(typer.BadParameter, match=r"ref.*chat"):
        _validate_scope_args(ref="@x", chat="@y", folder=None, global_scope=False)


def test_ref_and_global_both_set_raises():
    with pytest.raises(typer.BadParameter, match=r"ref.*global"):
        _validate_scope_args(ref="@x", chat=None, folder=None, global_scope=True)


def test_chat_and_folder_both_set_raises():
    with pytest.raises(typer.BadParameter, match=r"chat.*folder"):
        _validate_scope_args(ref=None, chat="@x", folder="Work", global_scope=False)


def test_folder_and_global_both_set_raises():
    with pytest.raises(typer.BadParameter, match=r"folder.*global"):
        _validate_scope_args(ref=None, chat=None, folder="Work", global_scope=True)


def test_all_none_is_ok():
    _validate_scope_args(ref=None, chat=None, folder=None, global_scope=False)


def test_only_global_is_ok():
    _validate_scope_args(ref=None, chat=None, folder=None, global_scope=True)


def test_only_ref_is_ok():
    _validate_scope_args(ref="@x", chat=None, folder=None, global_scope=False)


# --- Fail-fast: --refresh / --build-index without scope ---------------------


@pytest.mark.asyncio
async def test_refresh_without_scope_raises_before_wizard():
    """`atg ask --refresh` (bare) used to drop into the wizard, then fail at
    the end if the user picked ALL_LOCAL. Fail-fast keeps that wasted run
    from happening."""
    with pytest.raises(typer.BadParameter, match=r"--refresh requires"):
        await cmd_ask(
            question="Q",
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
            refresh=True,
        )


@pytest.mark.asyncio
async def test_build_index_without_scope_raises_before_wizard():
    with pytest.raises(typer.BadParameter, match=r"--build-index requires"):
        await cmd_ask(
            question="Q",
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
            build_index=True,
        )


@pytest.mark.asyncio
async def test_refresh_with_global_still_raises():
    """--refresh + --global: the new check rejects this even though
    `--global` is technically a scope; we don't backfill all dialogs."""
    with pytest.raises(typer.BadParameter, match=r"--refresh requires"):
        await cmd_ask(
            question="Q",
            ref=None,
            chat=None,
            folder=None,
            global_scope=True,
            refresh=True,
        )


# --- Fail-fast: period / thread / with-comments without question/scope ------


@pytest.mark.asyncio
async def test_last_days_without_question_or_scope_raises():
    """Period flags would be silently dropped by the wizard route — fail-fast."""
    with pytest.raises(typer.BadParameter, match=r"--since/--until/--last-days"):
        await cmd_ask(
            question=None,
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
            last_days=7,
        )


@pytest.mark.asyncio
async def test_thread_without_question_or_scope_raises():
    with pytest.raises(typer.BadParameter, match=r"--thread"):
        await cmd_ask(
            question=None,
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
            thread=5,
        )


@pytest.mark.asyncio
async def test_with_comments_without_question_or_scope_raises():
    with pytest.raises(typer.BadParameter, match=r"--with-comments"):
        await cmd_ask(
            question=None,
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
            with_comments=True,
        )
