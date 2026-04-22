"""Download + ffmpeg preprocessing for voice / videonote / video messages."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from analyzetg.config import get_settings
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)

MAX_OPENAI_MB = 24  # OpenAI audio API 25 MB limit; leave a 1 MB safety margin.


class FfmpegMissing(RuntimeError):
    """Raised when the configured ffmpeg binary isn't on PATH."""


async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def _ffmpeg_present(path: str) -> bool:
    try:
        rc, _, _ = await _run([path, "-version"])
        return rc == 0
    except FileNotFoundError:
        return False


async def download_message(client: TelegramClient, msg_obj, out_path: Path) -> Path:
    """Download a Telethon message's media to `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = await client.download_media(msg_obj, file=str(out_path))
    if result is None:
        raise RuntimeError(f"download_media returned None for msg={msg_obj.id}")
    return Path(result)


async def transcode_for_openai(src: Path, media_type: str, tmp_dir: Path) -> list[Path]:
    """Prepare audio for OpenAI:
      - voice (.ogg/opus): pass through.
      - videonote/video: extract mono 16 kHz mp3 at 64k.
      - split into ≤600 s segments if file > 25 MB.

    Returns a list of 1+ files; the caller transcribes each segment in order.
    """
    settings = get_settings()
    ffmpeg = settings.media.ffmpeg_path
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if media_type == "voice":
        prepared = src
    else:
        if not await _ffmpeg_present(ffmpeg):
            raise FfmpegMissing(
                f"ffmpeg not found at '{ffmpeg}'. Install ffmpeg or update config.media.ffmpeg_path."
            )
        prepared = tmp_dir / f"{src.stem}_prep.mp3"
        rc, _, err = await _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                str(prepared),
            ]
        )
        if rc != 0:
            raise RuntimeError(f"ffmpeg transcode failed: {err.decode(errors='ignore')[:500]}")

    size_mb = prepared.stat().st_size / (1024 * 1024)
    if size_mb <= MAX_OPENAI_MB:
        return [prepared]

    # Need to chunk. Re-encode voice to mp3 first if we didn't already.
    intermediate: Path | None = None
    if media_type == "voice":
        if not await _ffmpeg_present(ffmpeg):
            raise FfmpegMissing(f"ffmpeg required for chunking voice >{MAX_OPENAI_MB} MB.")
        normalized = tmp_dir / f"{src.stem}_voice.mp3"
        rc, _, err = await _run(
            [ffmpeg, "-y", "-i", str(prepared), "-ac", "1", "-b:a", "64k", str(normalized)]
        )
        if rc != 0:
            raise RuntimeError(f"ffmpeg voice→mp3 failed: {err.decode(errors='ignore')[:500]}")
        intermediate = normalized
        prepared = normalized

    seg_pattern = tmp_dir / f"{src.stem}_chunk_%03d.mp3"
    rc, _, err = await _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(prepared),
            "-f",
            "segment",
            "-segment_time",
            "600",
            "-c",
            "copy",
            str(seg_pattern),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"ffmpeg segment failed: {err.decode(errors='ignore')[:500]}")
    chunks = sorted(tmp_dir.glob(f"{src.stem}_chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg produced no chunks")
    # Intermediate re-encode (only created for voice) served only as input to the
    # segmenter — no longer needed.
    if intermediate is not None and intermediate not in chunks:
        with contextlib.suppress(FileNotFoundError):
            intermediate.unlink()
    return chunks


def sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1 << 16), b""):
            h.update(buf)
    return h.hexdigest()
