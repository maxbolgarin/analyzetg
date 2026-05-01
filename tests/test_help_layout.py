"""Tests for the redesigned help layout.

Pins three contracts:

  1. `unread help` (no args) shows status + ref types + grouped command
     list, and does NOT spill the analyze flag dump.
  2. `unread help <cmd>` and `unread <cmd> --help` produce byte-
     identical output across every command we care about.
  3. `unread help flags` exposes the root callback's flags (the ones
     accepted by `unread <ref>`) so users have a discoverable place
     to find them — without claiming an `analyze` subcommand exists.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from unread.cli import _REF_TYPES, app


def _invoke(*args: str) -> str:
    """Run `unread <args>` via CliRunner; return combined stdout/stderr.

    Strips ANSI codes since CliRunner runs in non-TTY mode but rich
    can still emit them in some configurations. Also strips trailing
    whitespace per line so terminal-width wrapping doesn't make the
    `help X` vs `X --help` byte-identical comparison flaky.
    """
    runner = CliRunner()
    result = runner.invoke(app, list(args))
    text = result.output
    # Strip ANSI escape sequences (rich color codes when force_terminal).
    import re

    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    # Strip trailing whitespace from each line, then strip overall
    # trailing whitespace so a stray final newline difference between
    # the two help paths doesn't fail the byte-identical assertion.
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip()


def test_help_overview_lists_every_panel() -> None:
    """The no-arg help has the three panel headers in order.

    The `(Telegram)` suffix on the Sync panel is the user-facing
    signal that those commands need a Telegram session — pinned here
    so the marker can't silently disappear.
    """
    out = _invoke("help")
    # Panel headers from i18n (en defaults).
    assert "Main" in out
    assert "Sync & subscriptions (Telegram)" in out
    assert "Maintenance" in out
    # Panel order: Main → Sync → Maintenance.
    assert out.index("Main") < out.index("Sync & subscriptions (Telegram)") < out.index("Maintenance")


def test_help_overview_lists_visible_commands() -> None:
    """Every non-hidden registered command appears in the overview.

    `tg` is intentionally absent — it became a magic ref token (see the
    `<ref> can be` block), not a subcommand. The Telegram setup verbs
    (`login`, `logout`, `describe`, `folders`) live at the top level.
    """
    out = _invoke("help")
    # Sample of commands we know are registered and visible.
    for name in (
        "ask",
        "dump",
        "init",
        "login",
        "logout",
        "describe",
        "folders",
        "sync",
        "chats",
        "stats",
        "cleanup",
        "settings",
        "doctor",
        "backup",
        "migrate",
        "cache",
        "reports",
    ):
        assert name in out, f"`{name}` missing from overview"
    # `tg` should NOT appear as a Commands-table entry — it's a ref now.
    # Use a strict match so we don't catch substrings like "settings".
    import re

    assert not re.search(r"^\s*tg\s", out, re.MULTILINE), (
        "`tg` should be a ref, not a subcommand row"
    )


def test_help_overview_lists_every_ref_form() -> None:
    """The `<ref> can be` block surfaces every entry from `_REF_TYPES`."""
    out = _invoke("help")
    assert "<ref> can be" in out
    for form, _desc in _REF_TYPES:
        assert form in out, f"ref form `{form}` missing from overview"


def test_help_overview_omits_analyze_flag_dump() -> None:
    """Regression guard: the overview must NOT spill the analyze flags.

    These flags belong on `unread help flags`. Pre-redesign they
    leaked into every `unread help` invocation because the root
    callback is the analyze command and Typer's default `--help`
    rendered every option.
    """
    out = _invoke("help")
    for flag in ("--from-msg", "--last-days", "--enrich", "--max-cost", "--prompt-file"):
        assert flag not in out, f"overview should not list `{flag}`; pin to `help flags`"


def test_help_overview_shows_status() -> None:
    """The Status block (full multi-line panel) leads the overview."""
    out = _invoke("help")
    assert "Status" in out
    assert "Install:" in out
    assert "AI provider:" in out
    assert "Telegram:" in out
    # Status appears BEFORE the Commands section.
    assert out.index("Status") < out.index("Commands")


def test_help_flags_exposes_root_options() -> None:
    """`unread help flags` is the canonical place for the root flags."""
    out = _invoke("help", "flags")
    # A representative sample.
    assert "--last-days" in out
    assert "--enrich" in out
    assert "--max-cost" in out
    assert "--from-msg" in out
    # Has the ref cheat-sheet too.
    assert "<ref> can be" in out
    # Usage line uses `<ref>` (not `analyze`, which isn't a real subcommand).
    assert "unread <ref>" in out
    assert "unread analyze" not in out


@pytest.mark.parametrize(
    "command",
    [
        ["ask"],
        ["dump"],
        ["doctor"],
        ["settings"],
        ["init"],
        ["sync"],
        ["stats"],
        ["cleanup"],
        ["backup"],
        ["backup", "up"],
        ["backup", "restore"],
        ["migrate"],
        ["login"],
        ["logout"],
        ["describe"],
        ["folders"],
        ["chats", "add"],
        ["chats", "manage"],
        ["chats", "run"],
    ],
)
def test_help_command_matches_dash_help(command: list[str]) -> None:
    """`unread help <cmd>` and `unread <cmd> --help` produce identical output."""
    via_help = _invoke("help", *command)
    via_dash = _invoke(*command, "--help")
    # Both surfaces must produce the same body. Allow a one-line
    # divergence on the program-name part of the Usage line if
    # CliRunner injects the script name; we already normalise that
    # via `_command_path` so they should match exactly.
    assert via_help == via_dash, (
        f"`help {' '.join(command)}` differs from `{' '.join(command)} --help`:\n"
        f"--- help ---\n{via_help}\n--- --help ---\n{via_dash}"
    )


def test_help_command_one_liner_present() -> None:
    """Per-command help leads with the compact `unread · ...` status line."""
    out = _invoke("help", "ask")
    assert "unread ·" in out


def test_help_overview_via_dash_help_matches_help_no_args() -> None:
    """`unread --help` and `unread help` (no args) produce identical output."""
    via_help = _invoke("help")
    via_dash = _invoke("--help")
    assert via_help == via_dash, (
        f"`unread help` differs from `unread --help`:\n--- help ---\n{via_help}\n--- --help ---\n{via_dash}"
    )


def test_help_unknown_command_errors_cleanly() -> None:
    """Unknown subcommand under `help` raises BadParameter, not crashes."""
    runner = CliRunner()
    result = runner.invoke(app, ["help", "nonexistent_xyzzy"])
    assert result.exit_code != 0
    assert "unknown command" in result.output.lower() or "no such" in result.output.lower()


def test_help_chats_lists_subcommands() -> None:
    """`unread help chats` lists the sub-typer's children."""
    out = _invoke("help", "chats")
    assert "Subcommands" in out
    for sub in ("add", "manage", "run"):
        assert sub in out, f"chats subcommand `{sub}` missing"


def test_help_lists_telegram_setup_commands() -> None:
    """The promoted top-level Telegram commands all show up in the overview."""
    out = _invoke("help")
    for cmd in ("login", "logout", "describe", "folders"):
        assert cmd in out, f"top-level `{cmd}` missing from overview"


def test_help_sync_and_chats_flag_telegram_dependency() -> None:
    """The strict-TG commands surface a `Telegram` cue in their one-liners.

    Per the plan: only `sync` and the `chats` group strictly need a
    Telegram session, so they get the inline marker. `ask` / `dump`
    are deliberately left untagged — they're conceptually multi-source
    (local archive Q&A, future YouTube / web-page exports) and tagging
    them as Telegram-only would mislead users.
    """
    overview = _invoke("help")
    # Match the row that *starts* with the bare command name to avoid
    # catching unrelated rows that happen to contain "chat" / "sync"
    # (e.g. `describe`'s help text mentions "chats").
    import re

    sync_line = next(
        (ln for ln in overview.splitlines() if re.match(r"^\s*sync\b", ln)),
        "",
    )
    chats_line = next(
        (ln for ln in overview.splitlines() if re.match(r"^\s*chats\b", ln)),
        "",
    )
    assert "Telegram" in sync_line, f"sync line missing Telegram cue: {sync_line!r}"
    assert "Telegram" in chats_line, f"chats line missing Telegram cue: {chats_line!r}"


def test_ask_and_dump_help_do_not_claim_telegram_only() -> None:
    """`ask` and `dump` are dual-source — their help must not say
    'needs Telegram' (it would mislead users with a YouTube/website
    archive who rely on local-only ask)."""
    for cmd in ("ask", "dump"):
        out = _invoke("help", cmd).lower()
        # The tolerant negative match: don't dictate the exact phrasing,
        # just guard against the inline "needs telegram" badge that
        # we attach to sync / chats.
        assert "needs telegram" not in out, f"`{cmd}` help should not claim 'needs Telegram'"
