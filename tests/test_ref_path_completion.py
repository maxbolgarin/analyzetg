"""Tab-completion for the `<ref>` positional includes local file paths."""

from __future__ import annotations


def test_path_prefix_detector_only_fires_on_pathy_strings() -> None:
    """Bare words like 'cleanup' or '@user' are NOT path prefixes."""
    from unread.cli import _looks_like_path_prefix

    assert _looks_like_path_prefix("./re") is True
    assert _looks_like_path_prefix("/abs/path") is True
    assert _looks_like_path_prefix("~/Documents") is True
    assert _looks_like_path_prefix("../sibling") is True
    assert _looks_like_path_prefix("dir/file") is True

    assert _looks_like_path_prefix("") is False
    assert _looks_like_path_prefix("cleanup") is False
    assert _looks_like_path_prefix("@somegroup") is False
    assert _looks_like_path_prefix("https://example.com") is False
    assert _looks_like_path_prefix("tg") is False


def test_complete_path_prefix_matches_files_and_dirs(tmp_path, monkeypatch) -> None:
    """Completion enumerates dir contents matching the partial name."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.pdf").write_text("a")
    (tmp_path / "report-old.pdf").write_text("b")
    (tmp_path / "notes.md").write_text("c")
    (tmp_path / "subdir").mkdir()

    from unread.cli import _complete_path_prefix

    matches = _complete_path_prefix("./re")
    assert "./report.pdf" in matches
    assert "./report-old.pdf" in matches
    # notes.md doesn't start with "re"
    assert "./notes.md" not in matches


def test_complete_path_prefix_appends_slash_for_dirs(tmp_path, monkeypatch) -> None:
    """Directories get a trailing slash so the user can keep tabbing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "subdir").mkdir()
    (tmp_path / "leaf.txt").write_text("a")

    from unread.cli import _complete_path_prefix

    matches = _complete_path_prefix("./")
    assert "./subdir/" in matches
    assert "./leaf.txt" in matches
    # Hidden files excluded by default
    (tmp_path / ".secret").write_text("hidden")
    matches = _complete_path_prefix("./")
    assert "./.secret" not in matches
    # …unless the user typed a leading dot
    matches = _complete_path_prefix("./.")
    assert "./.secret" in matches


def test_complete_path_prefix_returns_empty_for_non_pathy_input() -> None:
    """Bare words don't trip path completion (subcommand fallback owns them)."""
    from unread.cli import _complete_path_prefix

    assert _complete_path_prefix("cleanup") == []
    assert _complete_path_prefix("@user") == []
    assert _complete_path_prefix("") == []


def test_root_ref_completion_returns_paths_when_pathy(tmp_path, monkeypatch) -> None:
    """`unread ./re<Tab>` returns path matches, not subcommand names."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.pdf").write_text("a")

    from unread.cli import _complete_root_ref

    # Build a fake ctx whose .command has list_commands/get_command — we
    # only need them when the path branch isn't taken.
    class _FakeCommand:
        def list_commands(self, ctx):
            return ["cleanup", "doctor"]

        def get_command(self, ctx, name):
            class _C:
                hidden = False
                help = ""

            return _C()

    class _FakeCtx:
        command = _FakeCommand()

    matches = _complete_root_ref(_FakeCtx(), [], "./re")
    # Path branch wins: only path entries returned, no subcommand tuples.
    assert any(m == "./report.pdf" for m in matches)
    assert not any(isinstance(m, tuple) for m in matches)


def test_root_ref_completion_returns_subcommands_when_bare_word() -> None:
    """`unread cle<Tab>` returns subcommand names."""

    class _FakeCommand:
        def list_commands(self, ctx):
            return ["cleanup", "describe"]

        def get_command(self, ctx, name):
            class _C:
                hidden = False
                help = f"{name} help"

            return _C()

    class _FakeCtx:
        command = _FakeCommand()

    from unread.cli import _complete_root_ref

    matches = _complete_root_ref(_FakeCtx(), [], "cle")
    assert ("cleanup", "cleanup help") in matches
    assert ("describe", "describe help") not in matches


def test_ask_dump_ref_use_path_only_completion(tmp_path, monkeypatch) -> None:
    """`unread ask ./re<Tab>` and `unread dump ./re<Tab>` complete file paths."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.pdf").write_text("a")

    from unread.cli import _complete_ref

    # `_complete_ref` intentionally has no subcommand fallback — it's
    # used by `ask` and `dump`, neither of which has nested subcommands
    # competing with the ref positional.
    matches = _complete_ref(None, [], "./re")
    assert "./report.pdf" in matches

    # Bare words on ask/dump get no suggestions (they're TG handles or
    # URLs, both dynamic).
    assert _complete_ref(None, [], "cle") == []
    assert _complete_ref(None, [], "@user") == []
