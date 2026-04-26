"""Mutual-exclusion: <ref> vs --chat vs --folder vs --global."""

from __future__ import annotations

import pytest
import typer

from analyzetg.ask.commands import _validate_scope_args


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
