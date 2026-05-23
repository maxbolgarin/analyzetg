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
async def test_probe_candidate_owner_id_missing_file(tmp_path):
    """Candidate validator returns None on missing / empty files (no Telethon call)."""
    from unread.bot.session_upload import _probe_candidate_owner_id
    from unread.config import get_settings

    s = get_settings()
    missing = tmp_path / "absent.sqlite"
    assert await _probe_candidate_owner_id(missing, s) is None
    empty = tmp_path / "empty.sqlite"
    empty.write_bytes(b"")
    assert await _probe_candidate_owner_id(empty, s) is None


def test_normalized_session_path_adds_session_suffix():
    """Telethon appends `.session` — installer destination must match."""
    from pathlib import Path

    from unread.bot.session_upload import _normalized_session_path

    assert _normalized_session_path(Path("/x/y/session.sqlite")) == Path("/x/y/session.sqlite.session")
    # Already-suffixed paths are left alone.
    assert _normalized_session_path(Path("/x/y/foo.session")) == Path("/x/y/foo.session")


def test_has_session_blob_finds_dot_session_file(tmp_path, monkeypatch):
    """The legacy `session.sqlite` AND Telethon's `session.sqlite.session`
    both count as a usable session blob."""
    from unread.bot.app import _has_session_blob

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    storage = tmp_path / "storage"
    storage.mkdir()
    # Only the .session-suffixed file exists (this is the real Telethon shape).
    (storage / "session.sqlite.session").write_bytes(b"\x00")

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert _has_session_blob(s) is True
    finally:
        reset_settings()


def test_has_session_blob_false_when_neither_exists(tmp_path, monkeypatch):
    from unread.bot.app import _has_session_blob

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "fresh"))

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert _has_session_blob(s) is False
    finally:
        reset_settings()
