"""YouTube URL detection + video-id extraction.

Pure stdlib (urllib.parse). No yt-dlp here — keeps `is_youtube_url` cheap
to call from `cmd_analyze`'s detection branch without forcing the heavy
import. Playlist-only / channel-only links raise ValueError so the caller
can produce a clean error message instead of fetching the wrong thing.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# yt-dlp accepts these as the canonical 11-char video id alphabet, plus the
# occasional 12+ char id Google has used on test rollouts. We don't validate
# strictly: anything that isn't obvious garbage is forwarded to yt-dlp,
# which has the authoritative parser.
_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_youtube_url(s: str | None) -> bool:
    """True for any string that *looks* like a YouTube link.

    Cheap substring check via urllib parse — no validation that the video
    actually exists. Used only to decide which branch of `cmd_analyze`
    should handle the ref.
    """
    if not s:
        return False
    if not s.startswith(("http://", "https://", "//")):
        return False
    return _host(s) in _HOSTS


def extract_video_id(url: str) -> str:
    """Extract the 11-char video id from any supported YouTube URL shape.

    Drops `list=`, `start_radio`, `t=`, and any other query params. Raises
    `ValueError` for shapes we cannot turn into a single-video analysis
    (channel pages, playlist-only links, malformed URLs).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == "youtu.be":
        # https://youtu.be/<id>[?...]
        vid = path.lstrip("/").split("/", 1)[0]
        if vid:
            return vid
        raise ValueError(f"no video id in youtu.be URL: {url!r}")

    if host not in _HOSTS:
        raise ValueError(f"not a YouTube URL: {url!r}")

    # /watch?v=<id>  — the canonical desktop / music form.
    if path in ("/watch", "/watch/"):
        qs = parse_qs(parsed.query)
        vids = qs.get("v") or []
        if vids and vids[0]:
            return vids[0]
        raise ValueError(f"no v= param in watch URL: {url!r}")

    # /shorts/<id>, /embed/<id>, /live/<id>, /v/<id> — single-segment paths.
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if path.startswith(prefix):
            vid = path[len(prefix) :].split("/", 1)[0]
            if vid:
                return vid

    # /playlist, /channel/<id>, /@handle, /feed/* — not a single video.
    raise ValueError(f"not a single-video YouTube URL (path={path!r}): {url!r}")


def video_url(video_id: str) -> str:
    """Canonical watch URL for storage / report metadata."""
    return f"https://www.youtube.com/watch?v={video_id}"
