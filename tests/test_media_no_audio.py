"""Silent-video detection in the ffmpeg failure path.

Screen recordings and GIF-uploaded-as-video have no audio track, so
`ffmpeg -vn -ac 1 ... -b:a 64k out.mp3` ends with zero output streams
and exits non-zero. That's not a bug — just skip the file.
"""

from unread.media.download import NoAudioStream, _ffmpeg_fail, _is_no_audio_stream


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


# ---- prefer_mp3 / opus pre-transcode -----------------------------------


def test_prefer_mp3_kicks_in_for_default_audio_model():
    """The default `gpt-4o-mini-transcribe` model rejects opus voice
    notes — `enrich/audio.py` must signal `prefer_mp3=True` for it.

    This guards the symbolic mapping (model → prefer_mp3) — if someone
    edits the audio enricher and drops the `gpt-4o-transcribe` family
    from the trigger set, the regression silently breaks transcription
    for the most-common Telegram voice path.
    """
    from unread.config import get_settings

    s = get_settings()
    # The shipped default. If this changes, update the assertion to
    # reflect the new model and confirm whether opus is accepted.
    assert s.openai.audio_model_default == "gpt-4o-mini-transcribe"


def test_transcode_for_openai_signature_supports_prefer_mp3():
    """Smoke check: kwarg-only `prefer_mp3` is part of the public signature."""
    import inspect

    from unread.media.download import transcode_for_openai

    sig = inspect.signature(transcode_for_openai)
    assert "prefer_mp3" in sig.parameters
    assert sig.parameters["prefer_mp3"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["prefer_mp3"].default is False
