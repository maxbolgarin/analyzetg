"""Tab-completion for the `<ref>` positional delegates path completion to the shell."""

from __future__ import annotations


def test_path_prefix_detector_only_fires_on_pathy_strings() -> None:
    """Bare words like 'cleanup' or '@user' are NOT path prefixes."""
    from unread.cli import _looks_like_path_prefix

    assert _looks_like_path_prefix("./re") is True
    assert _looks_like_path_prefix("/abs/path") is True
    assert _looks_like_path_prefix("~/Documents") is True
    assert _looks_like_path_prefix("../sibling") is True
    assert _looks_like_path_prefix("dir/file") is True
    assert _looks_like_path_prefix(".") is True
    assert _looks_like_path_prefix("..") is True

    assert _looks_like_path_prefix("") is False
    assert _looks_like_path_prefix("cleanup") is False
    assert _looks_like_path_prefix("@somegroup") is False
    # URLs are not path prefixes (a glob would lie about remote files).
    assert _looks_like_path_prefix("https://example.com") is False
    assert _looks_like_path_prefix("t.me/c/123") is False
    assert _looks_like_path_prefix("tg") is False


def test_complete_path_prefix_delegates_to_shell_for_pathy_input() -> None:
    """Pathy prefixes return a CompletionItem(type='file'); the shell does the glob.

    The actual filesystem enumeration happens inside the shell's native
    file-completion machinery (`_path_files -f` in zsh, `__fish_complete_path`
    in fish). Those handle no-trailing-space-after-files automatically —
    which is the whole point of delegating instead of returning plain strings
    that would route through `compadd -U` and add a trailing space.
    """
    from click.shell_completion import CompletionItem

    from unread.cli import _complete_path_prefix

    matches = _complete_path_prefix("./re")
    assert len(matches) == 1
    assert isinstance(matches[0], CompletionItem)
    assert matches[0].type == "file"


def test_complete_path_prefix_returns_empty_for_non_pathy_input() -> None:
    """Bare words don't trip path completion (subcommand fallback owns them)."""
    from unread.cli import _complete_path_prefix

    assert _complete_path_prefix("cleanup") == []
    assert _complete_path_prefix("@user") == []
    assert _complete_path_prefix("") == []
    assert _complete_path_prefix("https://example.com") == []


def test_root_ref_completion_delegates_paths_when_pathy() -> None:
    """`unread ./re<Tab>` returns a 'file' CompletionItem, no subcommand names."""
    from click.shell_completion import CompletionItem

    from unread.cli import _complete_root_ref

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
    # Path branch wins: only the file-completion sentinel is returned.
    assert len(matches) == 1
    assert isinstance(matches[0], CompletionItem)
    assert matches[0].type == "file"


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


def test_ask_dump_ref_use_path_only_completion() -> None:
    """`unread ask ./re<Tab>` and `unread dump ./re<Tab>` delegate to shell file completion."""
    from click.shell_completion import CompletionItem

    from unread.cli import _complete_ref

    matches = _complete_ref(None, [], "./re")
    assert len(matches) == 1
    assert isinstance(matches[0], CompletionItem)
    assert matches[0].type == "file"

    # Bare words on ask/dump get no suggestions (they're TG handles or
    # URLs, both dynamic).
    assert _complete_ref(None, [], "cle") == []
    assert _complete_ref(None, [], "@user") == []
