"""`/upload_session` state machine.

Two states tracked on `app._chat_state[chat_id]`:
* ``pending_session_upload``: True after `/upload_session`; the next
  document from the owner is consumed here instead of routed to the
  file handler.
* (cleared after a successful install OR `/cancel`.)

Security: file mode is forced to 0o600 immediately after rename; the
validator opens the candidate as a Telethon `SQLiteSession`, runs
`is_user_authorized()`, and rejects anything that doesn't pass.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


# Max session size. A fresh SQLiteSession is well under 1 MB; a long-
# lived one with cached peers/auth grows but stays under a few MB.
# Cap at 50 MB so an accidental wrong-file upload (a video, a backup
# zip) fails fast on size rather than after a doomed validate.
_MAX_SESSION_BYTES = 50 * 1_000_000


async def start_upload(event: events.NewMessage.Event, *, app: BotApp) -> None:
    """Enter the upload-pending state for this chat."""
    chat_state = app._chat_state.setdefault(event.chat_id, {})
    chat_state["pending_session_upload"] = True
    await event.reply(
        "Send your Telethon `session.sqlite` as a **document** "
        "(not photo / not voice). I'll validate it before installing.\n\n"
        "`/cancel` to abort.",
        parse_mode="md",
    )


async def handle_uploaded_file(event: events.NewMessage.Event, *, app: BotApp) -> None:
    """Receive the candidate session file, validate, install.

    The file is written to `settings.telegram.session_path` — the
    SAME location the rest of unread reads when opening Telethon for
    chat analyze. So a successful install makes subsequent TG-link
    handlers Just Work, no further plumbing required.

    Never raises: any failure is reported back to the owner via a
    chat reply and the pending flag is cleared so the next document
    flows through the normal file handler.
    """
    chat_state = app._chat_state.setdefault(event.chat_id, {})
    chat_state["pending_session_upload"] = False
    s = get_settings()
    # Telethon's SQLiteSession appends `.session` to the path you give
    # it, so the actual on-disk file the CLI's `build_client` reads is
    # `<session_path>.session`. If we wrote the upload to the bare
    # name, `build_client` would open an empty new session file beside
    # it. Normalize once here so the install matches what every reader
    # expects, regardless of whether the operator's config has the
    # `.session` suffix or not.
    target = _normalized_session_path(s.telegram.session_path)

    name = _name_of_attachment(event)
    size = _size_of_attachment(event)

    if size is not None and size > _MAX_SESSION_BYTES:
        await event.reply(
            f"That file is {size / 1_000_000:.1f} MB — bigger than the "
            f"{_MAX_SESSION_BYTES // 1_000_000} MB safety cap. A session "
            "file should be well under 10 MB."
        )
        return
    if not name.endswith((".sqlite", ".session")):
        await event.reply(
            f"Refusing to install `{name}`: expected a `.sqlite` or "
            "`.session` file. (Telethon's default session file is "
            "`session.sqlite`.)"
        )
        return

    # Stage into a tempfile so a half-downloaded blob can't clobber the
    # currently-active session if something goes wrong mid-transfer.
    tmp_dir = Path(tempfile.mkdtemp(prefix="unread-bot-session-"))
    staged = tmp_dir / "candidate.sqlite"
    try:
        assert app.bot_client is not None
        downloaded = await app.bot_client.download_media(event.message, file=str(staged))
        if downloaded is None:
            await event.reply("⚠️ Download failed — no data received.")
            return

        # Combined validate + owner-id probe: must be authorized, AND
        # we want the user_id baked into the session so we can refresh
        # the allowlist after install. Probes the candidate file
        # directly (not through build_client) because we need to point
        # Telethon at the staged path, not the configured one.
        derived_owner = await _probe_candidate_owner_id(Path(downloaded), s)
        if derived_owner is None:
            await event.reply(
                "⚠️ That session file isn't authorized (or didn't load).\n\n"
                "Re-export it on a host that's already logged in: Telethon "
                "stores it as `session.sqlite.session` (with the `.session` "
                "suffix). On your laptop, copy the file shown by\n"
                "`ls ~/.unread/storage/session.sqlite.session`\n"
                "and send THAT file back here as a document."
            )
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(downloaded), str(target))
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
        app.user_session_ready = True
        # Session-derived owner wins. If env-var owner_id was a
        # bootstrap allowlist (or a typo), this swap brings the
        # allowlist in line with the actual session.
        previous = app.owner_id
        if previous and previous != derived_owner:
            log.warning(
                "bot.owner_id.session_overrides_env",
                env_owner_id=previous,
                session_owner_id=derived_owner,
            )
        app.owner_id = derived_owner
        await event.reply("✓ Session installed. You can now send `t.me/...` links.")
        log.info("bot.session.installed", path=str(target), owner_id=derived_owner)
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _normalized_session_path(configured: Path) -> Path:
    """Return the actual on-disk filename Telethon will read for `configured`.

    Telethon's `SQLiteSession` constructor appends `.session` to the
    name you pass unless it's already there. So a config like
    `session.sqlite` (the project default) results in `session.sqlite.session`
    on disk. We normalize once so the upload destination matches what
    the next `build_client()` call will look for.
    """
    s = str(configured)
    return Path(s if s.endswith(".session") else s + ".session")


async def _probe_candidate_owner_id(candidate: Path, settings) -> int | None:
    """Open `candidate` as a Telethon session and return the owner's user_id.

    Returns None on missing / empty / unauthorized / any error.
    Specific to the upload validator — `unread.bot.app._probe_session_owner_id`
    can't be reused here because it goes through `build_client(settings)`,
    which would open the *configured* path, not our staged candidate.
    """
    from telethon import TelegramClient

    if not candidate.exists() or candidate.stat().st_size == 0:
        return None
    client = TelegramClient(
        str(candidate),
        api_id=settings.telegram.api_id,
        api_hash=settings.telegram.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        if me is None or not getattr(me, "id", 0):
            return None
        return int(me.id)
    except Exception:
        log.exception("bot.session.candidate_probe_failed")
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def _name_of_attachment(event: events.NewMessage.Event) -> str:
    """Best-effort filename extraction from the message's document attribute."""
    msg = event.message
    media = msg.media
    if media is None:
        return ""
    doc = getattr(media, "document", None)
    if doc is None:
        return ""
    for attr in getattr(doc, "attributes", []) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return ""


def _size_of_attachment(event: events.NewMessage.Event) -> int | None:
    media = event.message.media
    if media is None:
        return None
    doc = getattr(media, "document", None)
    if doc is None:
        return None
    return getattr(doc, "size", None)
