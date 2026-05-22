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

    The Telegram panel groups all TG-needing commands (describe, login,
    logout, chats, sync) under a single header — pinned here so the
    grouping can't silently disappear.
    """
    out = _invoke("help")
    # Panel headers are rendered as "  <Name>\n" in the overview.
    # Use the indented form to avoid matching "Telegram" inside command
    # descriptions that appear before the panel section.
    assert "  Main\n" in out
    assert "  Telegram\n" in out
    assert "  Maintenance\n" in out
    # Panel order: Main → Telegram → Maintenance.
    assert out.index("  Main\n") < out.index("  Telegram\n") < out.index("  Maintenance\n")


def test_help_overview_lists_visible_commands() -> None:
    """Every non-hidden registered command appears in the overview.

    Telegram-only verbs are flattened under the Telegram panel as
    `tg login`, `tg describe`, `tg logout`. The bare `tg` row is
    intentionally absent — the panel header itself plus the
    `<ref> can be` row already document the bare-`unread tg` picker
    shortcut.

    `tg sync` and `tg chats` are temporarily hidden while the
    subscription-management rework is in flight (`hidden=True` in
    `cli.py`); they're checked separately in
    `test_tg_sync_and_chats_are_currently_hidden_from_help`.
    """
    out = _invoke("help")
    # Cross-panel sample (Main + Maintenance) — these stay at top level.
    # `cleanup` was renamed to `cache tg` in v1.x and is no longer top-level.
    for name in (
        "ask",
        "dump",
        "init",
        "stats",
        "settings",
        "doctor",
        "backup",
        "cache",
        "reports",
    ):
        assert name in out, f"`{name}` missing from overview"
    # Telegram-namespaced verbs are listed flat, prefixed with `tg`.
    for name in ("tg login", "tg logout", "tg describe"):
        assert name in out, f"`{name}` missing from overview"
    # The bare `tg` row should NOT appear as a Commands-table entry —
    # the Telegram panel header already covers it. Scope the check to
    # the Commands section so we don't false-positive on the legitimate
    # `tg` row inside `<ref> can be` / `Common patterns`.
    import re

    commands_idx = out.index("Commands")
    commands_section = out[commands_idx:]
    assert not re.search(r"^\s*tg\s+[A-Z]", commands_section, re.MULTILINE), (
        "bare `tg` row should be hidden — children are listed flat as `tg <verb>`"
    )


def test_tg_sync_and_chats_are_currently_hidden_from_help() -> None:
    """`tg sync` and `tg chats` are intentionally hidden from the help
    overview while the subscription-management rework is in flight.

    The commands themselves remain reachable
    (`unread tg sync --help` / `unread tg chats --help` still work) —
    only the advertising on `unread help` and `unread tg --help` is
    suppressed via `hidden=True` in `cli.py`. When the rework lands,
    drop the `hidden=True` flags AND delete this test (the previous
    `test_help_overview_lists_visible_commands` should grow `tg sync`
    / `tg chats` back into its assertion list).
    """
    overview = _invoke("help")
    import re

    # Neither verb should appear as its own row in the Commands table.
    # We anchor on `re.MULTILINE` so an unrelated mention in usage text
    # (e.g. a `<ref>` example) wouldn't trip the assertion.
    assert not re.search(r"^\s*tg\s+sync\b", overview, re.MULTILINE), (
        "`tg sync` row should be hidden from help while rework is in flight"
    )
    assert not re.search(r"^\s*tg\s+chats\b", overview, re.MULTILINE), (
        "`tg chats` row should be hidden from help while rework is in flight"
    )

    # But each command must still respond to its own --help, so power
    # users / scripts can keep invoking them while hidden.
    sync_help = _invoke("tg", "sync", "--help")
    assert "Usage" in sync_help and "sync" in sync_help, "tg sync --help must still render"
    chats_help = _invoke("tg", "chats", "--help")
    assert "Usage" in chats_help and "chats" in chats_help, "tg chats --help must still render"


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


def test_quickstart_shows_status() -> None:
    """The Status block leads bare `unread` (the orientation snapshot).

    `unread help` is the catalogue page — it intentionally omits Status
    so the actual help content (commands, refs, patterns) doesn't get
    pushed below the fold. The Status panel lives on the bare-invocation
    quickstart instead, where "what's wired up" is the whole point.
    """
    out = _invoke()
    assert "Status" in out
    assert "Install:" in out
    assert "AI provider:" in out
    assert "Telegram:" in out
    # Status leads — comes before the Usage / ref blocks.
    assert out.index("Status") < out.index("Usage")


def test_help_overview_omits_status() -> None:
    """Regression guard: `unread help` must NOT include the Status panel.

    Status duplicates the bare-`unread` quickstart and pushes the actual
    catalogue content below the fold. Pin the new contract.
    """
    out = _invoke("help")
    assert "Status" not in out
    # Spot-check: the Status panel's signature rows shouldn't leak in.
    assert "Install:" not in out
    assert "Security:" not in out


def test_help_overview_shows_common_patterns() -> None:
    """`unread help` documents how `<ref>` composes with subcommands.

    The `Common patterns` block is the discoverable place to learn
    `unread ask <ref> "Q"` / `unread dump <ref>` / `unread tg`.
    """
    out = _invoke("help")
    assert "Common patterns" in out
    assert "unread ask <ref>" in out
    assert "unread dump <ref>" in out
    assert "unread tg" in out


def test_ref_table_lists_tg() -> None:
    """`tg` appears in `<ref> can be` so users discover the picker.

    Pinned because the magic ref is fully wired across analyze / ask /
    dump but invisible to anyone who doesn't already know the trick.
    """
    out = _invoke("help")
    ref_idx = out.index("<ref> can be")
    # Scope to a generous slice after the header, ending at the next
    # major heading so we don't catch the `unread tg` Common-patterns row.
    section = out[ref_idx : ref_idx + 500]
    import re

    assert re.search(r"^\s*tg\s+interactive Telegram", section, re.MULTILINE), (
        "`tg` row missing from <ref> can be section"
    )


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
        ["tg", "sync"],
        ["stats"],
        ["cache", "tg"],
        ["backup"],
        ["backup", "up"],
        ["backup", "restore"],
        ["tg", "login"],
        ["tg", "logout"],
        ["tg", "describe"],
        ["tg", "describe", "folders"],
        ["tg", "chats", "add"],
        ["tg", "chats", "manage"],
        ["tg", "chats", "run"],
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
    """`unread help tg chats` lists the sub-typer's children."""
    out = _invoke("help", "tg", "chats")
    assert "Subcommands" in out
    for sub in ("add", "manage", "run"):
        assert sub in out, f"chats subcommand `{sub}` missing"


def test_help_lists_telegram_setup_commands() -> None:
    """The Telegram setup verbs all show up in the overview as `tg <verb>`."""
    out = _invoke("help")
    for cmd in ("tg login", "tg logout", "tg describe"):
        assert cmd in out, f"`{cmd}` missing from overview"


def test_help_sync_and_chats_flag_telegram_dependency() -> None:
    """The strict-TG commands surface a `Telegram` cue in their one-liners
    (when they're visible in the overview).

    Per the plan: only `sync` and the `chats` group strictly need a
    Telegram session, so they get the inline marker. `ask` / `dump`
    are deliberately left untagged — they're conceptually multi-source
    (local archive Q&A, future YouTube / web-page exports) and tagging
    them as Telegram-only would mislead users.

    NOTE: `tg sync` and `tg chats` are currently hidden from the help
    overview while the subscription-management rework is in flight
    (see `test_tg_sync_and_chats_are_currently_hidden_from_help`).
    Until they're un-hidden, this test inspects the per-command
    `--help` text instead of the overview, so the Telegram-cue
    invariant is still enforced where users see it.
    """
    sync_help = _invoke("tg", "sync", "--help")
    chats_help = _invoke("tg", "chats", "--help")
    assert "Telegram" in sync_help, "`tg sync --help` must surface its Telegram dependency"
    assert "Telegram" in chats_help, "`tg chats --help` must surface its Telegram dependency"


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
