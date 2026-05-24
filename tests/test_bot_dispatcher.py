"""Pure-logic tests for `unread.bot.dispatcher.classify`.

The classifier never touches Telethon's network layer, so we build
lightweight stand-ins for `event.message` and feed them in directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from unread.bot.dispatcher import classify


@dataclass
class _FakeAttr:
    file_name: str | None = None


@dataclass
class _FakeDoc:
    mime_type: str = ""
    size: int | None = None
    attributes: list[_FakeAttr] = field(default_factory=list)


@dataclass
class _FakeMessage:
    message: str = ""
    media: Any = None
    fwd_from: Any = None


@dataclass
class _FakeEvent:
    message: _FakeMessage


# ----------------------------------------------------------------------
# Text-only paths
# ----------------------------------------------------------------------


def test_classify_youtube_short_link():
    ev = _FakeEvent(_FakeMessage(message="https://youtu.be/dQw4w9WgXcQ"))
    kind, payload = classify(ev)
    assert kind == "youtube"
    assert payload["url"] == "https://youtu.be/dQw4w9WgXcQ"


def test_classify_youtube_watch_url():
    ev = _FakeEvent(_FakeMessage(message="watch this https://www.youtube.com/watch?v=abc"))
    kind, _ = classify(ev)
    assert kind == "youtube"


def test_classify_tme_public_link():
    ev = _FakeEvent(_FakeMessage(message="https://t.me/somechan/4567"))
    kind, payload = classify(ev)
    assert kind == "tg"
    assert payload["url"] == "https://t.me/somechan/4567"


def test_classify_tme_private_link():
    ev = _FakeEvent(_FakeMessage(message="https://t.me/c/123456/789"))
    kind, _ = classify(ev)
    assert kind == "tg"


def test_classify_bare_username_is_tg():
    ev = _FakeEvent(_FakeMessage(message="@somechannel"))
    kind, payload = classify(ev)
    assert kind == "tg"
    assert payload["url"] == "@somechannel"


def test_classify_website_url():
    ev = _FakeEvent(_FakeMessage(message="Check https://example.com/article"))
    kind, payload = classify(ev)
    assert kind == "url"
    assert payload["url"] == "https://example.com/article"


def test_classify_strips_trailing_punctuation_from_url():
    ev = _FakeEvent(_FakeMessage(message="see (https://example.com/foo)."))
    kind, payload = classify(ev)
    assert kind == "url"
    assert payload["url"] == "https://example.com/foo"


def test_classify_plain_text_routes_to_file_stdin():
    ev = _FakeEvent(_FakeMessage(message="just some prose"))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "text"
    assert payload["text"] == "just some prose"


def test_classify_empty_message_routes_to_help_cmd():
    ev = _FakeEvent(_FakeMessage(message=""))
    kind, payload = classify(ev)
    assert kind == "cmd"
    assert payload["name"] == "help"


def test_classify_slash_command_basic():
    ev = _FakeEvent(_FakeMessage(message="/help"))
    kind, payload = classify(ev)
    assert kind == "cmd"
    assert payload["name"] == "help"
    assert payload["args"] == []


def test_classify_slash_command_with_args_and_botname():
    ev = _FakeEvent(_FakeMessage(message="/preset@my_bot detailed"))
    kind, payload = classify(ev)
    assert kind == "cmd"
    assert payload["name"] == "preset"
    assert payload["args"] == ["detailed"]


# ----------------------------------------------------------------------
# Media paths
# ----------------------------------------------------------------------


def test_classify_photo_is_image_file():
    from telethon.tl.types import MessageMediaPhoto

    media = MessageMediaPhoto.__new__(MessageMediaPhoto)
    ev = _FakeEvent(_FakeMessage(media=media))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "media"
    assert payload["kind"] == "image"


def test_classify_youtube_url_with_link_preview_is_youtube():
    """Telegram auto-attaches a web-page preview for every URL message.
    The dispatcher must ignore that preview and classify by URL text,
    or YouTube / website URLs end up in the file handler instead.
    """
    from telethon.tl.types import MessageMediaWebPage

    preview = MessageMediaWebPage.__new__(MessageMediaWebPage)
    ev = _FakeEvent(
        _FakeMessage(
            message="https://www.youtube.com/watch?v=xwYfsknlWHI",
            media=preview,
        )
    )
    kind, payload = classify(ev)
    assert kind == "youtube"
    assert payload["url"] == "https://www.youtube.com/watch?v=xwYfsknlWHI"


def test_classify_website_url_with_link_preview_is_website():
    from telethon.tl.types import MessageMediaWebPage

    preview = MessageMediaWebPage.__new__(MessageMediaWebPage)
    ev = _FakeEvent(_FakeMessage(message="https://example.com/article", media=preview))
    kind, payload = classify(ev)
    assert kind == "url"
    assert payload["url"] == "https://example.com/article"


def test_classify_tme_link_with_link_preview_is_tg():
    from telethon.tl.types import MessageMediaWebPage

    preview = MessageMediaWebPage.__new__(MessageMediaWebPage)
    ev = _FakeEvent(_FakeMessage(message="https://t.me/somechan/123", media=preview))
    kind, _ = classify(ev)
    assert kind == "tg"


# ----------------------------------------------------------------------
# Forwarded-from-channel metadata + caption preservation
# ----------------------------------------------------------------------


@dataclass
class _FakeFwd:
    """Stand-in for `MessageFwdHeader`. Only the fields classify reads."""

    from_id: Any = None
    channel_post: int | None = None
    from_name: str | None = None


def test_classify_forwarded_photo_preserves_caption_and_channel_id():
    """A forwarded photo with caption + PeerChannel.from_id → file payload
    carries the caption AND the source channel/msg id."""
    from telethon.tl.types import MessageMediaPhoto, PeerChannel

    media = MessageMediaPhoto.__new__(MessageMediaPhoto)
    peer = PeerChannel.__new__(PeerChannel)
    peer.channel_id = 3853386994
    fwd = _FakeFwd(from_id=peer, channel_post=81, from_name="BullTrading")
    ev = _FakeEvent(_FakeMessage(message="caption body", media=media, fwd_from=fwd))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "media"
    assert payload["kind"] == "image"
    assert payload["caption"] == "caption body"
    assert payload["fwd_channel_id"] == 3853386994
    assert payload["fwd_msg_id"] == 81
    assert payload["fwd_title"] == "BullTrading"


def test_classify_forwarded_text_from_channel_carries_fwd_info():
    from telethon.tl.types import PeerChannel

    peer = PeerChannel.__new__(PeerChannel)
    peer.channel_id = 111
    fwd = _FakeFwd(from_id=peer, channel_post=42, from_name="NewsChan")
    ev = _FakeEvent(_FakeMessage(message="some forwarded text", fwd_from=fwd))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "text"
    assert payload["text"] == "some forwarded text"
    assert payload["fwd_channel_id"] == 111
    assert payload["fwd_msg_id"] == 42


def test_classify_forwarded_from_user_has_no_fwd_info():
    """User→user forwards (DM forward) don't carry a channel — no extra metadata."""
    from telethon.tl.types import PeerUser

    peer = PeerUser.__new__(PeerUser)
    peer.user_id = 999
    fwd = _FakeFwd(from_id=peer, from_name="Friend")
    ev = _FakeEvent(_FakeMessage(message="hello", fwd_from=fwd))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "text"
    assert "fwd_channel_id" not in payload


def test_classify_photo_without_caption_no_caption_key():
    """No caption → no `caption` key in payload (avoids empty-string ambiguity)."""
    from telethon.tl.types import MessageMediaPhoto

    media = MessageMediaPhoto.__new__(MessageMediaPhoto)
    ev = _FakeEvent(_FakeMessage(message="", media=media))
    _kind, payload = classify(ev)
    assert "caption" not in payload


def test_classify_album_member_carries_grouped_id():
    """Telegram albums: each photo arrives as a separate event with
    shared `grouped_id`. Dispatcher must surface it so the burst can
    merge album members into one logical item."""
    from telethon.tl.types import MessageMediaPhoto

    media = MessageMediaPhoto.__new__(MessageMediaPhoto)

    @dataclass
    class _GroupedMsg:
        message: str = ""
        media: Any = None
        fwd_from: Any = None
        grouped_id: int = 0

    msg = _GroupedMsg(message="album caption", media=media, grouped_id=42)
    ev = _FakeEvent(msg)
    _kind, payload = classify(ev)
    assert payload["grouped_id"] == 42
    assert payload["caption"] == "album caption"


def test_classify_non_album_msg_has_no_grouped_id_key():
    from telethon.tl.types import MessageMediaPhoto

    media = MessageMediaPhoto.__new__(MessageMediaPhoto)
    ev = _FakeEvent(_FakeMessage(message="", media=media))
    _kind, payload = classify(ev)
    assert "grouped_id" not in payload


@pytest.mark.parametrize(
    "name, mime, expected_kind",
    [
        ("paper.pdf", "application/pdf", "pdf"),
        ("notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        ("memo.txt", "text/plain", "text"),
        ("voice.ogg", "audio/ogg", "audio"),
        ("clip.mp4", "video/mp4", "video"),
        ("photo.jpg", "image/jpeg", "image"),
        ("snippet.py", "text/x-python", "text"),
        ("binary.bin", "application/octet-stream", "unknown"),
    ],
)
def test_classify_document_kinds(name, mime, expected_kind):
    doc = _FakeDoc(
        mime_type=mime,
        size=1234,
        attributes=[_FakeAttr(file_name=name)],
    )
    # Use real Telethon class to satisfy the isinstance check.
    from telethon.tl.types import Document, MessageMediaDocument

    real_doc = Document.__new__(Document)
    real_doc.mime_type = doc.mime_type
    real_doc.size = doc.size
    real_doc.attributes = doc.attributes
    real_media = MessageMediaDocument.__new__(MessageMediaDocument)
    real_media.document = real_doc
    ev = _FakeEvent(_FakeMessage(media=real_media))
    kind, payload = classify(ev)
    assert kind == "file"
    assert payload["source"] == "media"
    assert payload["kind"] == expected_kind
    assert payload["name"] == name
