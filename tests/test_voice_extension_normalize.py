"""Voice files must reach OpenAI with a `.ogg` filename suffix.

Real-world failure (forum analysis, 67/67 voice messages failing):
`download_message` uses an atomic `.part` rename. Telethon's
`_get_proper_filename` (see `.venv/.../telethon/client/downloads.py`)
sees `<path>.part` as having extension `.part` and refuses to add the
auto-detected `.oga`. After the rename, the file lives at `<path>` with
no extension. OpenAI's Whisper endpoint detects format from the
upload's filename — extensionless → 400 `Unsupported file format`.

`transcode_for_openai` must normalize voice files to `.ogg` regardless
of the input suffix so the bytes ride into Whisper with a recognized
filename.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_voice_no_extension_renamed_to_ogg(tmp_path: Path):
    """The buggy real-world path: voice file arrives with no suffix at all.
    Without normalization OpenAI rejects with 400 `Unsupported file format`.
    """
    from unread.media.download import transcode_for_openai

    src = tmp_path / "1234_5678"  # No extension — matches audio.py's src construction.
    src.write_bytes(b"OggS\x00\x02")  # Minimal OggS header bytes — enough for the rename path.

    parts = await transcode_for_openai(src, "voice", tmp_path, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0].suffix.lower() == ".ogg", (
        f"voice files must reach OpenAI with a .ogg suffix; got {parts[0].name!r}"
    )
    assert parts[0].exists()


@pytest.mark.asyncio
async def test_voice_oga_renamed_to_ogg(tmp_path: Path):
    """Telethon's historical naming (`.oga`) is also normalized — OpenAI's
    filename whitelist accepts `.oga` and `.ogg` both, but the prefer_mp3
    branch keys off `.ogg`, so we collapse here for consistency."""
    from unread.media.download import transcode_for_openai

    src = tmp_path / "1234_5678.oga"
    src.write_bytes(b"OggS\x00\x02")

    parts = await transcode_for_openai(src, "voice", tmp_path, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0].suffix.lower() == ".ogg"
    assert parts[0].exists()


@pytest.mark.asyncio
async def test_voice_ogg_passes_through(tmp_path: Path):
    """A file already named `.ogg` stays put — no needless rename."""
    from unread.media.download import transcode_for_openai

    src = tmp_path / "1234_5678.ogg"
    src.write_bytes(b"OggS\x00\x02")

    parts = await transcode_for_openai(src, "voice", tmp_path, prefer_mp3=False)

    assert len(parts) == 1
    assert parts[0] == src
    assert parts[0].exists()


def test_download_message_part_trick_strips_extension():
    """Lock the upstream behavior so the transcoder-side workaround stays
    justified. Telethon's `_get_proper_filename` sees `.part` as the
    existing extension and does NOT add the auto-detected one — meaning
    `download_message`'s atomic rename leaves the file extensionless.
    """
    import os

    from telethon.client.downloads import DownloadMethods

    # Telethon's helper with a `.part` path: should keep `.part` and NOT
    # add the proposed `.oga` extension.
    result = DownloadMethods._get_proper_filename(
        os.path.join(os.sep + "tmp", "1234_5678.part"),
        "document",
        ".oga",
    )
    # Path is unchanged — Telethon respects the existing (`.part`) suffix.
    assert result.endswith(".part"), (
        f"Telethon helper unexpectedly rewrote the path: {result!r}. "
        "If this assertion flips, revisit transcode_for_openai's voice branch "
        "— the no-extension workaround may no longer be necessary."
    )


# Avoid Telethon import-time noise if the SDK isn't available in some envs.
def _telethon_available() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark_telethon = pytest.mark.skipif(
    not _telethon_available(),
    reason="telethon not installed",
)
