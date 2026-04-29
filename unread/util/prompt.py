"""Reusable interactive prompts: arrow-driven select, single-key yes/no.

This module is the canonical interactive primitive for the CLI. Every
prompt — wizard step, settings menu, dump confirmation, analyze
picker — routes through :func:`confirm` / :func:`select` /
:func:`checkbox` / :func:`ask_text` so keyboard handling and styling
stay consistent everywhere. Calling `typer.prompt` / `typer.confirm`
or `questionary.*` directly is forbidden in new code.

Behavior:

  - :func:`confirm` accepts ``y`` / ``n`` immediately (no Enter),
    Enter takes the default.
  - :func:`select` shows an arrow-key menu. ↑/↓ navigate, ``/`` opens
    a filter, Enter selects. The default value (when set) starts
    highlighted. The active row is rendered with a cyan ``»`` pointer
    and bold text — no background bar, so the highlight matches the
    rest of the terminal aesthetic.
  - :func:`checkbox` is the same UX for multi-select: Space toggles
    the row under the cursor, Enter confirms.
  - :func:`ask_text` is a one-line text prompt with arrow-key history.
    ``password=True`` hides the typed characters — used for API keys
    and 2FA passwords.
  - **Esc and Ctrl-C are equivalent across every primitive: both
    raise KeyboardInterrupt out of the prompt.** Typer catches it at
    the CLI boundary and prints "Aborted!" before exiting. We do this
    so the user can't accidentally continue past a prompt they
    abandoned. Nested menus that need an in-app "back" navigation
    should add an explicit :data:`BACK` row to their choices instead
    of leaning on Esc.
  - :data:`BACK` is the conventional sentinel value to attach to a
    "← Back" :class:`Choice` in nested menus, so callers everywhere
    use the same string.

Falls back to plain typer prompts when stdin/stdout aren't a TTY
(scripts, CI, `unread tg init < answers.txt`). The fallback also
keeps the existing test suite working — tests patch `typer.confirm`
or `typer.prompt` and the fallback path delivers those calls.

These helpers are sync but get called from inside `cmd_init` (which
is async, driven by `asyncio.run`). Questionary internally reaches
for `asyncio.run` of its own, which raises when a loop is already
running. To stay simple at every call site, the sync API hands off
to a worker thread when called from inside a running loop — threads
own no event loop, so questionary spins up its own without crashing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import questionary
import typer
from rich.console import Console

console = Console()


@dataclass(slots=True)
class Choice:
    """One option in a :func:`select` / :func:`checkbox` menu.

    `value` is what the caller gets back; `label` is what the user
    sees. `description` is an optional dim-styled trailer on the same
    line — handy for menu hints like "Press Enter to keep the default".
    """

    value: str
    label: str
    description: str = ""


@dataclass(slots=True)
class _Separator:
    """Non-selectable divider row in a :func:`select` / :func:`checkbox`.

    Construct via :func:`separator`. The label is shown verbatim;
    leave empty for a blank line.
    """

    label: str = ""


def separator(label: str = "") -> _Separator:
    """Build a non-selectable divider row.

    Use to group choices visually (e.g. ``separator("── Settings ──")``).
    Empty label (``separator()``) renders as a blank line.
    """
    return _Separator(label=label)


def _can_interact() -> bool:
    """True iff both stdin and stdout look like a real terminal.

    questionary needs a real TTY for its raw-mode keypress handling.
    The CliRunner in tests, scripted invocations, and `< file` redirects
    all fail this check and fall through to the typer-based fallback —
    which preserves the existing test patches.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


_T = TypeVar("_T")


# Minimal highlight: cyan » pointer + bold text on the active row.
# We deliberately do NOT paint a background bar — it clashed with the
# user's terminal theme and made the picker look heavier than the
# surrounding banner. `class:highlighted` is the only signal for the
# cursor row; `class:selected` (which questionary applies to the row
# matching `default=` and to user-toggled checkboxes) is suppressed
# in select() via `_clear_default_selection`, so it only fires for
# checkbox-marked rows where bold + indicator make sense.
_SELECTED_STYLE = (
    ("qmark", "fg:#5fafff bold"),  # the leading "?" before the question
    ("question", "bold"),
    ("answer", "fg:#5fafff bold"),
    ("pointer", "fg:#5fafff bold"),  # the » arrow on the active row
    ("highlighted", "bold"),  # cursor row: bold only, no background
    ("selected", "noreverse bold"),  # checkbox-marked rows: bold, no bar
    ("instruction", "fg:#808080"),
    ("separator", "fg:#5f5f5f"),
)


# Sentinel returned/checked by callers for "← Back" rows in nested
# menus. Using one canonical string everywhere lets every wizard /
# settings / chats menu share one BACK convention.
BACK = "__back__"


def _run_questionary(call: Callable[[], _T]) -> _T:
    """Invoke a questionary `unsafe_ask()` call without colliding with a
    running event loop.

    Inside `cmd_init` we're already under `asyncio.run`; questionary's
    own `application.run()` reaches for `asyncio.run(...)` again and
    crashes with "cannot be called from a running event loop". Worker
    threads don't share the parent's loop, so spawning a one-shot
    thread lets questionary spin up its own loop normally. When we're
    NOT inside a loop (synchronous call sites), we just invoke
    directly — no thread overhead.
    """
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return call()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(call).result()


def _default_select_instruction() -> str:
    """Localized key-help line shown under every select() prompt.

    Reads the i18n key `wiz_filter_instruction` so this module's wording
    stays in lockstep with the analyze wizard's own pickers (which still
    call questionary.select directly with the same key). Lazy-imported
    so prompt.py can be loaded before i18n is fully wired.
    """
    try:
        from unread.i18n import t as _t

        return _t("wiz_filter_instruction")
    except Exception:
        return "(↑/↓ navigate · / search · Enter select · Esc cancel)"


_CHECKBOX_INSTRUCTION = "(↑/↓ navigate · Space toggle · Enter confirm · Esc cancel)"


def confirm(message: str, *, default: bool = True) -> bool:
    """Single-key yes/no.

    Pressing `y` / `Y` accepts immediately; `n` / `N` declines
    immediately. Enter takes the `default`. Esc and Ctrl-C raise
    KeyboardInterrupt — they cancel the whole command instead of
    quietly returning a default — so the wizard can't continue past
    a prompt the user explicitly abandoned. Typer catches the
    exception at the CLI boundary and prints "Aborted!".
    """
    if not _can_interact():
        # Fallback path: typer.confirm. Preserves test patches that
        # mock `typer.confirm` directly, and works when stdin is a
        # redirect. typer.Abort on Ctrl-C is converted to
        # KeyboardInterrupt so callers see a single cancel signal.
        try:
            return typer.confirm(message, default=default)
        except (typer.Abort, EOFError) as e:
            raise KeyboardInterrupt from e

    def _ask() -> bool:
        q = questionary.confirm(message, default=default, auto_enter=True)
        _bind_escape_cancel(q)
        return bool(q.unsafe_ask())

    return bool(_run_questionary(_ask))


def select(
    message: str,
    *,
    choices: Sequence[Choice | _Separator],
    default_value: str | None = None,
    instruction: str | None = None,
) -> str:
    """Arrow-key selector. Returns the chosen `value`.

    `default_value` (when set) pre-highlights the matching choice and
    is the selection if the user just hits Enter. `instruction` lets
    callers replace the default key-help line that questionary shows
    under the title. `choices` may include :func:`separator` items to
    insert visual dividers — they aren't selectable.

    Esc and Ctrl-C raise KeyboardInterrupt — same as
    :func:`confirm`. Nested menus that need a "back" navigation
    should add an explicit :data:`BACK` row to their choices instead
    of expecting Esc to mean "go back".
    """
    if not any(isinstance(c, Choice) for c in choices):
        raise ValueError("select() needs at least one Choice")
    if not _can_interact():
        result = _fallback_select(message, choices, default_value)
        if result is None:
            raise KeyboardInterrupt
        return result

    from prompt_toolkit.styles import Style

    qchoices = [_to_questionary(c) for c in choices]
    style = Style(list(_SELECTED_STYLE))
    instr = instruction or _default_select_instruction()

    def _ask() -> Any:
        q = questionary.select(
            message,
            choices=qchoices,
            default=default_value,
            instruction=instr,
            # `use_indicator=False` drops questionary's `●` / `○`
            # default-vs-not glyphs so the only visual marker is
            # the cursor's `»`. With the indicator on, questionary
            # draws a second filled row at the default value,
            # which reads like two selections and is confusing.
            use_indicator=False,
            # Numeric shortcuts conflict with our default-via-Enter UX.
            use_shortcuts=False,
            # Type-anywhere filter: matches against the rendered
            # label. No mode toggle — start typing to narrow.
            use_search_filter=True,
            use_jk_keys=False,
            qmark="?",
            style=style,
        )
        # Drop the default-row "marked" state questionary auto-applies;
        # cursor position survives, Enter still returns the right value.
        _clear_default_selection(q)
        _bind_escape_cancel(q)
        return q.unsafe_ask()

    result = _run_questionary(_ask)
    if not isinstance(result, str):
        # questionary should always return a string (or raise on
        # cancel) once a Choice is picked. Treat any other shape as
        # a soft cancel — propagate KeyboardInterrupt for parity.
        raise KeyboardInterrupt
    # Replace questionary's full-title echo with a short label when the
    # picked row had a description trailer (the long version clutters
    # scrollback). Skip in non-TTY: the fallback path didn't print one.
    if _can_interact():
        picked = next((c for c in choices if isinstance(c, Choice) and c.value == result), None)
        if picked is not None and picked.description:
            _echo_short_answer(message, picked.label)
    return result


def checkbox(
    message: str,
    *,
    choices: Sequence[Choice | _Separator],
    defaults: Sequence[str] = (),
    instruction: str | None = None,
) -> list[str]:
    """Multi-select checkbox menu. Returns the chosen values.

    `defaults` is the set of values that start checked. Space toggles
    the row under the cursor; Enter confirms (returning the marked
    list — possibly empty if the user wants to confirm "nothing
    selected"). Esc and Ctrl-C raise KeyboardInterrupt — same as
    :func:`select`. Separators in `choices` are rendered as dividers
    and skipped on navigation.
    """
    if not any(isinstance(c, Choice) for c in choices):
        raise ValueError("checkbox() needs at least one Choice")
    if not _can_interact():
        result = _fallback_checkbox(message, choices, defaults)
        if result is None:
            raise KeyboardInterrupt
        return result

    from prompt_toolkit.styles import Style

    default_set = set(defaults)
    qchoices = [_to_questionary(c, checked=isinstance(c, Choice) and c.value in default_set) for c in choices]
    style = Style(list(_SELECTED_STYLE))
    instr = instruction or _CHECKBOX_INSTRUCTION

    def _ask() -> Any:
        q = questionary.checkbox(
            message,
            choices=qchoices,
            instruction=instr,
            qmark="?",
            style=style,
        )
        _bind_escape_cancel(q)
        return q.unsafe_ask()

    result = _run_questionary(_ask)
    if result is None:
        raise KeyboardInterrupt
    return [v for v in result if isinstance(v, str)]


def ask_text(message: str, *, default: str = "", password: bool = False) -> str:
    """Free-form one-line text input.

    `password=True` hides the typed characters — used for API keys and
    2FA passwords. `default` pre-fills the line and is returned on a
    bare Enter. Esc and Ctrl-C raise KeyboardInterrupt — same as
    :func:`select` / :func:`confirm`.
    """
    if not _can_interact():
        try:
            return typer.prompt(message, default=default, hide_input=password, show_default=False)
        except (typer.Abort, EOFError) as e:
            raise KeyboardInterrupt from e

    def _ask() -> Any:
        q = (
            questionary.password(message, default=default)
            if password
            else questionary.text(message, default=default)
        )
        _bind_escape_cancel(q)
        return q.unsafe_ask()

    result = _run_questionary(_ask)
    if not isinstance(result, str):
        raise KeyboardInterrupt
    return result


# --- internals ----------------------------------------------------------


def _echo_short_answer(message: str, short: str) -> None:
    """Replace questionary's full-title answer line with a short echo.

    questionary always prints the picked Choice's full ``title`` after
    Enter, e.g.::

        ? Which AI provider do you want to use? local  —  Self-hosted (Ollama / LM Studio / vLLM). Chat + image if the model supports it.

    The dim description tail clutters the scrollback once the user is
    past the prompt and means nothing on its own. We move the cursor
    up one line, clear it, and reprint the question with just the
    short label::

        ? Which AI provider do you want to use? local

    Used by :func:`select` whenever the picked Choice has a non-empty
    description (i.e. a `_render_choice` line longer than the label).
    """
    sys.stdout.write("\x1b[1A\x1b[2K\r")
    sys.stdout.flush()
    console.print(f"[#5fafff bold]?[/] [bold]{message}[/] [#5fafff bold]{short}[/]")


def _clear_default_selection(question: Any) -> None:
    """Strip questionary's "default value is also marked" behaviour.

    questionary's `_is_selected()` (common.py:327) returns True when
    `default == choice.value`, which appends the default value to
    `InquirerControl.selected_options`. The renderer then paints that
    row with `class:selected` permanently — making the default row
    look bold even when the cursor has moved elsewhere. Cursor
    position lives in `pointed_at` and isn't touched, and Enter still
    returns `ic.get_pointed_at().value` — so wiping `selected_options`
    only affects rendering. Used in `select()`; checkboxes legitimately
    use `selected_options` to track Space-toggled rows, so we leave
    those alone.
    """
    try:
        from questionary.prompts.common import InquirerControl

        for container in question.application.layout.walk():
            content = getattr(container, "content", None)
            if isinstance(content, InquirerControl):
                content.selected_options = []
                break
    except Exception:
        # Defensive: if questionary's internal layout shape changes,
        # the worst that happens is the cosmetic regression above.
        pass


def _bind_escape_cancel(question: Any) -> None:
    """Make Esc behave like Ctrl-C: raise KeyboardInterrupt out of the prompt.

    questionary doesn't bind Esc to "exit" by default. The `select` /
    `checkbox` widgets register a fresh ``KeyBindings`` on which we can
    just `.add(Keys.Escape, ...)`. Text / password widgets are built
    via ``PromptSession`` whose ``application.key_bindings`` is a
    read-only ``_MergedKeyBindings`` proxy — `.add` doesn't exist on
    it. For those we build our own ``KeyBindings``, merge it on top
    of the existing one, and reassign back. The eager flag ensures we
    win over prompt_toolkit's default emacs-mode ``@handle("escape")``
    no-op (it would otherwise silently swallow the keystroke).
    """
    try:
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.keys import Keys

        def _on_escape(event):  # type: ignore[no-untyped-def]
            event.app.exit(exception=KeyboardInterrupt())

        kb = question.application.key_bindings
        if hasattr(kb, "add"):
            kb.add(Keys.Escape, eager=True)(_on_escape)
        else:
            extra = KeyBindings()
            extra.add(Keys.Escape, eager=True)(_on_escape)
            question.application.key_bindings = merge_key_bindings([kb, extra])
    except Exception:
        # Defensive: if questionary's internal layout ever changes
        # shape, the prompt still works — Esc just does nothing.
        pass


def _render_choice(c: Choice) -> str:
    """Compose the menu line: bold label, dim description trailer."""
    if c.description:
        # questionary renders the title with its own colors; we keep the
        # description dim-prefixed so it visually trails the label
        # without overpowering it.
        return f"{c.label}  —  {c.description}"
    return c.label


def _to_questionary(item: Choice | _Separator, *, checked: bool = False) -> Any:
    """Convert one of our items into questionary's equivalent."""
    if isinstance(item, _Separator):
        return questionary.Separator(item.label) if item.label else questionary.Separator()
    return questionary.Choice(title=_render_choice(item), value=item.value, checked=checked)


def _selectable_choices(choices: Sequence[Choice | _Separator]) -> list[Choice]:
    """Filter to actually-selectable rows for the numeric fallback."""
    return [c for c in choices if isinstance(c, Choice)]


def _fallback_select(
    message: str,
    choices: Sequence[Choice | _Separator],
    default_value: str | None,
) -> str | None:
    """Type-a-number prompt for non-TTY environments.

    Mirrors the previous wizard's manual menu so existing tests that
    feed `["1", "2", ...]` to `typer.prompt` keep working unchanged.
    """
    selectable = _selectable_choices(choices)
    console.print(f"[bold]{message}[/]")
    n = 0
    for c in choices:
        if isinstance(c, _Separator):
            if c.label:
                console.print(f"  [grey70]{c.label}[/]")
            else:
                console.print("")
            continue
        n += 1
        suffix = f"  [grey70]— {c.description}[/]" if c.description else ""
        console.print(f"  [cyan]{n}[/]. {c.label}{suffix}")
    default_idx = 1
    if default_value is not None:
        for i, c in enumerate(selectable, 1):
            if c.value == default_value:
                default_idx = i
                break
    while True:
        try:
            raw = typer.prompt(
                f"Select [1-{len(selectable)}]", default=str(default_idx), show_default=False
            ).strip()
        except (KeyboardInterrupt, EOFError, typer.Abort):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(selectable):
            return selectable[int(raw) - 1].value
        console.print(f"[yellow]Pick a number 1-{len(selectable)}.[/]")


def _fallback_checkbox(
    message: str,
    choices: Sequence[Choice | _Separator],
    defaults: Sequence[str],
) -> list[str] | None:
    """Comma-separated number prompt for non-TTY multi-select.

    Accepts ``"1,3,4"`` to pick rows 1/3/4, or empty input to keep
    the defaults. Same fallback contract as `_fallback_select`.
    """
    selectable = _selectable_choices(choices)
    default_set = set(defaults)
    console.print(f"[bold]{message}[/]")
    n = 0
    default_indices: list[str] = []
    for c in choices:
        if isinstance(c, _Separator):
            if c.label:
                console.print(f"  [grey70]{c.label}[/]")
            else:
                console.print("")
            continue
        n += 1
        marker = "[green]✓[/]" if c.value in default_set else " "
        suffix = f"  [grey70]— {c.description}[/]" if c.description else ""
        console.print(f"  {marker} [cyan]{n}[/]. {c.label}{suffix}")
        if c.value in default_set:
            default_indices.append(str(n))
    default_str = ",".join(default_indices)
    while True:
        try:
            raw = typer.prompt(
                f"Pick rows (comma-separated, 1-{len(selectable)})",
                default=default_str,
                show_default=bool(default_str),
            ).strip()
        except (KeyboardInterrupt, EOFError, typer.Abort):
            return None
        if not raw:
            return [c.value for c in selectable if c.value in default_set]
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        try:
            picked = [int(p) for p in parts]
        except ValueError:
            console.print(f"[yellow]Use comma-separated numbers 1-{len(selectable)}.[/]")
            continue
        if any(not (1 <= i <= len(selectable)) for i in picked):
            console.print(f"[yellow]Numbers must be in range 1-{len(selectable)}.[/]")
            continue
        return [selectable[i - 1].value for i in picked]
