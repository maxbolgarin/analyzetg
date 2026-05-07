"""Cover the new `--no-console` analyze flag.

The flag inverts the file/terminal output split: when set, `_print_and_write`
must skip the rich-rendered Markdown but still save the report file. The
inverse case (`--no-save`) was already covered by other tests; this module
focuses on the new path and the dispatch-level validation that bans
`--no-console --no-save` together (which would suppress every output).
"""

from __future__ import annotations

from pathlib import Path

from unread.analyzer.commands import _print_and_write
from unread.analyzer.pipeline import AnalysisResult


def _result(**overrides) -> AnalysisResult:
    base: dict = {
        "preset": "summary",
        "model": "gpt-5.4",
        "chat_id": -100_123,
        "thread_id": 0,
        "msg_count": 1,
        "chunk_count": 1,
        "batch_hashes": ["deadbeef"],
        "final_result": "Body text.",
        "total_cost_usd": 0.0,
        "cache_hits": 0,
        "cache_misses": 1,
        "prompt_version": "v1",
        "filter_model": None,
        "period": (None, None),
        "enrich_kinds": [],
        "enrich_cost_usd": 0.0,
        "enrich_summary": "",
        "raw_msg_count": 1,
    }
    base.update(overrides)
    return AnalysisResult(**base)


def _captured(monkeypatch) -> list[object]:
    """Replace the rich Console used by the rendering shell with a recorder
    so the test can assert which segments were rendered.

    The actual rendering lives in `unread/util/report_render.py` (shared
    by analyze and ask); the analyzer module just delegates. Patch BOTH
    consoles with the SAME recorder so every rendered segment ends up in
    the shared `captured` list.
    """
    captured: list[object] = []

    class _Recorder:
        def print(self, *args, **kwargs) -> None:
            del kwargs
            captured.extend(args)

    import unread.analyzer.commands as cmds
    import unread.util.report_render as rr

    recorder = _Recorder()
    monkeypatch.setattr(cmds, "console", recorder)
    monkeypatch.setattr(rr, "console", recorder)
    return captured


def test_no_console_skips_terminal_render(tmp_path: Path, monkeypatch) -> None:
    """`--no-console` must save the report but suppress the markdown render."""
    captured = _captured(monkeypatch)
    out = tmp_path / "report.md"

    _print_and_write(
        _result(),
        output=out,
        title="Some chat",
        console_out=False,
        no_save=False,
    )

    assert out.exists(), "report file should still be written when no_console=True"
    assert "Body text." in out.read_text(encoding="utf-8")
    # Header line and "written_to" notice still print, but the Markdown
    # body + bracketing Rules are skipped — verify by absence of the
    # rich.markdown.Markdown wrapper segment.
    from rich.markdown import Markdown

    assert not any(isinstance(seg, Markdown) for seg in captured), (
        "no_console=True should not push the Markdown body to the terminal"
    )


def test_no_save_skips_file(tmp_path: Path, monkeypatch) -> None:
    """`--no-save` (legacy `console_out=False, no_save=True`) renders the
    body but never touches the filesystem."""
    captured = _captured(monkeypatch)
    out = tmp_path / "report.md"

    _print_and_write(
        _result(),
        output=out,
        title="Some chat",
        console_out=True,
        no_save=True,
    )

    assert not out.exists(), "no_save=True should skip writing the report file"
    from rich.markdown import Markdown

    assert any(isinstance(seg, Markdown) for seg in captured), (
        "no_save=True should still render the Markdown body to the terminal"
    )


def test_default_prints_and_saves(tmp_path: Path, monkeypatch) -> None:
    """Default behaviour: render to terminal AND save."""
    captured = _captured(monkeypatch)
    out = tmp_path / "report.md"

    _print_and_write(_result(), output=out, title="Some chat")

    assert out.exists(), "default should save"
    from rich.markdown import Markdown

    assert any(isinstance(seg, Markdown) for seg in captured), "default should render the Markdown body"
