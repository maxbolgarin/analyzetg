"""Tests for `unread.tg.folders` — matching logic + MTProto parsing.

No real Telegram traffic. We feed `_folder_from_filter` fake objects that
duck-type the Telethon DialogFilter / DialogFilterChatlist / DialogFilterDefault
classes so we can exercise every branch cheaply.
"""

from __future__ import annotations

from typing import Any

from unread.tg.folders import (
    Folder,
    _folder_from_filter,
    _peer_id,
    resolve_folder,
)

# --- Fake MTProto shapes ------------------------------------------------


def _channel_peer(channel_id: int):
    """Real telethon Peer for channels — `get_peer_id` converts it to the
    bot-API style `-100xxxxxxxxxx` chat_id."""
    from telethon.tl.types import PeerChannel

    return PeerChannel(channel_id=channel_id)


class _FakeFilter:
    """Duck-types Telethon's DialogFilter / DialogFilterChatlist / DialogFilterDefault.

    `_folder_from_filter` uses `obj.__class__.__name__` to branch, so we
    override `__class__` via a dynamically-named subclass per instance."""

    def __init__(
        self,
        *,
        cls_name: str = "DialogFilter",
        fid: int = 1,
        title: Any = "Alpha",
        emoticon: str | None = None,
        include_peers: list[Any] | None = None,
        pinned_peers: list[Any] | None = None,
        **flags: bool,
    ) -> None:
        self.id = fid
        self.title = title
        self.emoticon = emoticon
        self.include_peers = include_peers or []
        self.pinned_peers = pinned_peers or []
        for flag in ("contacts", "non_contacts", "groups", "broadcasts", "bots"):
            setattr(self, flag, bool(flags.get(flag, False)))
        self.__class__ = type(cls_name, (_FakeFilter,), {})


def _fake_filter(**kw: Any) -> _FakeFilter:
    return _FakeFilter(**kw)


# --- resolve_folder -----------------------------------------------------


def test_resolve_by_exact_title_case_insensitive() -> None:
    folders = [Folder(id=1, title="Alpha"), Folder(id=2, title="News")]
    assert resolve_folder("alpha", folders).id == 1
    assert resolve_folder("ALPHA", folders).id == 1
    assert resolve_folder("Alpha", folders).id == 1


def test_resolve_by_numeric_id() -> None:
    folders = [Folder(id=7, title="Alpha"), Folder(id=13, title="News")]
    assert resolve_folder("7", folders).id == 7
    assert resolve_folder("13", folders).id == 13
    assert resolve_folder("99", folders) is None


def test_resolve_unique_substring() -> None:
    folders = [Folder(id=1, title="Alpha"), Folder(id=2, title="News"), Folder(id=3, title="Tech")]
    # "alp" uniquely matches Alpha.
    assert resolve_folder("alp", folders).id == 1


def test_resolve_ambiguous_substring_returns_none() -> None:
    folders = [Folder(id=1, title="Alpha"), Folder(id=2, title="Alternative")]
    # Both contain "al" — ambiguous → None (force user to disambiguate).
    assert resolve_folder("al", folders) is None


def test_resolve_empty_inputs() -> None:
    assert resolve_folder("", [Folder(id=1, title="Alpha")]) is None
    assert resolve_folder("anything", []) is None


def test_resolve_exact_match_wins_over_substring() -> None:
    # "Life" matches "Life" exactly and is a substring of "Lifestyle";
    # exact match should win even though substring alone would be ambiguous.
    folders = [Folder(id=1, title="Life"), Folder(id=2, title="Lifestyle")]
    assert resolve_folder("Life", folders).id == 1


# --- _folder_from_filter ------------------------------------------------


def test_default_filter_returns_none() -> None:
    # The implicit "All chats" filter — not a real folder.
    assert _folder_from_filter(_fake_filter(cls_name="DialogFilterDefault")) is None


def test_filter_with_explicit_peers() -> None:
    peers = [_channel_peer(111), _channel_peer(222)]
    result = _folder_from_filter(_fake_filter(title="Alpha", include_peers=peers))
    assert result is not None
    assert result.title == "Alpha"
    # Channel ids get normalized to bot-API form (-1000000000000 - chan_id).
    assert len(result.include_chat_ids) == 2
    # Telethon's get_peer_id emits the -100xxxxxxxxxx style.
    assert all(cid < 0 for cid in result.include_chat_ids)
    assert result.has_rule_based_inclusion is False


def test_filter_pinned_peers_are_merged_with_include_peers() -> None:
    inc = [_channel_peer(111)]
    pinned = [_channel_peer(222)]
    result = _folder_from_filter(_fake_filter(title="Alpha", include_peers=inc, pinned_peers=pinned))
    assert result is not None
    assert len(result.include_chat_ids) == 2


def test_filter_title_wrapped_in_text_object() -> None:
    # Telethon 2.x wraps title as TextWithEntities.
    class _TextWithEntities:
        def __init__(self, text: str) -> None:
            self.text = text

    result = _folder_from_filter(_fake_filter(title=_TextWithEntities("News")))
    assert result.title == "News"


def test_filter_rule_based_flag_set() -> None:
    result = _folder_from_filter(_fake_filter(title="Groups", include_peers=[], groups=True))
    assert result is not None
    assert result.has_rule_based_inclusion is True
    assert result.include_chat_ids == set()


def test_filter_empty_title_is_dropped_by_list_folders() -> None:
    # `_folder_from_filter` still returns an object; list_folders filters
    # these out. Guard the convention here so silent regressions show up.
    result = _folder_from_filter(_fake_filter(title=""))
    assert result is not None
    assert result.title == ""


def test_chatlist_variant_flagged() -> None:
    result = _folder_from_filter(_fake_filter(cls_name="DialogFilterChatlist", title="Shared folder"))
    assert result is not None
    assert result.is_chatlist is True


def test_peer_id_falls_back_gracefully() -> None:
    # A peer shape that get_peer_id can't handle must return None, not crash.
    class _Garbage:
        pass

    assert _peer_id(_Garbage()) is None
