"""Tests for `unread.bot.session_upload` validators and metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from unread.bot import session_upload


@dataclass
class _Attr:
    file_name: str | None = None


@dataclass
class _Doc:
    mime_type: str = ""
    size: int | None = None
    attributes: list[_Attr] = field(default_factory=list)


@dataclass
class _Media:
    document: Any = None


@dataclass
class _Msg:
    media: Any = None


@dataclass
class _Event:
    message: _Msg


def test_name_of_attachment_returns_filename():
    ev = _Event(_Msg(media=_Media(document=_Doc(attributes=[_Attr(file_name="session.sqlite")]))))
    assert session_upload._name_of_attachment(ev) == "session.sqlite"


def test_name_of_attachment_empty_when_no_media():
    ev = _Event(_Msg(media=None))
    assert session_upload._name_of_attachment(ev) == ""


def test_name_of_attachment_empty_when_no_filename_attr():
    ev = _Event(_Msg(media=_Media(document=_Doc(attributes=[]))))
    assert session_upload._name_of_attachment(ev) == ""


def test_size_of_attachment_reads_document_size():
    ev = _Event(_Msg(media=_Media(document=_Doc(size=12345))))
    assert session_upload._size_of_attachment(ev) == 12345


def test_size_of_attachment_none_when_no_media():
    ev = _Event(_Msg(media=None))
    assert session_upload._size_of_attachment(ev) is None


@pytest.mark.asyncio
async def test_probe_session_owner_id_missing_file(tmp_path):
    """Owner-id probe returns None on a missing file (no Telethon call)."""
    from unread.bot.app import _probe_session_owner_id
    from unread.config import get_settings

    s = get_settings()
    missing = tmp_path / "absent.sqlite"
    assert await _probe_session_owner_id(missing, s) is None
