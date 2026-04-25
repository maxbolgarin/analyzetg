"""Telegram chat folders (dialog filters) — list them, get chat ids.

Telegram calls them "dialog filters"; the UI calls them "folders" or
"chat lists". Each folder has a title, optional emoji icon, and an explicit
`include_peers` list plus category flags (contacts/groups/channels/bots)
that further include chats by kind.

We only materialize the explicit `include_peers` + `pinned_peers`. Rule-based
inclusion (`contacts=True` etc.) is not expanded — it would require scanning
every dialog in your account on every call. Good enough for "read all chats
in folder Alpha" where Alpha is a curated list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


@dataclass(slots=True)
class Folder:
    id: int
    title: str
    emoticon: str | None = None
    # chat_ids included explicitly (include_peers + pinned_peers). These are
    # bot-API style ids (-100xxxxxxxxxx for channels/supergroups).
    include_chat_ids: set[int] = field(default_factory=set)
    # True if the folder has category-based inclusion flags that we don't expand.
    has_rule_based_inclusion: bool = False
    # "Shareable" chat-list folder (read-only peer list).
    is_chatlist: bool = False


def _peer_id(peer) -> int | None:
    """Convert Telethon InputPeer/Peer to a numeric bot-API chat_id."""
    try:
        from telethon.utils import get_peer_id

        return int(get_peer_id(peer))
    except Exception as e:
        log.debug("folders.peer_id_failed", peer=type(peer).__name__, err=str(e)[:100])
        return None


def _folder_from_filter(f) -> Folder | None:
    """Convert a raw `DialogFilter` / `DialogFilterChatlist` object to our Folder.

    Returns None for the implicit "All chats" default filter (DialogFilterDefault),
    which isn't a real folder the user created."""
    cls_name = f.__class__.__name__
    if cls_name == "DialogFilterDefault":
        return None

    title_attr = getattr(f, "title", None)
    # Newer Telethon (2.x) wraps title in a `TextWithEntities`-like object.
    title = getattr(title_attr, "text", None) or (title_attr if isinstance(title_attr, str) else "") or ""

    include: set[int] = set()
    for peer_list_name in ("include_peers", "pinned_peers"):
        for peer in getattr(f, peer_list_name, None) or []:
            pid = _peer_id(peer)
            if pid is not None:
                include.add(pid)

    # Category-based inclusion flags (non-shareable filters only).
    rule_based = any(
        bool(getattr(f, flag, False)) for flag in ("contacts", "non_contacts", "groups", "broadcasts", "bots")
    )

    return Folder(
        id=int(getattr(f, "id", 0) or 0),
        title=title.strip(),
        emoticon=getattr(f, "emoticon", None) or None,
        include_chat_ids=include,
        has_rule_based_inclusion=rule_based,
        is_chatlist=cls_name == "DialogFilterChatlist",
    )


async def list_folders(client: TelegramClient) -> list[Folder]:
    """Return all user-defined folders. Excludes the implicit 'All chats'."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[attr-defined]

    result = await client(GetDialogFiltersRequest())
    raw = getattr(result, "filters", None)
    if raw is None:
        raw = result if isinstance(result, list) else []
    folders: list[Folder] = []
    for f in raw:
        folder = _folder_from_filter(f)
        if folder is not None and folder.title:
            folders.append(folder)
    return folders


def _normalize(s: str) -> str:
    return s.strip().casefold()


def resolve_folder(needle: str, folders: list[Folder]) -> Folder | None:
    """Match a user-supplied folder name (case-insensitive) or numeric id.

    Exact case-insensitive title match wins; falls back to unique substring
    match. Returns None if no match or multiple substring matches."""
    if not needle or not folders:
        return None
    # Numeric id
    if needle.isdigit():
        want = int(needle)
        return next((f for f in folders if f.id == want), None)

    n = _normalize(needle)
    exact = [f for f in folders if _normalize(f.title) == n]
    if len(exact) == 1:
        return exact[0]
    # Unique substring match (so "alp" finds "Alpha" when unambiguous).
    subs = [f for f in folders if n in _normalize(f.title)]
    if len(subs) == 1:
        return subs[0]
    return None


async def chat_folder_index(client: TelegramClient) -> dict[int, list[str]]:
    """Return `{chat_id: [folder_title, ...]}` for every explicitly-included chat.

    Rule-based folders (contacts/groups/etc.) are not expanded — same caveat
    as `list_folders`. Each chat may appear in multiple folders; titles are
    returned in folder-iteration order. Empty dict if there are no folders.
    """
    folders = await list_folders(client)
    out: dict[int, list[str]] = {}
    for f in folders:
        for cid in f.include_chat_ids:
            out.setdefault(cid, []).append(f.title)
    return out


__all__ = ["Folder", "chat_folder_index", "list_folders", "resolve_folder"]
