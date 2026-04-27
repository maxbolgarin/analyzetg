"""Parser for Telegram references: links, @username, numeric ids, invite links."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from atg.models import ParsedLink

_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_NUMERIC_RE = re.compile(r"^-?\d+$")

# https://t.me/c/<internal_id>/<msg> OR /<internal_id>/<thread>/<msg>
_PRIVATE_POST_RE = re.compile(r"^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?/?$")
# https://t.me/<username>/<msg>  OR /<username>/<thread>/<msg>  OR /<username>
_PUBLIC_POST_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d+))?(?:/(\d+))?/?$")
# invite: https://t.me/+hash OR https://t.me/joinchat/hash
_INVITE_RE = re.compile(r"^(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)/?$")
# tg://resolve?domain=x&post=y
_TG_PROTOCOL_RE = re.compile(r"^tg://(?P<action>\w+)\?(?P<qs>.+)$")


def _private_id_to_chat_id(internal_id: int) -> int:
    """Channel/supergroup chat_id = -100 prepended to internal id."""
    return int(f"-100{internal_id}")


def parse(ref: str) -> ParsedLink:
    """Parse any user-facing reference string into a normalized ParsedLink.

    Supports all formats from spec §6.1. Returns a `ParsedLink` with a kind field
    describing what was detected; callers resolve to an entity via resolver.resolve().
    """
    s = ref.strip()
    raw = s

    # Self markers
    if s.lower() in {"me", "@me"}:
        return ParsedLink(kind="self", raw=raw)

    # tg:// deeplinks
    m = _TG_PROTOCOL_RE.match(s)
    if m:
        action = m.group("action").lower()
        qs = parse_qs(m.group("qs"))
        if action == "resolve" and "domain" in qs:
            post = int(qs["post"][0]) if "post" in qs else None
            return ParsedLink(kind="username", username=qs["domain"][0], msg_id=post, raw=raw)

    # Invite links (priority over generic public/private because of /+ prefix)
    m = _INVITE_RE.match(s)
    if m:
        return ParsedLink(kind="invite", invite_hash=m.group(1), raw=raw)

    # Private post t.me/c/<id>[/<thread>[/<msg>]]
    m = _PRIVATE_POST_RE.match(s)
    if m:
        internal_id = int(m.group(1))
        thread_or_msg = int(m.group(2)) if m.group(2) else None
        msg = int(m.group(3)) if m.group(3) else None
        chat_id = _private_id_to_chat_id(internal_id)
        if msg is not None:
            return ParsedLink(
                kind="internal_id",
                internal_id=internal_id,
                chat_id=chat_id,
                thread_id=thread_or_msg,
                msg_id=msg,
                raw=raw,
            )
        return ParsedLink(
            kind="internal_id",
            internal_id=internal_id,
            chat_id=chat_id,
            msg_id=thread_or_msg,
            raw=raw,
        )

    # Public post t.me/<username>[/<thread>[/<msg>]]
    m = _PUBLIC_POST_RE.match(s)
    if m:
        username = m.group(1)
        if username in {"c", "joinchat"}:
            pass  # already handled above; fall through
        else:
            thread_or_msg = int(m.group(2)) if m.group(2) else None
            msg = int(m.group(3)) if m.group(3) else None
            if msg is not None:
                return ParsedLink(
                    kind="username",
                    username=username,
                    thread_id=thread_or_msg,
                    msg_id=msg,
                    raw=raw,
                )
            return ParsedLink(kind="username", username=username, msg_id=thread_or_msg, raw=raw)

    # URL that doesn't match t.me — let it fall to fuzzy
    if urlparse(s).scheme:
        return ParsedLink(kind="fuzzy", raw=raw)

    # Numeric id (with possible -100 prefix or just -)
    if _NUMERIC_RE.match(s):
        chat_id = int(s)
        # UX: if the user typed a positive channel/supergroup id (shape
        # `100xxxxxxxxxx`, 13+ digits starting with 100), assume they meant
        # the negative form and auto-flip. Plain user ids are shorter.
        if chat_id > 0 and s.startswith("100") and len(s) >= 13:
            chat_id = -chat_id
        return ParsedLink(kind="numeric_id", chat_id=chat_id, raw=raw)

    # Plain username (@user or user)
    m = _USERNAME_RE.match(s)
    if m:
        return ParsedLink(kind="username", username=m.group(1), raw=raw)

    # Fallback — fuzzy title search
    return ParsedLink(kind="fuzzy", raw=raw)
