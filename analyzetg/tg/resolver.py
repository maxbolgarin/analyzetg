"""Resolve user-supplied references to canonical Telegram entities.

Implements the algorithm from spec §6.2. Uses the `chats` table as a cache of
previously resolved entities so repeat lookups don't hit the Telegram API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz, process

from analyzetg.db.repo import Repo
from analyzetg.models import ParsedLink, ResolvedRef
from analyzetg.tg.client import _chat_kind, entity_id, entity_title, entity_username
from analyzetg.tg.links import parse
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


@dataclass(slots=True)
class FuzzyCandidate:
    chat_id: int
    title: str | None
    username: str | None
    kind: str
    score: int


async def resolve(
    client: TelegramClient,
    repo: Repo,
    ref: str,
    *,
    join: bool = False,
    fuzzy_threshold: int = 80,
    fuzzy_margin: int = 10,
    prompt_choice=None,
) -> ResolvedRef:
    """Resolve any supported reference format to a `ResolvedRef`.

    `prompt_choice` is an optional callback used when fuzzy matching is ambiguous;
    it receives a ranked list of candidates and returns the chosen index (or None).
    """
    parsed = parse(ref)
    log.debug("resolve.parsed", parsed=parsed)

    # Self
    if parsed.kind == "self":
        me = await client.get_me()
        eid = entity_id(me)
        await repo.upsert_chat(
            eid,
            "user",
            title=entity_title(me),
            username=entity_username(me),
        )
        return ResolvedRef(chat_id=eid, kind="user", title=entity_title(me), username=entity_username(me))

    # Direct numeric id
    if parsed.kind == "numeric_id" and parsed.chat_id is not None:
        entity = await client.get_entity(parsed.chat_id)
        return await _record_and_return(repo, entity, parsed)

    # Private link (t.me/c/<id>/...) → compose -100<id>
    if parsed.kind == "internal_id" and parsed.chat_id is not None:
        entity = await client.get_entity(parsed.chat_id)
        return await _record_and_return(repo, entity, parsed)

    # Invite link
    if parsed.kind == "invite" and parsed.invite_hash:
        from telethon.tl.functions.messages import (  # type: ignore[attr-defined]
            CheckChatInviteRequest,
            ImportChatInviteRequest,
        )

        info = await client(CheckChatInviteRequest(parsed.invite_hash))
        chat = getattr(info, "chat", None)
        if chat is None:
            # ChatInvite (not-yet-joined) — can only join, can't read
            if not join:
                raise RuntimeError("Invite link requires joining the chat. Re-run with --join.")
            result = await client(ImportChatInviteRequest(parsed.invite_hash))
            chat = result.chats[0] if getattr(result, "chats", None) else None
            if chat is None:
                raise RuntimeError("Failed to import invite link.")
        eid = entity_id(chat)
        await repo.upsert_chat(
            eid,
            _chat_kind(chat),
            title=entity_title(chat),
            username=entity_username(chat),
        )
        return ResolvedRef(
            chat_id=eid,
            kind=_chat_kind(chat),  # type: ignore[arg-type]
            title=entity_title(chat),
            username=entity_username(chat),
            thread_id=parsed.thread_id,
            msg_id=parsed.msg_id,
            requires_join=getattr(info, "chat", None) is None,
        )

    # Username
    if parsed.kind == "username" and parsed.username:
        # Local cache first
        cached = await repo.find_chat_by_username(parsed.username)
        if cached:
            try:
                entity = await client.get_entity(cached["id"])
                return await _record_and_return(repo, entity, parsed)
            except Exception:
                pass  # fall through to live lookup
        try:
            entity = await client.get_entity(parsed.username)
            return await _record_and_return(repo, entity, parsed)
        except ValueError as e:
            # Telethon raises ValueError("No user has ...") on UsernameNotOccupied.
            # Fall through to fuzzy: the ref might be a dialog title, not a @username.
            # Visible warning: without this, fuzzy could silently return a
            # different chat whose title happens to match the query — and the
            # user wouldn't know their @username lookup failed.
            log.warning("resolve.username_miss", username=parsed.username, err=str(e)[:80])
            from rich.console import Console

            Console().print(
                f"[yellow]⚠ @{parsed.username} not found on Telegram;[/] "
                "searching your dialogs by fuzzy title match instead."
            )

    # Fuzzy / fallback: search iter_dialogs by title+username
    return await _fuzzy_resolve(
        client=client,
        repo=repo,
        query=parsed.raw,
        threshold=fuzzy_threshold,
        margin=fuzzy_margin,
        prompt_choice=prompt_choice,
    )


async def _record_and_return(repo: Repo, entity: Any, parsed: ParsedLink) -> ResolvedRef:
    eid = entity_id(entity)
    kind = _chat_kind(entity)
    title = entity_title(entity)
    username = entity_username(entity)
    await repo.upsert_chat(eid, kind, title=title, username=username)
    return ResolvedRef(
        chat_id=eid,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        username=username,
        thread_id=parsed.thread_id,
        msg_id=parsed.msg_id,
    )


async def _fuzzy_resolve(
    *,
    client: TelegramClient,
    repo: Repo,
    query: str,
    threshold: int,
    margin: int,
    prompt_choice,
) -> ResolvedRef:
    """Scan iter_dialogs() and rank candidates by fuzzy match on title+username."""
    pool: list[FuzzyCandidate] = []
    async for dialog in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = dialog.entity
        title = entity_title(entity)
        username = entity_username(entity)
        searchable = " ".join(filter(None, [title, f"@{username}" if username else None]))
        if not searchable:
            continue
        score = max(
            fuzz.WRatio(query, searchable),
            fuzz.partial_ratio(query.lower(), searchable.lower()),
        )
        if score >= threshold // 2:
            pool.append(
                FuzzyCandidate(
                    chat_id=entity_id(entity),
                    title=title,
                    username=username,
                    kind=_chat_kind(entity),
                    score=int(score),
                )
            )
        # Remember every seen chat in cache (useful for later resolves)
        await repo.upsert_chat(entity_id(entity), _chat_kind(entity), title=title, username=username)

    if not pool:
        raise RuntimeError(f"No dialog matches '{query}'. Try a link or @username.")

    pool.sort(key=lambda c: c.score, reverse=True)
    top = pool[0]
    second_score = pool[1].score if len(pool) > 1 else 0

    if top.score >= threshold and top.score - second_score >= margin:
        return _candidate_to_ref(top)

    # Ambiguous — ask caller to choose
    candidates = pool[:5]
    if prompt_choice is not None:
        idx = prompt_choice(candidates)
        if idx is not None and 0 <= idx < len(candidates):
            return _candidate_to_ref(candidates[idx])
    # No interactive caller — default to best
    log.warning(
        "resolve.fuzzy.ambiguous",
        top=top.title,
        top_score=top.score,
        second=pool[1].title if len(pool) > 1 else None,
    )
    return _candidate_to_ref(top)


def _candidate_to_ref(c: FuzzyCandidate) -> ResolvedRef:
    return ResolvedRef(
        chat_id=c.chat_id,
        kind=c.kind,
        title=c.title,
        username=c.username,  # type: ignore[arg-type]
    )


def rank_candidates(query: str, items: list[tuple[int, str, str | None, str]]) -> list[FuzzyCandidate]:
    """Pure-Python ranker used by tests.

    items: list of (chat_id, title, username, kind).
    Returns candidates sorted by score desc.
    """
    choices = {
        (chat_id, title, username, kind): " ".join(
            filter(None, [title, f"@{username}" if username else None])
        )
        for chat_id, title, username, kind in items
    }
    results = process.extract(query, choices, scorer=fuzz.WRatio, limit=len(choices) or 1)
    out: list[FuzzyCandidate] = []
    for _matched_str, score, key in results:
        chat_id, title, username, kind = key
        out.append(
            FuzzyCandidate(chat_id=chat_id, title=title, username=username, kind=kind, score=int(score))
        )
    return out
