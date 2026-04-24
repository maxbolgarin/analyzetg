"""Silent-video detection in the ffmpeg failure path.

Screen recordings and GIF-uploaded-as-video have no audio track, so
`ffmpeg -vn -ac 1 ... -b:a 64k out.mp3` ends with zero output streams
and exits non-zero. That's not a bug — just skip the file.
"""

from analyzetg.media.download import NoAudioStream, _ffmpeg_fail, _is_no_audio_stream


def test_detects_no_audio_stream_needle():
    stderr = (
        b"[out#0/mp3] Output file does not contain any stream\nError opening output files: Invalid argument\n"
    )
    assert _is_no_audio_stream(stderr)


def test_detects_variant_phrasing():
    stderr = b"Output file #0 does not contain any stream\n"
    assert _is_no_audio_stream(stderr)


def test_real_error_is_not_no_audio():
    stderr = b"[mp3 @ 0xabc] Invalid codec: opus\nError opening output.\n"
    assert not _is_no_audio_stream(stderr)


def test_empty_stderr_is_not_no_audio():
    assert not _is_no_audio_stream(b"")


def test_ffmpeg_fail_returns_noaudio_for_silent_video():
    stderr = b"Output file does not contain any stream\n"
    exc = _ffmpeg_fail(["ffmpeg", "-i", "in.mp4", "out.mp3"], stderr, "transcode")
    assert isinstance(exc, NoAudioStream)


def test_ffmpeg_fail_returns_runtime_for_other_failures():
    stderr = b"[mp3 @ 0xdef] broken pipe\n"
    exc = _ffmpeg_fail(["ffmpeg"], stderr, "transcode")
    # Must be the generic RuntimeError branch, not NoAudioStream.
    assert not isinstance(exc, NoAudioStream)
    assert "ffmpeg transcode failed" in str(exc)
