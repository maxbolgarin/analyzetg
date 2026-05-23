"""Classify an incoming owner message → (handler_kind, payload).

Pure logic — no Telegram I/O, no async. Returns one of:

* ``("cmd", {"name": str, "args": list[str], "raw": str})``
* ``("file", {"source": "media"|"text", ...})``
* ``("youtube", {"url": str})``
* ``("url", {"url": str})``
* ``("tg", {"url": str})``

The forward case is handled here by recursing on the inner content
once. Recursion depth is capped at 1 — chained forwards (forward of a
forward) collapse to the innermost message's classification.
"""

from __future__ import annotations

import re
from typing import Any

from telethon import events
from telethon.tl.types import (
    Document,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

# URL grabber. Intentionally permissive — we re-validate with the
# YouTube/TG/website-specific helpers downstream. Anchored to `http(s)`
# only so a stray "google.com" in prose doesn't trigger website analysis.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# t.me link variants we recognize as "fetch this TG message/thread/chat".
# Public ("t.me/<username>/<msg_id>") and private ("t.me/c/<id>/<msg>")
# forms. The "@username" bare form is treated as TG-handler input too.
_TME_RE = re.compile(
    r"^https?://(?:t\.me|telegram\.me)/(?:c/\d+/\d+|[A-Za-z0-9_]+(?:/\d+)?)/?$",
    re.IGNORECASE,
)
_BARE_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def classify(event: events.NewMessage.Event) -> tuple[str, dict[str, Any]]:
    """Single-level classifier. Recurses once for forwards."""
    msg = event.message

    # `MessageMediaWebPage` is Telegram's auto-generated link preview
    # that fires whenever a user sends a URL — every YouTube / web URL
    # message has one. We MUST NOT treat it as a file attachment;
    # the URL itself lives in `msg.message` and gets the proper
    # classifier branch below. Real downloadable media (documents,
    # photos) keep the file branch.
    if msg.media is not None and not isinstance(msg.media, MessageMediaWebPage):
        return ("file", _classify_media(msg.media))

    text = (msg.message or "").strip()

    # Forwarded text-only message: treat the inner text the same way.
    # Telethon exposes the original-sender metadata on `fwd_from` but
    # the displayed text is on `message` already, so a recursion is
    # unnecessary — the same classification applies whether the text
    # arrived directly or as a forward. (Forwards WITH media follow
    # the media branch above and don't reach here.)

    if not text:
        # Empty, no media — nothing actionable. Treat as a malformed
        # command so the handler responds with the help text.
        return ("cmd", {"name": "help", "args": [], "raw": ""})

    if text.startswith("/"):
        parts = text[1:].split()
        name = parts[0].lower() if parts else "help"
        # Strip an `@botname` suffix common when the bot is used in
        # groups (`/help@unread_bot args`).
        if "@" in name:
            name = name.split("@", 1)[0]
        return ("cmd", {"name": name, "args": parts[1:], "raw": text})

    if _BARE_USERNAME_RE.match(text):
        return ("tg", {"url": text})

    url_match = _URL_RE.search(text)
    if url_match is not None:
        url = url_match.group(0).rstrip(").,;!?")
        if _is_tme_url(url):
            return ("tg", {"url": url})
        if _is_youtube_url(url):
            return ("youtube", {"url": url})
        return ("url", {"url": url})

    # Plain text, no URL, no command — treat as stdin-style file input.
    return (
        "file",
        {"source": "text", "text": text, "name": "stdin"},
    )


def _classify_media(media: Any) -> dict[str, Any]:
    """Extract a normalized media descriptor from a Telethon media object.

    Only carries fields the file handler needs: doc/photo kind, mime
    type (for routing to the right extractor), reported size, and the
    raw media object so the handler can call `client.download_media`.
    """
    if isinstance(media, MessageMediaPhoto):
        return {
            "source": "media",
            "kind": "image",
            "mime": "image/jpeg",
            "size": None,
            "media": media,
            "name": "photo.jpg",
        }
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if not isinstance(doc, Document):
            return {
                "source": "media",
                "kind": "unknown",
                "mime": "",
                "size": None,
                "media": media,
                "name": "attachment",
            }
        mime = doc.mime_type or ""
        size = getattr(doc, "size", None)
        name = _filename_from_doc(doc) or _name_for_mime(mime)
        return {
            "source": "media",
            "kind": _kind_for_mime(mime, name),
            "mime": mime,
            "size": size,
            "media": media,
            "name": name,
        }
    # Round videos, audio, contact, geo, etc. all reach here as
    # MessageMediaDocument; anything else (poll, geo, contact, …) is
    # surfaced as `unknown` for the handler to refuse politely.
    return {
        "source": "media",
        "kind": "unknown",
        "mime": "",
        "size": None,
        "media": media,
        "name": "attachment",
    }


def _filename_from_doc(doc: Document) -> str | None:
    # Telethon doesn't expose a top-level `file_name`; it lives in one
    # of the document attributes. Iterate to find it.
    for attr in getattr(doc, "attributes", []) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return None


def _name_for_mime(mime: str) -> str:
    if not mime:
        return "attachment"
    # Crude — but good enough for a download path. The extractor uses
    # the extension to pick an extractor, so we synthesize a reasonable
    # one when the original filename is absent.
    if "/" in mime:
        return f"attachment.{mime.split('/', 1)[1].split(';', maxsplit=1)[0]}"
    return "attachment"


def _kind_for_mime(mime: str, name: str) -> str:
    """Coarse-grained kind used by the file handler for routing.

    Mirrors the categories `unread/files/extractors.py` already
    recognizes: text / pdf / docx / audio / video / image / unknown.
    """
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = mime.lower()
    if mime.startswith("image/") or suffix in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}:
        return "image"
    if mime.startswith("audio/") or suffix in {
        "mp3",
        "m4a",
        "wav",
        "flac",
        "ogg",
        "oga",
        "opus",
        "aac",
        "wma",
    }:
        return "audio"
    if mime.startswith("video/") or suffix in {"mp4", "mov", "mkv", "webm", "avi", "m4v", "mpeg", "mpg"}:
        return "video"
    if mime == "application/pdf" or suffix == "pdf":
        return "pdf"
    if "officedocument.wordprocessingml" in mime or suffix == "docx":
        return "docx"
    if mime.startswith("text/") or suffix in {
        "txt",
        "md",
        "markdown",
        "rst",
        "log",
        "csv",
        "tsv",
        "json",
        "yaml",
        "yml",
        "toml",
        "xml",
        "html",
        "htm",
        "ini",
        "cfg",
        "conf",
        "env",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "c",
        "cpp",
        "h",
        "hpp",
        "cs",
        "swift",
        "sh",
        "bash",
        "zsh",
        "sql",
        "lua",
        "php",
    }:
        return "text"
    return "unknown"


def _is_tme_url(url: str) -> bool:
    return bool(_TME_RE.match(url))


def _is_youtube_url(url: str) -> bool:
    """Best-effort YT detection. The handler re-validates via extract_video_id."""
    lower = url.lower()
    return (
        "youtube.com/watch" in lower
        or "youtube.com/shorts/" in lower
        or "youtube.com/embed/" in lower
        or "youtube.com/live/" in lower
        or "youtu.be/" in lower
    )
