"""Shared data models used across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

MediaType = Literal["voice", "videonote", "video", "photo", "doc"]
SourceKind = Literal["chat", "channel", "topic", "comments"]
ChatKind = Literal["user", "group", "supergroup", "channel", "forum"]


@dataclass(slots=True)
class ParsedLink:
    """Result of parsing a Telegram reference string (link, @user, numeric id, ...)."""

    kind: str  # username | internal_id | invite | numeric_id | fuzzy
    username: str | None = None
    internal_id: int | None = None  # from t.me/c/<id>/...
    chat_id: int | None = None  # directly given (with -100 prefix)
    thread_id: int | None = None
    msg_id: int | None = None
    invite_hash: str | None = None
    raw: str = ""


@dataclass(slots=True)
class ResolvedRef:
    """Canonical resolution of a user-supplied reference."""

    chat_id: int
    kind: ChatKind
    title: str | None = None
    username: str | None = None
    thread_id: int | None = None
    msg_id: int | None = None
    requires_join: bool = False
    linked_chat_id: int | None = None


@dataclass(slots=True)
class Subscription:
    chat_id: int
    thread_id: int = 0
    title: str | None = None
    source_kind: SourceKind = "chat"
    enabled: bool = True
    start_from_msg_id: int | None = None
    start_from_date: datetime | None = None
    transcribe_voice: bool = True
    transcribe_videonote: bool = True
    transcribe_video: bool = False
    added_at: datetime | None = None


@dataclass(slots=True)
class Message:
    chat_id: int
    msg_id: int
    date: datetime
    thread_id: int | None = None
    sender_id: int | None = None
    sender_name: str | None = None
    text: str | None = None
    reply_to: int | None = None
    forward_from: str | None = None
    media_type: MediaType | None = None
    media_doc_id: int | None = None
    media_duration: int | None = None
    transcript: str | None = None
    transcript_model: str | None = None
    reactions: dict[str, int] | None = None
    # Derived, not persisted:
    duplicates: int = 0


@dataclass(slots=True)
class SyncState:
    chat_id: int
    thread_id: int = 0
    last_msg_id: int | None = None
    last_synced_at: datetime | None = None


@dataclass(slots=True)
class Chunk:
    """A group of messages that fits within a model's token budget."""

    messages: list[Message] = field(default_factory=list)
    tokens: int = 0

    @property
    def msg_ids(self) -> list[int]:
        return [m.msg_id for m in self.messages]
