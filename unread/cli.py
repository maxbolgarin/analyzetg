"""unread CLI (Typer). Commands are wired in later phases; stubs here
declare the final signatures so UX is stable from day one."""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from datetime import datetime
from pathlib import Path

import click
import typer
import typer.rich_utils as _typer_rich
from rich.console import Console
from typer.core import TyperGroup

from unread import __version__
from unread.config import get_settings
from unread.db.repo import apply_db_overrides_sync, open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import setup_logging

# Typer's defaults render option names as `bold cyan` in error / help output
# and the "Try '… --help' for help." suggestion line as `dim` wrapped in
# `[blue]…[/]`. Both can be unreadable on terminals whose palette maps cyan
# or blue to a very dark, near-background colour. Override at import time —
# Typer reads these constants when it builds the Rich console for each
# error / help render. `RICH_HELP` is a markup template with a hard-coded
# `[blue]` wrapper around the command path; rewrite it to a readable colour.
_typer_rich.STYLE_OPTION = "bold yellow"
_typer_rich.STYLE_NEGATIVE_OPTION = "bold magenta"
_typer_rich.STYLE_SWITCH = "bold green"
_typer_rich.STYLE_NEGATIVE_SWITCH = "bold red"
_typer_rich.STYLE_ERRORS_SUGGESTION = ""
_typer_rich.RICH_HELP = "Try [bold]'{command_path} {help_option}'[/] for help."


def _version_callback(value: bool) -> None:
    """`--version` / `-V` short-circuit: print version and exit cleanly.

    Marked `is_eager=True` on the option so Typer dispatches this before
    parsing the rest of the args — `unread --version` is safe to run
    even on a broken install where `~/.unread/` isn't readable.
    """
    if value:
        typer.echo(f"unread {__version__}")
        raise typer.Exit()


class _PreferSubcommandsGroup(TyperGroup):
    """Click group that prefers subcommand routing over the optional
    positional `ref` argument on the root callback.

    The root callback declares `ref: str | None = typer.Argument(None)`.
    Standard Click consumes the first non-option token into `ref` BEFORE
    checking for subcommand matches — so `unread settings` ends up
    invoking analyze with ref="settings" instead of dispatching to the
    settings subcommand. We peel a leading subcommand token out of args
    so the positional sees nothing, then inject the token back into
    `ctx.protected_args` so Group's normal routing fires.

    `unread -- settings` (or `unread tg settings`) explicitly forces
    the ref interpretation when a chat is literally titled like a
    subcommand.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        # Find the first non-option token. If it's a registered
        # subcommand name, peel it out before super() can consume it
        # as the optional positional.
        sub_idx = -1
        for i, tok in enumerate(args):
            if tok == "--":
                break  # `--` terminates options; everything after is positional
            if tok.startswith("-"):
                continue
            if tok in self.commands:
                sub_idx = i
            break  # first non-option non-terminator decides
        if sub_idx < 0:
            return super().parse_args(ctx, args)
        sub_name = args[sub_idx]
        # Tokens BEFORE the subcommand belong to the root callback
        # (options, possibly a positional ref). Tokens AFTER belong to
        # the subcommand. We parse only the `pre` slice here so the
        # root's optional positional doesn't accidentally consume the
        # subcommand's own arguments.
        pre = list(args[:sub_idx])
        post = list(args[sub_idx + 1 :])
        click.Command.parse_args(self, ctx, pre)
        # Click 8.3 made `Context.protected_args` a read-only property;
        # the canonical setter is the underscored attribute Click's own
        # `Group.parse_args` writes to.
        if self.chain:
            ctx._protected_args = [sub_name, *post]
            ctx.args = []
        else:
            ctx._protected_args = [sub_name]
            ctx.args = post
        return ctx.args


class _UnreadHelpMixin:
    """Mixin: replace Click's stock `format_help` with the new layout.

    Used by both the root-style group (which combines the prefer-
    subcommands parse logic + the new help) and the plain child-group
    class for sub-typers that don't have a positional `[REF]` of
    their own.
    """

    def format_help(self, ctx, formatter):  # type: ignore[override,no-untyped-def]
        # Root only renders the global overview; sub-groups (including
        # the `tg` namespace) render their own subcommand listing so
        # `unread tg --help` shows login / describe / sync / … rather
        # than re-printing the whole catalogue.
        if ctx.info_name == "unread" or ctx.parent is None:
            _print_help_overview()
        else:
            _print_help_for_group(self, ctx)


class _UnreadRootGroup(_UnreadHelpMixin, _PreferSubcommandsGroup):
    """Root + `tg` group: inherits the optional-positional handling
    from `_PreferSubcommandsGroup` AND the new help renderer."""


class _UnreadGroup(_UnreadHelpMixin, typer.core.TyperGroup):
    """Sub-typer group (`chats` / `cache` / `reports`): plain Click
    routing — these don't have a positional `[REF]` to peel — plus
    the new help renderer."""


class _UnreadCommand(typer.core.TyperCommand):
    """`TyperCommand` whose `--help` renders the new per-command
    layout (status one-liner → usage → description → arguments →
    options) — same shape as `unread help <cmd>`."""

    def format_help(self, ctx, formatter):  # type: ignore[override]
        _print_help_for_command(self, ctx)


class _UnreadTyper(typer.Typer):
    """Typer subclass that defaults every `@app.command(...)` to use
    `_UnreadCommand` so per-command `--help` flows through our custom
    formatter without each call site having to pass `cls=` explicitly.
    Setting `app.command_class` post-construction doesn't propagate in
    Typer 0.x — this is the only knob that does.
    """

    def command(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("cls", _UnreadCommand)
        return super().command(*args, **kwargs)


# Bootstrap DB-saved overrides into the live settings singleton BEFORE
# Typer constructs the app — Typer reads `help=` strings (and panel
# names) at app-construction time. Without this early sync, `--help`
# would render in the config-file language and ignore `unread settings`.
# A read-only sqlite open is safe (~1ms) and degrades to no-op when the
# DB doesn't exist yet (fresh install). Wrapped in `suppress(Exception)`
# because a corrupt `data.sqlite` would otherwise crash `unread --help`
# at import time, leaving the user with no way to even discover the
# `doctor` / `killme` recovery commands.
with contextlib.suppress(Exception):
    apply_db_overrides_sync(get_settings())

# Panel names — looked up once at import-time so each Typer-decorated
# command can pin its panel to the right localized header.
PANEL_MAIN = _t("cli_panel_main")
PANEL_TELEGRAM = _t("cli_panel_telegram")
PANEL_MAINT = _t("cli_panel_maint")

# `no_args_is_help` removed: the root callback handles the no-arg case
# (opens the analyze wizard). `--help` and the new `help` subcommand are
# the explicit help entry points.
app = _UnreadTyper(
    name="unread",
    help=_t("cli_app_help"),
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
    cls=_UnreadRootGroup,
    # Click groups default to `allow_interspersed_args=False`, which
    # would reject `unread @somegroup --dry-run` (an option after the
    # positional ref). With our `_PreferSubcommandsGroup` already
    # peeling subcommand tokens explicitly, it's safe to allow
    # interspersed options on the root callback.
    context_settings={"allow_interspersed_args": True},
)

chats_app = _UnreadTyper(help=_t("cmd_chats"), no_args_is_help=True, cls=_UnreadGroup)
cache_app = _UnreadTyper(help=_t("cmd_cache"), no_args_is_help=True, cls=_UnreadGroup)
# `cache` is a façade over three independently-shaped caches; the user
# surface is uniform per-entity (`<entity> [ls|purge|stats|show|export]`)
# even though the underlying tables (`analysis_cache`, `website_pages` /
# `youtube_videos` / `local_files`, and `messages`) have very different
# schemas. Bare `cache <entity>` opens the entity's `ls` view via each
# subgroup's `invoke_without_command=True` callback.
cache_ai_app = _UnreadTyper(
    help="LLM analysis cache (one row per cached LLM call).",
    no_args_is_help=True,
    cls=_UnreadGroup,
)
cache_sources_app = _UnreadTyper(
    help="Per-input source caches: extracted pages, YouTube transcripts, local-file text.",
    no_args_is_help=True,
    cls=_UnreadGroup,
)
cache_tg_app = _UnreadTyper(
    help="Telegram message cache: text, transcripts, and chat / topic metadata.",
    no_args_is_help=True,
    cls=_UnreadGroup,
)
cache_app.add_typer(cache_ai_app, name="ai")
cache_app.add_typer(cache_sources_app, name="sources")
cache_app.add_typer(cache_tg_app, name="tg")
backup_app = _UnreadTyper(help=_t("cmd_backup"), no_args_is_help=True, cls=_UnreadGroup)
# `tg` is the Telegram-source namespace. Bare `unread tg` (no
# subcommand) opens the interactive analyze picker — same behavior as
# the pre-namespace magic ref. `unread tg login`, `unread tg describe`,
# `unread tg sync`, … hang Telegram-only setup / inspection verbs off
# the same prefix so future sources (e.g. `unread wa describe`) can
# follow the same shape. The constant is kept for the few internal
# call sites (cmd_ask / cmd_dump) that still treat "tg" as a magic ref
# inside their own ref string.
TG_INTERACTIVE_REF = "tg"
tg_app = _UnreadTyper(
    help=_t("cmd_tg_group"),
    # _UnreadGroup gives us the per-command help formatter; no positional
    # arg on the callback so we don't need _PreferSubcommandsGroup here.
    cls=_UnreadGroup,
)

app.add_typer(tg_app, name="tg", rich_help_panel=PANEL_TELEGRAM)
app.add_typer(cache_app, name="cache", rich_help_panel=PANEL_MAINT)
app.add_typer(backup_app, name="backup", rich_help_panel=PANEL_MAINT)
# `chats` lives under `tg` (Telegram subscriptions). The previous
# top-level registration is gone — tests / scripts now use `tg chats`.
tg_app.add_typer(chats_app, name="chats")


@tg_app.callback(invoke_without_command=True)
def _tg_root(ctx: typer.Context) -> None:
    """Telegram source. Bare `unread tg` opens the analyze chat picker.

    `unread tg <subcommand>` (login / logout / describe / sync / …) runs
    the named verb. With no subcommand, dispatches straight to the
    interactive analyze wizard — same flow the previous magic-ref form
    (`unread tg`) took, just rooted at the real subgroup so help groups
    every Telegram verb under one prefix.

    Callers who want analyze with explicit flags should use
    `unread <ref> [flags]` instead — wiring the full root flag set onto
    this callback would duplicate ~30 typer options for a path the
    wizard immediately overwrites with its own picker answers.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (login / describe / sync / …) was matched; let it run.
        return
    # Bare `unread tg` → analyze wizard. Same gating as the analyze root path.
    if not _ensure_ready_for_analyze(None):
        return
    from unread.interactive import run_interactive_analyze

    _run(run_interactive_analyze())


console = Console()


def _run(coro) -> None:
    """Run an async command coroutine and convert known errors to friendly exits.

    Every Typer subcommand routes its `async def cmd_…` body through
    here, which makes this the right chokepoint for:
      - `TelegramSessionExpired` → friendly "re-run init" banner.
      - `KeyboardInterrupt` → friendly "Cancelled" line (partial state
        on disk is already safe — context managers and per-document
        enrichment persistence make Ctrl-C resume-friendly by design).
      - Any other Exception → one-line "Error: …" message instead of
        a multi-frame Rich traceback. The traceback is panic-inducing
        for non-technical users and rarely actionable; users opt back
        in with ``-v / --verbose`` (sets ``UNREAD_DEBUG=1`` upstream)
        when they want the full thing for a bug report.
    `typer.Exit` and `SystemExit` always re-raise unchanged so exit
    codes stay correct for shell scripts.
    """
    import os as _os

    from unread.tg.client import TelegramSessionExpired, exit_session_expired

    # Sync-context guard: if a future caller routes through `_run` from
    # inside an already-running event loop, `asyncio.run` raises a
    # confusing late-stage RuntimeError. Detect upfront so the message
    # names the actual misuse (sync-only entry point) instead of leaking
    # asyncio internals.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Close the unconsumed coroutine so we don't trigger a
        # "coroutine was never awaited" RuntimeWarning on top of the
        # error we're raising.
        with contextlib.suppress(Exception):
            coro.close()
        raise RuntimeError(
            "_run was called inside a running event loop; this CLI entry "
            "point must be invoked from sync context only."
        )

    try:
        asyncio.run(coro)
    except TelegramSessionExpired:
        exit_session_expired()
    except KeyboardInterrupt:
        # Note: asyncio.run() runs the coro's cleanup (cancels tasks,
        # waits for context managers to exit) before re-raising, so by
        # the time we land here the DB / sessions are flushed. The line
        # below is purely UX so the user sees a clean message instead
        # of `^CTraceback (most recent call last)…`. The "re-run to
        # resume" hint matters for the enrichment path: media transcripts,
        # link summaries, and YouTube/website extractions are all
        # cached on stable keys (doc_id, URL hash, video_id), so a
        # second run picks up where the first stopped without re-paying
        # for the work already done.
        console.print(
            f"\n[yellow]{_t('cli_cancelled_partial_saved')}[/]\n[grey70]{_t('cli_cancelled_resume_hint')}[/]"
        )
        raise typer.Exit(130) from None  # 128 + SIGINT
    except (typer.Exit, SystemExit):
        # The command already produced its own user-facing message and
        # picked an exit code. Pass through.
        raise
    except Exception as e:
        # In verbose mode, re-raise so the developer / power user sees
        # the full Rich traceback. Otherwise, render a one-liner that
        # points at -v and bug-report.
        if _os.environ.get("UNREAD_DEBUG") or _os.environ.get("UNREAD_VERBOSE"):
            raise
        # Strip the leading exception class qualname in the message —
        # users care about WHAT happened, not whether it was a
        # `ValueError` vs `RuntimeError`.
        msg = str(e).strip() or type(e).__name__
        console.print(f"\n[red]{_t('cli_error_prefix')}[/] {msg}")
        console.print(f"[grey70]{_t('cli_error_traceback_hint')}[/]")
        raise typer.Exit(1) from None


# Names of every Typer command/group on the root app. Used by the root
# callback to warn when `unread <bare-word>` collides with a subcommand
# name (Click resolves subcommands first; the user almost certainly
# wanted to look up a chat by that title via the interactive picker).
_RESERVED_TOP_LEVEL: set[str] = set()


def _maybe_warn_subcommand_collision(ref: str | None) -> None:
    """Surface a one-line hint when `ref` shadows a real subcommand.

    The user typed something like `unread settings` intending a chat
    titled "settings" — Click already routed to the subcommand instead.
    They land here only when `ref` slipped through the parser (which
    means they used a non-colliding form). The escape hatch for a chat
    that genuinely is named after a subcommand is the interactive
    picker (`unread tg`), which can find it by title.
    """
    if ref and ref in _RESERVED_TOP_LEVEL:
        console.print(
            f"[yellow]Note: `{ref}` is also a subcommand name. "
            f"For a chat literally titled '{ref}', use `unread tg` "
            f"and search for it in the picker.[/]"
        )


def _session_exists() -> bool:
    """True iff a Telegram session file is present (either name variant)."""
    settings = get_settings()
    p = Path(settings.telegram.session_path)
    return p.exists() or p.with_name(p.name + ".session").exists()


def _telegram_credentials_present() -> bool:
    """True iff Telegram api_id + api_hash are resolvable from settings or env."""
    import os as _os

    s = get_settings()
    if s.telegram.api_id and s.telegram.api_hash:
        return True
    return all(_os.environ.get(k) for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"))


def _openai_credentials_present() -> bool:
    """True iff the OpenAI API key is resolvable from settings or env.

    OpenAI is special-cased because it's the only provider that backs
    Whisper / embeddings / vision in addition to chat. Even when chat
    runs through Anthropic / Google / OpenRouter, those three features
    look here.
    """
    import os as _os

    s = get_settings()
    return bool(s.openai.api_key) or bool(_os.environ.get("OPENAI_API_KEY"))


def _active_provider_credentials_present() -> bool:
    """True iff the chat slot's provider has its key set.

    Routed by `settings.ai.chat_provider` (per-slot). Used to gate
    chat-only commands (`analyze`, `ask`) so a Telegram-only or
    wrong-provider install surfaces a focused banner instead of a
    confusing 401.
    """
    import os as _os

    from unread.ai.providers import _resolve_provider_name

    s = get_settings()
    name = _resolve_provider_name(s, "chat")
    if name == "openai":
        return bool(s.openai.api_key) or bool(_os.environ.get("OPENAI_API_KEY"))
    if name == "openrouter":
        return bool(s.openrouter.api_key)
    if name == "anthropic":
        return bool(s.anthropic.api_key)
    if name == "google":
        return bool(s.google.api_key)
    # Local mode — base_url + placeholder key are always present.
    return name == "local"


def _credentials_present() -> bool:
    """True iff BOTH Telegram and OpenAI credentials are resolvable.

    Used for the strict "everything ready" check; per-command gating
    typically wants the granular `_telegram_credentials_present` /
    `_openai_credentials_present` instead so a Telegram-only or
    OpenAI-only install can still run the commands it supports.
    """
    return _telegram_credentials_present() and _openai_credentials_present()


def _print_first_run_banner(missing: str = "both") -> None:
    """Print the friendly setup banner, scoped to which keys are missing.

    ``missing`` controls the copy:
      - ``"openai"`` — only the OpenAI key is missing (chat-provider
        scope, possibly because the user picked OpenAI as provider).
      - ``"ai"`` — generic "an AI key is missing"; mentions all four
        chat-provider options. Used when the user invoked a non-Telegram
        path (YouTube / website / file) without any chat provider key.
      - ``"telegram"`` — only Telegram credentials are missing.
      - ``"telegram_session_only"`` — the user has a Telegram session
        file but it's not authorized; points at ``unread tg login --force``.
      - ``"both"`` — neither side is set up. Default.

    The banner always points at ``unread init`` first (the interactive
    wizard handles missing keys without re-prompting for already-set
    ones), and mentions the ``~/.unread/.env`` non-interactive path as
    a secondary option.
    """
    from unread.core.paths import default_env_path, ensure_unread_home

    ensure_unread_home()
    env_path = default_env_path()
    # Link rows, in display order. Scoped below to whichever side is
    # actually missing so the banner doesn't dump irrelevant URLs on the
    # user (e.g. listing every AI provider when only Telegram is missing).
    tg_link = "  Telegram   https://my.telegram.org → API development tools"
    ai_links = (
        "  OpenAI     https://platform.openai.com/api-keys\n"
        "  Anthropic  https://console.anthropic.com/settings/keys\n"
        "  Google     https://aistudio.google.com/app/apikey\n"
        "  OpenRouter https://openrouter.ai/keys"
    )
    openai_link = "  OpenAI     https://platform.openai.com/api-keys"
    if missing == "openai":
        title = _t("cred_banner_title_openai")
        env_lines = "  OPENAI_API_KEY=sk-…"
        providers_note = _t("cred_banner_alternative_providers")
        links_block = openai_link
    elif missing == "ai":
        title = _t("cred_banner_title_ai")
        env_lines = "  # any of: OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENROUTER_API_KEY"
        providers_note = _t("cred_banner_providers_note")
        links_block = ai_links
    elif missing == "telegram":
        title = _t("cred_banner_title_telegram")
        env_lines = "  TELEGRAM_API_ID=…\n  TELEGRAM_API_HASH=…"
        providers_note = ""
        links_block = tg_link
    else:
        title = _t("cred_banner_title_full")
        env_lines = "  OPENAI_API_KEY=sk-…\n  TELEGRAM_API_ID=…\n  TELEGRAM_API_HASH=…"
        providers_note = ""
        links_block = f"{tg_link}\n{ai_links}"
    extra = f"\n\n{providers_note}" if providers_note else ""
    console.print(
        f"[bold yellow]{title}[/]\n"
        f"\n"
        f"{_t('cred_banner_run_init')}\n"
        f"\n"
        f"{_tf('cred_banner_env_intro', env_path=f'[bold]{env_path}[/]')}\n"
        f"{env_lines}"
        f"{extra}\n"
        f"\n"
        f"[bold]{_t('cred_banner_links_header')}[/]\n"
        f"{links_block}"
    )


def _exit_missing_openai_credentials() -> typer.Exit:
    """Print the OpenAI-missing banner and raise `typer.Exit(1)`.

    Used by analyze and ask paths so the user gets a consistent message
    instead of OpenAI's "401 Unauthorized" raw error mid-run.
    """
    _print_first_run_banner("openai")
    raise typer.Exit(1)


def _seed_home_templates() -> None:
    """Drop `.env` and `config.toml` templates into `~/.unread/` if absent.

    Lets the user fill in credentials in-place after a first-run banner
    instead of hunting for the example files in the repo.

    The `.env` write goes through `secret_write_text` so the file is
    mode 0o600 from creation — without that, a brief world-readable
    window exists between `copyfile` (which inherits umask, typically
    0o644) and the follow-up `chmod`. The window is small but real on
    multi-user hosts and the .env carries fresh API keys.
    """
    from unread.core.paths import default_config_path, default_env_path, ensure_unread_home

    ensure_unread_home()
    # Repo-relative example files. Best-effort: skipped if the install
    # is the wheel without the templates.
    repo_root = Path(__file__).resolve().parent.parent
    env_target = default_env_path()
    cfg_target = default_config_path()
    env_template = repo_root / ".env.example"
    cfg_template = repo_root / "config.toml.example"
    if not env_target.exists() and env_template.exists():
        from unread.util.fsmode import secret_write_text

        try:
            secret_write_text(env_target, env_template.read_text(encoding="utf-8"))
        except OSError as e:
            console.print(
                f"[yellow]Couldn't seed {env_target}: {e} — copy {env_template} "
                f"manually and `chmod 600 {env_target}`.[/]"
            )
    if not cfg_target.exists() and cfg_template.exists():
        # Pre-prod review: also seed config.toml.example via
        # secret_write_text. The example file isn't itself sensitive,
        # but the user's eventual edits will hold settings (model
        # picks, base URLs) and the file lives in ~/.unread next to
        # the credentials. 0o600 from creation closes the brief
        # world-readable window between copyfile + chmod.
        from unread.util.fsmode import secret_write_text

        try:
            secret_write_text(cfg_target, cfg_template.read_text(encoding="utf-8"))
        except OSError as e:
            console.print(
                f"[yellow]Couldn't seed {cfg_target}: {e} — copy {cfg_template} "
                f"manually and `chmod 600 {cfg_target}`.[/]"
            )


def _validate_lang_flags(
    language: str | None,
    report_language: str | None,
    source_language: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Validate and normalise the three language CLI flags.

    Returns the canonical lowercase ISO 639-1 codes, or the original
    value when ``None`` / empty (which means "use config default").
    Raises :class:`typer.BadParameter` on any unrecognised code.

    ``--language`` (UI) is held to a stricter pool — only languages
    that ship i18n + presets — because rendering UI strings in an
    untranslated language produces fallback English anyway.
    """
    from unread.util.languages import normalize_language_code

    def _check(name: str, value: str | None, *, ui: bool = False) -> str | None:
        if value is None or value == "":
            return value
        code = normalize_language_code(value)
        if code is None:
            raise typer.BadParameter(
                f"Invalid language code for --{name}: {value!r}. "
                f"Use an ISO 639-1 code (e.g. 'en', 'pt', 'zh') or its English name."
            )
        if ui:
            from unread.settings.commands import _supported_ui_languages

            supported = _supported_ui_languages()
            if code not in supported:
                raise typer.BadParameter(
                    f"--{name}={value!r} is not available as a UI language. "
                    f"UI is only translated into: {', '.join(supported)}. "
                    f"For LLM output language, use --report-language."
                )
        return code

    return (
        _check("language", language, ui=True),
        _check("report-language", report_language),
        _check("content-language", source_language),
    )


def _dispatch_analyze(**kwargs) -> None:
    """Shared bridge from the root + tg callbacks to `cmd_analyze`.

    Both the root callback (`unread <ref>`) and the `tg` callback
    (`unread tg <ref>`) collect the same option set and need to dispatch
    to the same analyze pipeline. This helper lives here so the only
    difference between the two callbacks is the auto-init policy.
    """
    from unread.analyzer.commands import cmd_analyze

    (
        kwargs["language"],
        kwargs["report_language"],
        kwargs["source_language"],
    ) = _validate_lang_flags(
        kwargs.get("language"),
        kwargs.get("report_language"),
        kwargs.get("source_language"),
    )
    save_flag = kwargs.pop("save", False)
    # Reject contradictory output flags up front so the user gets a
    # clear error rather than discovering downstream that one of the
    # three got silently dropped. `--save` is deprecated (`save_default`
    # below threads it through for back-compat) and inherently
    # conflicts with `--console`/`--no-save`.
    no_save = kwargs.get("no_save", False)
    console_out = kwargs.get("console_out", False)
    no_console = kwargs.get("no_console", False)
    if save_flag and (no_save or console_out):
        import typer as _typer

        raise _typer.BadParameter(
            "--save conflicts with --no-save / --console; pass at most one of these flags."
        )
    # `--no-console --no-save` (or its deprecated alias `--console --no-console`)
    # would suppress every form of output, leaving an LLM run with nothing to
    # show for the spend. Reject it instead of silently producing nothing.
    if no_console and (no_save or console_out):
        import typer as _typer

        raise _typer.BadParameter(
            "--no-console combined with --no-save would suppress all output; pick at most one."
        )
    # `--plain-citations` flips a console-only rendering knob. It does
    # not change the LLM input, the cache key, or the saved file — so we
    # apply it as a one-shot override to the settings singleton instead
    # of threading it through every analyze helper signature.
    plain_citations = kwargs.pop("plain_citations", False)
    if plain_citations:
        from unread.config import get_settings

        get_settings().analyze.plain_citations = True
    _run(cmd_analyze(save_default=save_flag, **kwargs))


_STDIN_REF_SENTINEL = "<stdin>"


def _looks_like_local_file(ref: str) -> bool:
    """True iff `ref` resolves to a local file on disk.

    Files are detected before Telegram so `unread ./report.pdf` and
    `unread /tmp/notes.md` route to the file analyzer instead of
    being interpreted as a chat title. We use a path-shape probe
    first (cheap, never touches the filesystem) and only stat when
    the shape is ambiguous — avoids surprising Telegram users with
    stat() calls for `@username` etc.
    """
    rl = ref.strip()
    if not rl:
        return False
    # Path-shape signals: explicit relative / absolute / home-relative,
    # or `file://` URI. These never collide with Telegram refs.
    if rl.startswith(("./", "../", "/", "~/", "~")) or rl.startswith("file://"):
        return True
    # `@user` is a Telegram ref; never a file path.
    if rl.startswith("@"):
        return False
    # `http(s)://` and `tg://` URLs route to website / YouTube / Telegram.
    if rl.startswith(("http://", "https://", "tg://")):
        return False
    # Bare names with a `/` (Windows uses `\` too) → very likely a path.
    # Bare extension-only names (`report.pdf`) need a stat to disambiguate
    # from chat titles. We do that probe last and only if the token
    # contains a recognized file extension to avoid stat-storming on
    # every fuzzy chat lookup.
    if "/" in rl or "\\" in rl:
        return True
    # Last-resort probe: bare filename with a known extension AND the
    # file exists in cwd. Anything else falls through to Telegram so
    # fuzzy-title lookups still work.
    from pathlib import Path as _Path

    p = _Path(rl).expanduser()
    if not p.suffix:
        return False
    # `is_file()` does a `stat()` syscall, which can hang indefinitely
    # on a stalled NFS / SMB mount in cwd or `~`. Run it in a worker
    # thread with a 200 ms cap so a wedged filesystem can't freeze
    # every `unread <something>` invocation. Common errors
    # (PermissionError, FileNotFoundError, "Stale file handle", "Host
    # is down") fall through as "not a file" so the ref still reaches
    # Telegram resolution.
    return _is_file_with_timeout(p, timeout_sec=0.2)


def _is_file_with_timeout(path: Path, timeout_sec: float = 0.2) -> bool:  # type: ignore[name-defined]
    """`Path.is_file()` with a hard wall-clock cap.

    A stalled network mount makes raw `stat()` block indefinitely with
    no exception — `OSError` only fires when the kernel finally gives
    up minutes later. We probe in a daemon thread and treat a timeout
    as "not a file": the ref then falls through to Telegram resolution
    instead of freezing the CLI.
    """
    import threading

    result: list[bool] = [False]

    def _probe() -> None:
        try:
            result[0] = path.is_file()
        except OSError:
            result[0] = False
        except Exception:
            result[0] = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        # Stat is still running; abandon it. Daemon thread will be
        # cleaned up when the process exits.
        return False
    return result[0]


def _resolve_local_file_path(ref: str) -> Path | None:  # type: ignore[name-defined]
    """Return the resolved absolute path for a file ref, or None on miss."""
    rl = ref.strip()
    if rl.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(rl)
        rl = unquote(parsed.path)
    p = Path(rl).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not p.is_file():
        return None
    return p


def _stdin_has_data() -> bool:
    """True when stdin is piped / redirected and actually contains bytes.

    `unread` (no args) on a TTY shows the quickstart panel. The same
    invocation with stdin piped (`cat foo.txt | unread`) routes the
    piped bytes through the file analyzer instead.

    Distinguishing "non-TTY but empty" (e.g. `unread < /dev/null`,
    or `runner.invoke(app, [])` in tests where Click hands us an
    empty BytesIO) matters because we don't want to send the user
    into the file-analyze path with nothing to analyze.

    Strategy:
      - `isatty()` False *and* the underlying buffer has at least one
        byte queued. We peek when the buffer supports it; for streams
        that don't (Click's BytesIO-backed test stdin), we fall back
        to checking if the underlying file has nonzero size.
    """
    try:
        if sys.stdin.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        # Some embedded environments raise on `isatty()`; treat as
        # "no stdin" so the quickstart panel still shows.
        return False
    # Peek when we can — works for real pipes wrapping a BufferedReader.
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None and hasattr(buf, "peek"):
        try:
            return bool(buf.peek(1))
        except (ValueError, OSError):
            pass
    # File redirect: fstat the descriptor and check size.
    try:
        import os
        import stat

        st = os.fstat(sys.stdin.fileno())
        if stat.S_ISREG(st.st_mode):
            return st.st_size > 0
    except (AttributeError, ValueError, OSError):
        pass
    # In-memory test stream (e.g. Click's CliRunner): inspect the
    # BytesIO directly. `getbuffer().nbytes` is the canonical "is
    # there content?" probe and works without consuming.
    if buf is not None and hasattr(buf, "getbuffer"):
        try:
            return bool(buf.getbuffer().nbytes)
        except (ValueError, OSError):
            pass
    # Unknown stream type — assume content. Worst case, the file
    # analyzer will raise a clean "no input on stdin" error.
    return True


def _looks_like_telegram_ref(ref: str | None) -> bool:
    """True when this analyze run will need a live Telegram session.

    Wizard mode (`ref is None`) and Telegram-shaped refs need it.
    YouTube, websites, local files, and stdin do not.
    """
    if ref is None:
        return True
    rl = ref.strip().lower()
    if rl in (_STDIN_REF_SENTINEL, "-"):
        return False
    if _looks_like_local_file(rl):
        return False
    youtube_prefixes = (
        "https://www.youtube.com/",
        "https://youtu.be/",
        "https://m.youtube.com/",
        "https://music.youtube.com/",
        "http://www.youtube.com/",
        "http://youtu.be/",
    )
    if rl.startswith(youtube_prefixes):
        return False
    if rl.startswith(("https://t.me/", "http://t.me/", "tg://")):
        return True
    # Any other http(s) URL → website analyzer; everything else (@user,
    # fuzzy title, numeric id, …) routes to Telegram.
    return not rl.startswith(("http://", "https://"))


# Numeric Telegram chat ids, with optional `-100` channel prefix.
_TELEGRAM_NUMERIC_RE = re.compile(r"^-?\d+$")


def _is_explicit_telegram_ref(ref: str) -> bool:
    """True for ``ref`` shapes that are unambiguously Telegram.

    Used by the bare ``unread <ref>`` callback to decide whether the
    user clearly meant "look this up on Telegram" — vs. a free-form
    quoted string that might be a typo, a shell mishap, or a confused
    new user. Bare quoted strings like ``unread "some text"`` no
    longer fall through to fuzzy chat-title matching at the root —
    that mode now lives only behind ``unread tg <ref>`` / the
    Telegram-explicit subcommands. Helps remove a long-standing
    "why does my random text try to log into Telegram?" surprise.

    True for:
      * ``@username``
      * ``t.me/...`` / ``https://t.me/...`` / ``tg://...``
      * numeric id (``-1001234567890`` or bare ``123``)
      * the literal ``me`` (Saved Messages alias)
    """
    rl = ref.strip().lower()
    if not rl:
        return False
    if rl == "me":
        return True
    if rl.startswith("@"):
        return True
    if rl.startswith(("https://t.me/", "http://t.me/", "tg://")):
        return True
    return bool(_TELEGRAM_NUMERIC_RE.match(rl))


def _is_uninitialized() -> bool:
    """True iff the install can't run chat / analyze commands.

    The decisive signal is whether the active chat provider has a
    resolvable API key — without it every analyze / ask call 401s and
    the bare-`unread` quickstart can't help.

    The wizard pointer file (``~/.unread/install.toml``) used to be
    treated as a separate "setup happened" requirement. That produced
    a false-positive setup prompt for users who configured `.env` /
    secrets backend / env vars by hand (or via an older release that
    pre-dates the pointer file) — they had a fully working install
    but bare `unread` kept offering to re-run the wizard. The pointer
    is now informational only: present iff the wizard was ever run,
    absent does NOT mean "needs setup".

    We intentionally don't check Telegram credentials — a YouTube /
    website / file-only user with the AI provider configured is
    fully functional.
    """
    return not _active_provider_credentials_present()


def _maybe_offer_init() -> None:
    """Friendly prompt: 'unread isn't set up yet — run setup now?'.

    Caller (the bare-`unread` path) gates this to truly-first-run:
    install pointer missing, active AI provider has no key, stdin is
    a TTY. On Yes, runs the full `cmd_init` wizard. On No, the caller
    falls through to the quickstart panel.
    """
    from unread.tg.commands import cmd_init
    from unread.util.prompt import confirm as _confirm

    console.print("[bold yellow]Looks like unread isn't set up yet.[/]")
    console.print(
        "[grey70]Setup picks an install folder, an AI provider, and (optionally) "
        "links Telegram. Takes about a minute.[/]\n"
    )
    if not _confirm("Run setup now?", default=True):
        console.print("[grey70]No worries — run `unread init` whenever you're ready.[/]\n")
        return
    _seed_home_templates()
    _run(cmd_init(scope="full"))


def _print_config_status() -> None:
    """Show what's configured: install dir, AI providers, Telegram session.

    Cheap (no network): provider keys come straight from the resolved
    settings, Telegram presence is checked by inspecting the local
    session SQLite (or encrypted slot) for an actual ``auth_key`` —
    file existence alone isn't enough since Telethon writes the file
    on first connect, before login completes. The Telegram username
    isn't shown — it would require ``client.get_me()`` which is what
    ``unread doctor`` is for.
    """
    from unread.ai.providers import _resolve_provider_name
    from unread.core.paths import unread_home

    s = get_settings()
    home = unread_home()
    active = _resolve_provider_name(s, "chat")

    # Per-provider key state. Mirrors `_active_provider_credentials_present`
    # but for every provider, not just the active one — this is the panel
    # that answers "what do I already have?".
    import os as _os

    provider_keys: list[tuple[str, bool]] = [
        ("openai", bool(s.openai.api_key) or bool(_os.environ.get("OPENAI_API_KEY"))),
        ("openrouter", bool(s.openrouter.api_key)),
        ("anthropic", bool(s.anthropic.api_key)),
        ("google", bool(s.google.api_key)),
        ("local", active == "local"),  # local needs only base_url, no key
    ]

    def _mark(ok: bool) -> str:
        return "[green]✓[/]" if ok else "[red]✗[/]"

    # Use `grey70` (a fixed mid-grey hex shade in rich) instead of
    # rich's `dim` attribute. ANSI `dim` is renderer-specific — on
    # some terminal themes it picks up a purple/blue cast that's
    # nearly invisible. `grey70` resolves to a deterministic colour
    # so the panel reads the same on every theme.
    rows: list[str] = []
    rows.append(f"  [bold]Install:[/] [grey70]{home}[/]")

    # Languages: UI / report / source. Report falls back to UI when unset
    # (matches `_resolve_report_lang` runtime semantics); source is shown
    # only when the user explicitly opted in (empty = LLM auto-detect).
    ui_lang = (s.locale.language or "en").lower()
    report_lang = (s.locale.report_language or "").strip().lower() or ui_lang
    source_lang = (s.locale.content_language or "").strip().lower()
    lang_parts = [f"UI [cyan]{ui_lang}[/]", f"report [cyan]{report_lang}[/]"]
    if source_lang:
        lang_parts.append(f"source [cyan]{source_lang}[/]")
    rows.append(f"  [bold]Languages:[/] {' · '.join(lang_parts)}")

    active_ok = next((ok for name, ok in provider_keys if name == active), False)
    rows.append(
        f"  [bold]AI provider:[/] {active} {_mark(active_ok)}"
        + ("" if active_ok else "  [grey70](no key — `analyze` / `ask` will fail)[/]")
    )
    other_keys = [name for name, ok in provider_keys if ok and name != active]
    if other_keys:
        rows.append(f"  [bold]Other AI keys:[/] [green]{', '.join(other_keys)}[/]")

    # File presence alone isn't authorization — Telethon writes the
    # SQLite session file on first `client.connect()` (DC info, server
    # addresses, port — well before the user completes login). The
    # authoritative signal is `is_user_authorized()`; `is_session_authorized_sync`
    # is its no-network, no-decrypt twin so this status panel can trust
    # the answer without reaching for telethon.
    from unread.tg.session_state import is_session_authorized_sync

    creds_present = bool(s.telegram.api_id and s.telegram.api_hash)
    authorized = creds_present and is_session_authorized_sync(s)
    if authorized:
        tg_line = f"  [bold]Telegram:[/] {_mark(True)} session linked"
    elif creds_present:
        tg_line = "  [bold]Telegram:[/] [yellow]creds set, not logged in[/]  [grey70](run `unread login`)[/]"
    else:
        tg_line = f"  [bold]Telegram:[/] {_mark(False)} not configured"
    rows.append(tg_line)

    # Active credential-storage backend. `read_active_backend_sync` is
    # a cheap read-only SQLite query and falls through to "db" on any
    # error, so it's safe to call from the no-args overview.
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        BACKEND_PASSPHRASE,
        read_active_backend_sync,
    )

    backend = read_active_backend_sync(s.storage.data_path)
    if backend == BACKEND_KEYCHAIN:
        sec_line = f"  [bold]Security:[/] keystore {_mark(True)}  [grey70](OS keychain)[/]"
    elif backend == BACKEND_PASSPHRASE:
        sec_line = f"  [bold]Security:[/] pass {_mark(True)}  [grey70](passphrase-encrypted)[/]"
    else:
        sec_line = (
            "  [bold]Security:[/] [yellow]plain[/]  "
            "[grey70](run `unread security set keystore` to encrypt at rest)[/]"
        )
    rows.append(sec_line)

    console.print("[bold]Status[/]")
    for row in rows:
        console.print(row)
    console.print(
        "  [grey70]Add or change AI keys / providers:[/] [cyan]unread init[/]  "
        "[grey70]·[/]  [grey70]Re-link Telegram:[/] [cyan]unread tg login --force[/]"
    )


# Single source of truth for the `<ref>` cheat-sheet shown in the help
# overview AND in `unread help flags`. Each row: (form, description).
# `tg` is the magic ref token (see `TG_INTERACTIVE_REF`); listing it
# first surfaces the interactive picker as the friendliest entry-point
# for users who don't yet have a chat handle / link in hand.
# Fuzzy chat-title match is intentionally NOT in this table — the bare
# `unread <ref>` form rejects free-form strings to avoid the
# "why does my random text try to log into Telegram?" surprise. The
# interactive picker (`unread tg`) is the supported route for browsing
# chats by name.
_REF_TYPES: tuple[tuple[str, str], ...] = (
    ("tg", "interactive Telegram chat picker (also: `ask tg`, `dump tg`)"),
    ("@username", "Telegram handle"),
    ("t.me/c/<id>/<msg>", "Telegram link (channel post, topic, message)"),
    ("-1001234567890", "numeric Telegram chat id (use `--` to separate from flags)"),
    ("https://youtu.be/...", "YouTube video URL"),
    ("https://example.com/...", "web page URL"),
    ("./report.pdf", "local file (txt / md / pdf / docx / audio / video / image)"),
    ("-", "stdin (also auto-detects piped input)"),
)


def _status_one_liner() -> str:
    """Compact `local ✓ · Telegram ✓` line shown above per-command help.

    Cheap: same checks as `_print_config_status` but rendered as one
    inline line so help screens for individual commands aren't dominated
    by a multi-row status block. The full status is reserved for the
    no-args overview.
    """
    import os as _os

    from unread.ai.providers import _resolve_provider_name
    from unread.tg.session_state import is_session_authorized_sync

    s = get_settings()
    active = _resolve_provider_name(s, "chat")
    has_key = {
        "openai": bool(s.openai.api_key) or bool(_os.environ.get("OPENAI_API_KEY")),
        "openrouter": bool(s.openrouter.api_key),
        "anthropic": bool(s.anthropic.api_key),
        "google": bool(s.google.api_key),
        "local": True,  # local provider doesn't need a key
    }.get(active, False)
    creds_present = bool(s.telegram.api_id and s.telegram.api_hash)
    tg_ok = creds_present and is_session_authorized_sync(s)
    ai_mark = "[green]✓[/]" if has_key else "[red]✗[/]"
    tg_mark = "[green]✓[/]" if tg_ok else "[red]✗[/]"
    return f"[grey70]unread · {active} {ai_mark} · Telegram {tg_mark}[/]"


def _enumerate_commands(typer_app) -> list[tuple[str, str, str, bool]]:  # type: ignore[no-untyped-def]
    """Walk a Typer app and return (name, panel, help, hidden) per entry.

    Includes both leaf commands (`registered_commands`) and sub-typers
    (`registered_groups`). The panel is read from `rich_help_panel`;
    sub-typers nested via `add_typer(panel=...)` use the panel passed
    to `add_typer`.
    """
    rows: list[tuple[str, str, str, bool]] = []
    for ci in typer_app.registered_commands:
        name = ci.name or (ci.callback.__name__ if ci.callback else "")
        # Typer maps Python `_` → CLI `-` for command names derived from
        # function names; explicit `name=` is taken verbatim.
        cli_name = name.replace("_", "-") if not ci.name else ci.name
        help_str = ci.help or (ci.callback.__doc__ or "").strip().split("\n")[0]
        panel = ci.rich_help_panel or ""
        rows.append((cli_name, panel, help_str, bool(ci.hidden)))
    for gi in typer_app.registered_groups:
        if not gi.typer_instance:
            continue
        name = gi.name or ""
        help_str = gi.help or (gi.typer_instance.info.help or "")
        panel = gi.rich_help_panel or ""
        rows.append((name, panel, help_str, bool(gi.hidden)))
    return rows


def _enumerate_tg_subcommands() -> list[tuple[str, str]]:
    """Return (name, one-line help) pairs for visible `tg` subcommands.

    Hidden subcommands (legacy aliases like `dialogs`, `topics`,
    `channel-info`, `download-media`, `backfill`) and nested groups
    (e.g. `tg chats` — listed once under its own row) follow the same
    rule: skip hidden, surface visible commands and groups as-is.
    """
    rows: list[tuple[str, str]] = []
    for ci in tg_app.registered_commands:
        if ci.hidden:
            continue
        cli_name = ci.name or (ci.callback.__name__ if ci.callback else "")
        if not ci.name:
            cli_name = cli_name.replace("_", "-")
        # Strip any trailing "_cmd" added to function names so we don't
        # need a manual `name=` on every command.
        if cli_name.endswith("-cmd"):
            cli_name = cli_name[: -len("-cmd")]
        help_str = ci.help or (ci.callback.__doc__ or "").strip().split("\n")[0]
        rows.append((cli_name, help_str))
    for gi in tg_app.registered_groups:
        if gi.hidden or not gi.typer_instance:
            continue
        name = gi.name or ""
        help_str = gi.help or (gi.typer_instance.info.help or "")
        rows.append((name, help_str))
    rows.sort(key=lambda r: r[0])
    return rows


def _format_command_table(rows: list[tuple[str, str]], indent: str = "    ") -> str:
    """Two-column table of (name, description) with aligned descriptions."""
    if not rows:
        return ""
    width = max(len(name) for name, _ in rows)
    width = max(width, 8)
    lines: list[str] = []
    for name, desc in rows:
        lines.append(f"{indent}[cyan]{name:<{width}}[/]  {desc}")
    return "\n".join(lines)


_COMMON_PATTERNS: tuple[tuple[str, str], ...] = (
    ("unread <ref>", "analyze (default action)"),
    ('unread ask <ref> "question"', "ask a question about the ref"),
    ("unread dump <ref>", "export the ref's messages to disk"),
    ("unread tg", "open the interactive Telegram picker"),
)


def _print_usage_and_refs() -> None:
    """Usage line + `<ref> can be` cheat-sheet + common `<command> <ref>`
    patterns. Shared by bare `unread` and `unread help` so both
    surfaces give the user a consistent description of what the binary
    does, what `<ref>` accepts, and how `<ref>` composes with
    subcommands."""
    console.print(
        "\n[bold]Usage[/]\n"
        "  [cyan]unread <ref> [OPTIONS][/]              analyze a chat / file / URL / stdin\n"
        "  [cyan]unread <command> [OPTIONS] [ARGS][/]   run a specific command\n"
    )
    console.print("[bold]<ref> can be[/]")
    width = max(len(form) for form, _ in _REF_TYPES)
    for form, desc in _REF_TYPES:
        console.print(f"  [cyan]{form:<{width}}[/]  [grey70]{desc}[/]")

    console.print("\n[bold]Common patterns[/]")
    pw = max(len(left) for left, _ in _COMMON_PATTERNS)
    for left, desc in _COMMON_PATTERNS:
        console.print(f"  [cyan]{left:<{pw}}[/]  [grey70]{desc}[/]")


def _print_help_overview() -> None:
    """Full help screen shown by `unread help` and `unread --help`.

    Order: usage → ref types → patterns → commands → footer. Header
    + status panel are intentionally absent — they belong on the bare
    `unread` orientation snapshot, not on the catalogue page. No
    analyze flag dump either — that lives behind `unread help flags`
    and `unread <cmd> --help` for individual commands.
    """
    _print_usage_and_refs()
    console.print("")

    # Commands grouped by panel. Order: Main → Sync → Maintenance,
    # matching the existing rich_help_panel layout. We pull metadata
    # straight from the Typer app so adding a new command shows up here
    # automatically, no two-place edit.
    rows = _enumerate_commands(app)
    # `init` is a top-of-mind command for new users (Main), but once setup is
    # done it's just a re-link tool — demote it to Maintenance so the Main
    # group stays focused on the analyze/ask/dump trio.
    init_panel = PANEL_MAIN if _is_uninitialized() else PANEL_MAINT
    by_panel: dict[str, list[tuple[str, str]]] = {}
    for name, panel, help_str, hidden in rows:
        if hidden or not name:
            continue
        # The `tg` subgroup gets flattened in the overview so individual
        # verbs (`tg login`, `tg describe`, …) appear directly under the
        # Telegram panel. The bare `tg` row would just duplicate the
        # panel header; users who want it call `unread tg --help`.
        if name == "tg":
            for sub_name, sub_help in _enumerate_tg_subcommands():
                by_panel.setdefault(PANEL_TELEGRAM, []).append((f"tg {sub_name}", sub_help))
            continue
        effective_panel = init_panel if name == "init" else (panel or PANEL_MAIN)
        by_panel.setdefault(effective_panel, []).append((name, help_str))

    console.print("[bold]Commands[/]")
    for panel in (PANEL_MAIN, PANEL_TELEGRAM, PANEL_MAINT):
        items = by_panel.get(panel, [])
        if not items:
            continue
        console.print(f"  [bold]{panel}[/]")
        # Pin `init` to the top of Main when surfacing it to a fresh user —
        # they need that command before any other one is even useful.
        items.sort(key=lambda r: (0 if (panel == PANEL_MAIN and r[0] == "init") else 1, r[0]))
        console.print(_format_command_table(items, indent="    "))
        console.print("")

    console.print(
        "[grey70]Per-command help:[/] [cyan]unread <cmd> --help[/]  "
        "[grey70]·[/]  [cyan]unread help <cmd>[/]  "
        "[grey70]·[/]  [cyan]unread help flags[/] [grey70](flags accepted by `unread <ref>`)[/]"
    )


def _print_quickstart() -> None:
    """Bare `unread` (no args, TTY) — header + status + usage + ref
    types + a one-liner pointing at the full help.

    Intentionally omits the command catalogue: zero-arg `unread` is
    a "what's wired up + how do I invoke it" snapshot, not the full
    command listing. Users who want the catalogue run `unread help`.
    """
    console.print("[bold]unread[/] — Telegram / YouTube / web-page analyzer\n")
    _print_config_status()
    _print_usage_and_refs()
    console.print("\n[grey70]Run[/] [cyan]unread help[/] [grey70]for the full command list.[/]")


def _command_path(ctx: click.Context) -> str:
    """Build a clean `unread <sub> ...` path from the Click context chain.

    `ctx.command_path` would also splice in the root callback's
    `[REF]` positional and uses whatever invocation the user typed
    (`python -m unread.cli` when run as a module). For help output we
    always want the canonical `unread <sub>` form, so we walk the
    parent chain manually and force the root to read as `unread`.
    """
    parts: list[str] = []
    cur: click.Context | None = ctx
    while cur is not None:
        parts.append(str(cur.info_name or ""))
        cur = cur.parent
    parts.reverse()
    if parts:
        parts[0] = "unread"
    return " ".join(p for p in parts if p)


def _help_summary(cmd: click.Command) -> str:
    """First non-empty line of the command's help / docstring."""
    raw = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if stripped:
            return stripped
    return ""


def _help_long(cmd: click.Command) -> str:
    """Body of the command's docstring beyond the summary line, or ''."""
    raw = (cmd.callback.__doc__ if cmd.callback else "") or cmd.help or ""
    lines = [line.rstrip() for line in raw.splitlines()]
    # Strip leading blanks then drop the first non-blank line (the summary).
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        i += 1  # skip summary
    while i < len(lines) and not lines[i].strip():
        i += 1
    body = "\n".join(lines[i:]).rstrip()
    # De-indent — Python docstrings carry the function indent.
    if body:
        import textwrap

        body = textwrap.dedent(body)
    return body


def _safe_metavar(p: click.Parameter, ctx: click.Context | None) -> str:
    """Cross-Click-version metavar lookup.

    Click 8.3 made `make_metavar(ctx)` mandatory; older versions had
    `make_metavar()` no-arg. Fall back to a manual upper-cased name
    when the call signature doesn't match the runtime Click.
    """
    try:
        return p.make_metavar(ctx) if ctx is not None else p.make_metavar()  # type: ignore[arg-type]
    except TypeError:
        try:
            return p.make_metavar()  # type: ignore[call-arg]
        except TypeError:
            return str(p.name or "").upper()


def _format_param_table(
    params: list[click.Parameter], ctx: click.Context | None = None
) -> list[tuple[str, str]]:
    """Render Click parameters as `(left, right)` rows for two-column
    table output. `left` is the option spelling (with type hint and
    short alias), `right` is the help text."""
    rows: list[tuple[str, str]] = []
    for p in params:
        if getattr(p, "hidden", False):
            continue
        if isinstance(p, click.Option):
            opts = list(p.opts) + list(p.secondary_opts)
            spelling = ", ".join(opts)
            type_hint = ""
            if p.type and p.type.name not in ("bool", "BOOL"):
                tn = p.type.name.upper()
                if tn != "TEXT" or not p.is_flag:
                    type_hint = f" {tn}"
            left = f"{spelling}{type_hint}"
        else:  # Argument
            left = _safe_metavar(p, ctx).strip("[]")
        help_text = (getattr(p, "help", "") or "").strip()
        if isinstance(p, click.Option) and p.default is not None and not p.is_flag:
            default_val = p.default() if callable(p.default) else p.default
            if default_val not in ("", None, ()):
                help_text = (
                    f"{help_text}  [grey70][default: {default_val}][/]"
                    if help_text
                    else f"[grey70][default: {default_val}][/]"
                )
        rows.append((left, help_text))
    return rows


def _print_param_table(rows: list[tuple[str, str]]) -> None:
    """Render `(left, right)` parameter rows as a Rich table.

    Rich's `Table` keeps wrapped right-column lines aligned (instead
    of bleeding back to column 0 the way our hand-rolled wrapper
    did), which was the main readability complaint with long option
    descriptions like `--rerank` and `--semantic`.
    """
    if not rows:
        return
    from rich import box
    from rich.table import Table

    # `HORIZONTALS` + `show_lines=True` draws a horizontal rule
    # between each row but leaves the column boundary clean — each
    # option reads as its own cell without the visual clutter of
    # vertical pipes (`SQUARE`/`ROUNDED`) or just-blank-lines
    # (`SIMPLE`). Long wrapped descriptions stay aligned in their
    # own column.
    table = Table(
        show_header=False,
        box=box.HORIZONTALS,
        padding=(0, 2, 0, 0),
        pad_edge=False,
        expand=False,
        show_lines=True,
        border_style="grey50",
    )
    table.add_column("name", style="cyan", no_wrap=True, vertical="top")
    table.add_column("description", overflow="fold", vertical="top")
    for left, right in rows:
        # Collapse internal whitespace (docstrings often have line
        # breaks in the middle of sentences).
        cleaned = " ".join((right or "").split())
        table.add_row(left, cleaned)
    console.print(table)


def _print_help_for_command(cmd: click.Command, ctx: click.Context, *, label: str | None = None) -> None:
    """Render help for a single command in the new layout.

    Shared by `unread <cmd> --help` (via `_UnreadCommand.format_help`)
    and `unread help <cmd>` (via `help_cmd`). `label` overrides the
    displayed command name — used for `unread help flags` to render
    the root callback's params under a `<ref>` label rather than the
    literal "unread" group name (and to avoid suggesting an `analyze`
    subcommand exists, since it doesn't).
    """
    # `_command_path(ctx)` produces a clean "unread <sub>" string,
    # bypassing Click's `command_path` (which splices the root
    # callback's `[REF]` positional and uses the literal argv[0]).
    usage_path = f"unread {label}" if label else _command_path(ctx)
    console.print(_status_one_liner())
    console.print("")
    # Usage
    args = [p for p in cmd.params if isinstance(p, click.Argument)]
    # When `label` already names the positional (e.g. `<ref>` for the
    # root callback's flag-listing page), don't append `[REF]` after
    # the options — they refer to the same positional and the
    # repetition reads like a typo.
    suppress_args = label == "<ref>"
    arg_parts = "" if suppress_args else " ".join(_safe_metavar(p, ctx) for p in args)
    options_part = (
        " [OPTIONS]" if any(isinstance(p, click.Option) and not p.hidden for p in cmd.params) else ""
    )
    console.print(
        f"[bold]Usage[/]\n  [cyan]{usage_path}{options_part}{(' ' + arg_parts) if arg_parts else ''}[/]\n"
    )
    # Description
    summary = _help_summary(cmd)
    if summary:
        console.print(f"[bold]Description[/]\n  {summary}\n")
    long_body = _help_long(cmd)
    if long_body:
        console.print(f"[grey70]{long_body}[/]\n")
    # `<ref>` cheat-sheet — only shown for the analyze (root callback) help.
    if label == "<ref>":
        console.print("[bold]<ref> can be[/]")
        ref_w = max(len(form) for form, _ in _REF_TYPES)
        for form, desc in _REF_TYPES:
            console.print(f"  [cyan]{form:<{ref_w}}[/]  [grey70]{desc}[/]")
        console.print("")
    # Arguments
    if args:
        console.print("[bold]Arguments[/]")
        _print_param_table(_format_param_table(args, ctx))
        console.print("")
    # Options
    opts = [p for p in cmd.params if isinstance(p, click.Option) and not p.hidden]
    if opts:
        console.print("[bold]Options[/]")
        _print_param_table(_format_param_table(opts, ctx))
        console.print("")
    # Footer
    console.print("[grey70]All commands:[/] [cyan]unread help[/]")


def _print_help_for_group(grp: click.Group, ctx: click.Context) -> None:
    """Render help for a Typer sub-group (chats / cache / reports / tg).

    Shows the one-line status, usage, description, the group's own
    options (rare), and the list of subcommands. Behaves like
    `_print_help_overview` but scoped to one sub-group's tree.
    """
    name = _command_path(ctx)
    console.print(_status_one_liner())
    console.print("")
    console.print(f"[bold]Usage[/]\n  [cyan]{name} <subcommand> [OPTIONS] [ARGS][/]\n")
    summary = _help_summary(grp)
    if summary:
        console.print(f"[bold]Description[/]\n  {summary}\n")

    # Subcommands of this group (no panels — the nested groups are
    # small enough to list flat). For each child that's itself a
    # `click.Group` (e.g. `cache ai`, `cache sources`, `cache tg`),
    # also surface its own leaf names on a follow-up indented line.
    # Without this, `unread cache` showed three entity descriptions and
    # the user had to drill into each `cache <entity> --help` to discover
    # `ls / purge / stats / show / export` — defeats the point of a
    # one-shot overview.
    sub_rows: list[tuple[str, str, list[str]]] = []
    for sub_name in grp.list_commands(ctx):
        sub = grp.get_command(ctx, sub_name)
        if sub is None or getattr(sub, "hidden", False):
            continue
        leaves: list[str] = []
        if isinstance(sub, click.Group):
            sub_ctx = click.Context(sub, info_name=sub_name, parent=ctx)
            for leaf_name in sub.list_commands(sub_ctx):
                leaf = sub.get_command(sub_ctx, leaf_name)
                if leaf is None or getattr(leaf, "hidden", False):
                    continue
                leaves.append(leaf_name)
            leaves.sort()
        sub_rows.append((sub_name, _help_summary(sub), leaves))
    if sub_rows:
        console.print("[bold]Subcommands[/]")
        sub_rows.sort(key=lambda r: r[0])
        width = max(len(sname) for sname, _, _ in sub_rows)
        for sname, sdesc, leaves in sub_rows:
            console.print(f"  [cyan]{sname:<{width}}[/]  [grey70]{sdesc}[/]")
            if leaves:
                # Two-space indent past the subcommand-name column so the
                # leaf row visually nests under its parent.
                indent = " " * (2 + width + 2)
                console.print(f"{indent}[grey50]↳[/] [cyan]{' / '.join(leaves)}[/]")
        console.print("")

    # Group-level options (usually empty for sub-typers).
    opts = [p for p in grp.params if isinstance(p, click.Option) and not p.hidden]
    if opts:
        console.print("[bold]Options[/]")
        _print_param_table(_format_param_table(opts, ctx))
        console.print("")

    console.print(
        f"[grey70]Per-subcommand help:[/] [cyan]{name} <sub> --help[/]  [grey70]·[/]  [cyan]unread help[/]"
    )


# Note: `_UnreadGroup` and `_UnreadCommand` are defined right after
# `_PreferSubcommandsGroup` at the top of this module so the
# `typer.Typer(cls=...)` declarations can refer to them. Their
# `format_help` bodies call helpers defined here — that's fine because
# the lookup happens at format-help time, not at class-definition time.


def _ensure_ready_for_analyze(ref: str | None) -> bool:
    """Bootstrap `~/.unread/` and verify the active provider key.

    Called for both `unread <ref>` and `unread tg <ref>`. Analyze always
    needs the *active chat provider's* key (OpenAI / OpenRouter /
    Anthropic / Google / Local-server-credential) — gate on that and
    surface a focused banner pointing at `unread init` when missing.

    Telegram-side gating (missing api_id / api_hash, missing or expired
    session) is delegated to ``tg_client``'s built-in retry loop in
    ``unread.tg.client`` — it offers ``cmd_init(scope="telegram_only")``
    on a single prompt and exits cleanly on decline. We deliberately do
    NOT trigger an eager full-scope ``cmd_init()`` here: it would re-ask
    the AI provider question even when the only thing missing is
    Telegram, and a "no" answer to its TG step would leave the user
    facing the same prompt again from ``tg_client``.

    Returns True if the caller should proceed with analyze, False if
    the caller should stop (a banner has already been printed).
    """
    _seed_home_templates()
    if not _active_provider_credentials_present():
        # Raises typer.Exit(1) — analyze is dead in the water without
        # the active provider's key, so we surface the friendly banner
        # + non-zero exit instead of silently returning to the caller.
        _exit_missing_provider_credentials()
    return True


def _exit_missing_provider_credentials() -> typer.Exit:
    """Banner + exit for chat commands when the active provider has no key."""
    from unread.ai.providers import _resolve_provider_name

    s = get_settings()
    provider = _resolve_provider_name(s, "chat")
    _print_provider_credentials_banner(provider)
    raise typer.Exit(1)


def _exit_unrecognized_ref(ref: str) -> None:
    """Friendly banner + exit for the bare `unread <ref>` form when ``ref``
    doesn't match any known input shape (file / URL / explicit Telegram).

    Replaces the old "fall through to Telegram fuzzy match" behavior,
    which was confusing — users typing arbitrary text would silently
    end up trying to authenticate with Telegram. Now we reject up
    front and point at the right entry point for each shape.
    """
    pretty = ref.strip()
    console.print(f"[bold yellow]{_tf('err_route_title', ref=f'{pretty!r}')}[/]\n")
    console.print(f"[bold]{_t('err_route_telegram_header')}[/]")
    console.print(f"  [cyan]unread @username[/]               {_t('err_route_telegram_handle')}")
    console.print(f"  [cyan]unread t.me/c/<id>/<msg>[/]       {_t('err_route_telegram_link')}")
    console.print(f"  [cyan]unread -1001234567890[/]          {_t('err_route_telegram_id')}")
    console.print(f"  [cyan]unread tg[/]                       {_t('err_route_telegram_picker')}")
    console.print(f"\n[bold]{_t('err_route_url_header')}[/]")
    console.print("  [cyan]unread https://youtu.be/<id>[/]")
    console.print("  [cyan]unread https://example.com/article[/]")
    console.print(f"\n[bold]{_t('err_route_file_header')}[/]")
    console.print("  [cyan]unread ./path/to/notes.pdf[/]")
    console.print(f"\n[bold]{_t('err_route_stdin_header')}[/]")
    console.print(f'  [cyan]echo "{pretty}" | unread[/]')
    console.print(f'  [cyan]unread - <<< "{pretty}"[/]')
    raise typer.Exit(1)


def _print_provider_credentials_banner(provider: str) -> None:
    """One unified banner for any provider's missing chat credential."""
    from unread.core.paths import default_env_path, ensure_unread_home

    ensure_unread_home()
    env_path = default_env_path()
    label_map = {
        "openai": ("OpenAI", "OPENAI_API_KEY=sk-…"),
        "openrouter": ("OpenRouter", "OPENROUTER_API_KEY=sk-or-…"),
        "anthropic": ("Anthropic (Claude)", "ANTHROPIC_API_KEY=sk-ant-…"),
        "google": ("Google (Gemini)", "GOOGLE_API_KEY=AI…"),
        "local": ("local server", "<set local.base_url in config.toml>"),
    }
    label, env_line = label_map.get(provider, (provider, "<provider-specific key>"))
    console.print(
        f"[bold yellow]{_tf('cred_banner_title_provider', label=label)}[/]\n"
        f"\n"
        f"{_t('cred_banner_run_init_provider')}\n"
        f"\n"
        f"{_tf('cred_banner_env_intro', env_path=f'[bold]{env_path}[/]')}\n"
        f"  {env_line}"
    )


describe_app = _UnreadTyper(
    help=_t("cmd_describe"),
    # _UnreadRootGroup (not _UnreadGroup) because the callback declares
    # an optional positional `ref`. Without _PreferSubcommandsGroup's
    # peel logic, Click would consume "folders" as ref and never route
    # to the subcommand. The chats/cache/backup sub-typers don't have
    # this problem because their callbacks have no positional args.
    cls=_UnreadRootGroup,
)
tg_app.add_typer(describe_app, name="describe")


@describe_app.callback(invoke_without_command=True)
def describe(
    ctx: typer.Context,
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference. Without it, prints an overview of dialogs. "
            "For a chat: shows kind, username, stats, and (for forums) topics. "
            "For a channel: shows linked discussion group and subscriber count."
        ),
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Filter overview by kind: user | group | supergroup | channel | forum.",
    ),
    search: str | None = typer.Option(None, "--search", help="Substring filter on title/username."),
    limit: int | None = typer.Option(None, "--limit", help="Max rows in overview."),
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Show every dialog, including read ones and all kinds. "
        "Default overview: chats with unread messages in forum/group/supergroup.",
    ),
) -> None:
    """List chats (no ref) or inspect one chat (with ref).

    Default overview shows unread forums/groups/supergroups — the places
    real discussion happens. Use --all to see everything, or narrow with
    --kind / --search / --limit. With a ref, forums get a topics table
    and channels get linked-discussion + subscriber count.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand was matched (e.g. `describe folders`).
        return
    from unread.tg.commands import cmd_describe

    _run(
        cmd_describe(
            ref,
            kind=kind,
            search=search,
            limit=limit,
            show_all=show_all,
        )
    )


@describe_app.command("folders", help=_t("cmd_folders"))
def describe_folders() -> None:
    """List your Telegram folders (for use with `--folder NAME`)."""
    _run(_list_folders())


# --- Hidden compatibility aliases: the consolidated `describe` absorbs these.
# Kept callable so existing scripts don't break.


@tg_app.command(hidden=True)
def dialogs(
    search: str | None = typer.Option(None, "--search", help="Substring filter on chat title or @username."),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Filter by chat kind: user | group | supergroup | channel | forum.",
    ),
    limit: int = typer.Option(50, "--limit", help="Max rows to return."),
) -> None:
    """Deprecated: use `tg describe` instead."""
    from unread.tg.commands import cmd_dialogs

    _run(cmd_dialogs(search=search, kind=kind, limit=limit))


@tg_app.command(hidden=True)
def topics(
    chat_ref: str | None = typer.Argument(None),
    chat: int | None = typer.Option(
        None,
        "--chat",
        help="Numeric chat id (alternative to passing the chat ref positionally).",
    ),
) -> None:
    """Deprecated: use `tg describe <ref>` instead."""
    from unread.tg.commands import cmd_topics

    if chat_ref is None and chat is None:
        console.print(f"[red]{_t('cli_ref_or_chat_required')}[/]")
        raise typer.Exit(2)
    _run(cmd_topics(chat_ref if chat_ref is not None else str(chat)))


@tg_app.command(hidden=True)
def resolve(anything: str = typer.Argument(...)) -> None:
    """Diagnostic: parse a reference and show the resolution path."""
    from unread.tg.commands import cmd_resolve

    _run(cmd_resolve(anything))


@tg_app.command("channel-info", hidden=True)
def channel_info(ref: str = typer.Argument(...)) -> None:
    """Deprecated: use `tg describe <channel-ref>` instead."""
    from unread.tg.commands import cmd_channel_info

    _run(cmd_channel_info(ref))


# =========================================================== 5.2 Subscriptions


@chats_app.command("add")
def chats_add(
    ref: str | None = typer.Argument(
        None,
        help="Chat reference. Omit to pick from an interactive list of dialogs.",
    ),
    from_date: str | None = typer.Option(None, "--from-date", help="YYYY-MM-DD"),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Message link or msg_id."),
    last: int | None = typer.Option(None, "--last", help="Backfill last N messages."),
    full_history: bool = typer.Option(False, "--full-history", help="Sync the whole chat (danger)."),
    thread: int | None = typer.Option(None, "--thread", help="Specific forum topic id."),
    all_topics: bool = typer.Option(False, "--all-topics", help="Subscribe to every forum topic."),
    with_comments: bool = typer.Option(False, "--with-comments", help="Channel + discussion group."),
    join: bool = typer.Option(False, "--join", help="Auto-join via invite link if required."),
    no_transcribe: bool = typer.Option(False, "--no-transcribe", help="Disable transcription for this sub."),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Default preset for `unread tg chats run` on this sub (summary, action_items, …). Wizard asks if not set.",
    ),
    period: str | None = typer.Option(
        None,
        "--period",
        help="Default period for `unread tg chats run` on this sub: unread | last24h | last96h | last7 | last30 | last90 | year_start | full. Wizard asks if not set.",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Default enrichments for `unread tg chats run` on this sub. CSV of "
            "voice,videonote,video,image,doc,link. Empty string disables all. "
            "Unset = use config defaults at run time."
        ),
    ),
    no_mark_read: bool = typer.Option(
        False,
        "--no-mark-read",
        help="Don't advance Telegram's read marker after `unread tg chats run` analyzes this sub.",
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help="Telegram chat ref to post the report to (`me` for Saved Messages). Used by `unread tg chats run`.",
    ),
) -> None:
    """Add a subscription (chat / topic / channel with comments).

    Without a `<ref>`, opens the interactive chat picker (same one used by
    `unread analyze`). For a channel, asks whether to also subscribe to its
    linked discussion group; for a forum, asks whether to include every
    topic. CLI flags pre-fill those answers when given.

    The wizard also captures per-subscription defaults consumed by
    `unread tg chats run` — preset, period, enrich kinds, mark-read, post-to — so a
    later `unread tg chats run` walks every enabled sub and analyzes each one with
    its own settings. CLI flags `--preset`, `--period`, `--enrich`,
    `--no-mark-read`, `--post-to` skip the matching wizard step.
    """
    from unread.tg.commands import cmd_chats_add

    _run(
        cmd_chats_add(
            ref=ref,
            from_date=from_date,
            from_msg=from_msg,
            last=last,
            full_history=full_history,
            thread=thread,
            all_topics=all_topics,
            with_comments=with_comments,
            join=join,
            no_transcribe=no_transcribe,
            preset=preset,
            period=period,
            enrich=enrich,
            no_mark_read=no_mark_read,
            post_to=post_to,
        )
    )


@chats_app.command("manage")
def chats_manage() -> None:
    """Interactive panel — list, enable / disable, remove subscriptions.

    Prints the full subscriptions table on entry (preset, period,
    enrich, mark-read, post-to, comments, start), then picks one
    subscription and presents an action menu (toggle on/off, remove
    keeping messages, remove and purge stored messages). Loops back to
    the table after each action. Close with `← Done`, Ctrl-C, or ESC.
    """
    from unread.tg.commands import cmd_chats_manage

    _run(cmd_chats_manage())


async def _list_folders() -> None:
    from rich.table import Table

    from unread.tg.client import tg_client
    from unread.tg.folders import list_folders

    settings = get_settings()
    async with tg_client(settings) as client:
        folders = await list_folders(client)

    if not folders:
        console.print(f"[yellow]{_t('cli_no_folders')}[/]")
        return
    t = Table(title=_t("cli_folders_table_title"))
    t.add_column(_t("cli_folder_col_id"), justify="right")
    t.add_column(_t("cli_folder_col_title"))
    t.add_column(_t("cli_folder_col_icon"))
    t.add_column(_t("cli_folder_col_chats"), justify="right")
    t.add_column(_t("cli_folder_col_kind"))
    for f in folders:
        kind = (
            _t("cli_folder_kind_chatlist")
            if f.is_chatlist
            else (
                _t("cli_folder_kind_rule_based")
                if f.has_rule_based_inclusion and not f.include_chat_ids
                else _t("cli_folder_kind_explicit")
            )
        )
        t.add_row(
            str(f.id),
            f.title,
            f.emoticon or "",
            str(len(f.include_chat_ids)),
            kind,
        )
    console.print(t)
    console.print(f"[grey70]{_t('cli_folders_use_with')}[/]")


# ================================================================ 5.3 Sync


@tg_app.command(help=_t("cmd_sync"))
def sync(
    chat: int | None = typer.Option(
        None,
        "--chat",
        help="Numeric chat id to sync. Mutually exclusive with --all.",
    ),
    thread: int | None = typer.Option(
        None,
        "--thread",
        help="Forum-topic id (only meaningful when paired with `--chat` for a forum).",
    ),
    all_subs: bool = typer.Option(
        False,
        "--all",
        help="Sync every enabled subscription (also the default when no --chat is given).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the work (chat list + message counts) without writing to the DB.",
    ),
) -> None:
    """Incrementally fetch new messages for all (or one) subscriptions."""
    if all_subs and (chat is not None or thread is not None):
        raise typer.BadParameter("--all is mutually exclusive with --chat / --thread.")
    from unread.tg.commands import cmd_sync

    _run(cmd_sync(chat=chat, thread=thread, dry_run=dry_run))


@chats_app.command("run")
def chats_run(
    only_chat: int | None = typer.Option(
        None,
        "--only-chat",
        help="Limit to one chat (numeric chat_id). Default: every enabled subscription.",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Override every sub's stored preset for this run only.",
    ),
    period: str | None = typer.Option(
        None,
        "--period",
        help="Override every sub's stored period: unread | last24h | last96h | last7 | last30 | last90 | year_start | full.",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help="Override stored enrichments — CSV of voice,videonote,video,image,doc,link.",
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Override stored enrichments — enable everything.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Override stored enrichments — disable all enrichment.",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Override stored mark-read setting for this run.",
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help="Override stored post-to target for this run (e.g. `me`, @channel).",
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="Refuse to run any sub whose estimated cost exceeds this (USD).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan table and exit — no backfill, no OpenAI calls.",
    ),
    flat: bool = typer.Option(
        False,
        "--flat",
        help=(
            "Single combined report across every enabled sub instead of "
            "one report per chat. Per-sub stored preset/period/enrich are "
            "ignored — uses CLI overrides + defaults. Saved to "
            "reports/run-flat-<ts>.md."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt before launching the batch.",
    ),
) -> None:
    """Walk every enabled subscription, sync + analyze with stored settings.

    `unread tg chats add` captures per-subscription preset / period / enrich
    kinds / mark-read / post-to. `unread tg chats run` walks each enabled
    subscription (skipping comments-side subs — they ride along with
    their parent channel via auto `--with-comments`) and dispatches
    `cmd_analyze` with that sub's stored settings. Override flags
    (`--preset`, `--period`, `--enrich`, `--mark-read`, `--post-to`)
    apply to every sub for this invocation only and don't touch the
    saved values.

    `--flat` switches to a single multi-chat report: every enabled
    sub's messages are merged into one input and analyzed in one
    pass. Per-chat sections in the report keep their own citation
    templates so links resolve correctly.
    """
    from unread.runner import cmd_run

    _run(
        cmd_run(
            only_chat=only_chat,
            preset_override=preset,
            period_override=period,
            enrich_override=enrich,
            enrich_all_override=enrich_all,
            no_enrich_override=no_enrich,
            mark_read_override=mark_read,
            post_to_override=post_to,
            max_cost=max_cost,
            dry_run=dry_run,
            flat=flat,
            yes=yes,
        )
    )


@tg_app.command(hidden=True)
def backfill(
    chat: int = typer.Option(..., "--chat", help="Numeric chat id to backfill (required)."),
    from_msg: str = typer.Option(
        ...,
        "--from-msg",
        help="Anchor message id or t.me link. The backfill walks from here in `--direction`.",
    ),
    direction: str = typer.Option("back", "--direction", help="back | forward"),
) -> None:
    """One-shot history backfill starting from a specific message.

    Niche helper — most users want `analyze --from-msg <id>` or
    `dump --from-msg <id>` instead.
    """
    from unread.tg.commands import cmd_backfill

    _run(cmd_backfill(chat=chat, from_msg=from_msg, direction=direction))


# =================================================================== 5.4 Analyze


def _looks_like_path_prefix(incomplete: str) -> bool:
    """Heuristic: should we treat `incomplete` as a partial filesystem path?

    We complete paths only when the user has clearly committed to a path
    shape — leading `./`, `../`, `/`, `~`, or an embedded slash that
    isn't part of a URL scheme. Bare words like `settings` stay free for
    subcommand-name completion; URLs (`https://…`, `t.me/…`) are also
    excluded so we don't suggest fake matches under a remote-looking
    prefix.
    """
    if not incomplete:
        return False
    if "://" in incomplete:
        return False
    if incomplete.lower().startswith(("t.me/", "telegram.me/", "telegram.org/")):
        return False
    return (
        incomplete.startswith(("./", "../", "/", "~"))
        or incomplete in (".", "..")
        or "/" in incomplete
        or "\\" in incomplete
    )


def _complete_path_prefix(incomplete: str):  # type: ignore[no-untyped-def]
    """Delegate file completion to the shell when the prefix looks pathy.

    Returns a single ``CompletionItem(type="file")`` which our zsh / fish
    completion scripts route to ``_path_files -f`` / ``__fish_complete_path``.
    Those handle every detail the shell already knows: no trailing space
    after a file, trailing slash after a dir, symlink detection, the
    user's hidden-file policy, and — critically — they don't append a
    space so the user can keep refining the path.

    Doing the glob in Python (and returning plain strings) routes
    through ``compadd -U`` and adds a trailing space. Delegating to the
    shell's native file-completion machinery is shorter, faster, and
    behaves correctly out of the box.

    Returning an empty list when the prefix isn't pathy lets the
    subcommand-name fallback in ``_complete_root_ref`` take over.
    """
    if not _looks_like_path_prefix(incomplete):
        return []
    from click.shell_completion import CompletionItem

    return [CompletionItem(value="", type="file")]


def _complete_root_ref(ctx, args, incomplete):  # type: ignore[no-untyped-def]
    """Yield path matches and visible subcommand names for `unread <Tab>` completion.

    Without this, Click's `_resolve_incomplete` picks the unfilled
    optional `ref` positional as the completion target — and since
    `ref` has no value enumerator (chat handles / URLs / file paths
    are dynamic), the user gets no suggestions at all when pressing
    Tab right after `unread`. We hand back a mix of:

      - file/directory entries when `incomplete` looks pathy (so
        `unread ./re<Tab>` expands to `./reports/`),
      - subcommand names otherwise (so `unread se<Tab>` → `settings`).

    Typer's `autocompletion=` callback signature: ``(ctx, args, incomplete)
    → list[tuple[str, str] | str]``. Returning a mix of bare strings
    (paths) and ``(name, help)`` tuples (subcommands) is fine — zsh's
    `_describe` handles both shapes.
    """
    out: list[tuple[str, str] | str] = []
    out.extend(_complete_path_prefix(incomplete))
    if _looks_like_path_prefix(incomplete):
        # Don't pollute path completion with subcommand names — once the
        # user committed to a path shape, subcommand suggestions are noise.
        return out
    root = ctx.command
    # Sort alphabetically so the completion menu matches the order
    # `unread help` prints. `list_commands` returns registration order
    # (the order `@app.command` decorators ran), which is unpredictable
    # to scan in a Tab popup.
    for name in sorted(root.list_commands(ctx)):
        cmd = root.get_command(ctx, name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        if name.startswith(incomplete):
            out.append((name, cmd.help or ""))
    return out


def _complete_ref(ctx, args, incomplete):  # type: ignore[no-untyped-def]
    """File/directory completion for the `ref` arg on `ask` and `dump`.

    Same logic as `_complete_root_ref` but without subcommand-name
    fallback — `unread ask` / `unread dump` don't have nested
    subcommands competing with the ref positional.
    """
    return _complete_path_prefix(incomplete)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    ref: str | None = typer.Argument(
        None,
        autocompletion=_complete_root_ref,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "A message link like t.me/c/ID/MSG is treated as single-message "
            "mode (analyze just that one message, auto-transcribing voice/video). "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`unread -- -1001234567890`. Omit to pick every dialog "
            "with unread messages (interactive)."
        ),
    ),
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the unread version and exit.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
    thread: int | None = typer.Option(None, "--thread", help="Forum-topic id."),
    msg: str | None = typer.Option(
        None,
        "--msg",
        help="Analyze just one message (id or link). Auto-transcribes voice/video if needed.",
    ),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Start at this msg_id (or a message link)."),
    full_history: bool = typer.Option(
        False, "--full-history", help="Analyze the whole chat, not just unread."
    ),
    since: str | None = typer.Option(None, "--since", help="Start date (YYYY-MM-DD)."),
    until: str | None = typer.Option(None, "--until", help="End date (YYYY-MM-DD)."),
    last_days: int | None = typer.Option(
        None,
        "--last-days",
        help="Restrict to messages newer than N days ago. Mutually exclusive with other window flags.",
    ),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help="Restrict to messages newer than N hours ago. Mutually exclusive with other window flags.",
    ),
    last_minutes: int | None = typer.Option(
        None,
        "--last-minutes",
        help="Restrict to messages newer than N minutes ago. Mutually exclusive with other window flags.",
    ),
    last_msgs: int | None = typer.Option(
        None,
        "--last-msgs",
        help="Analyze the last N messages, regardless of unread state. Mutually exclusive with other window flags.",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Analysis preset (default: summary for chats, single_msg for one message).",
    ),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="Path to a custom prompt body to use instead of a preset."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Override the chat model used for map + reduce passes."
    ),
    filter_model: str | None = typer.Option(
        None, "--filter-model", help="Override the cheap model used for the pre-filter pass."
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write the report to this path instead of the default reports folder."
    ),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="[DEPRECATED] Same as --no-save.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="[DEPRECATED] No-op. Saving is the default; pass --no-save to opt out.",
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        help="Skip writing the report file. The result still renders in the terminal.",
    ),
    no_console: bool = typer.Option(
        False,
        "--no-console",
        help="Skip rendering the report to the terminal. The report file is still saved. Cannot be combined with --no-save.",
    ),
    plain_citations: bool = typer.Option(
        False,
        "--plain-citations",
        help="Render citations as plain URLs in the console (use when your terminal can't handle OSC 8 hyperlinks). Saved markdown is unaffected.",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Advance Telegram's read marker after analysis. Without either flag, you'll be asked interactively.",
    ),
    all_flat: bool = typer.Option(
        False,
        "--all-flat",
        help="Forum only: analyze the whole forum as one chat. Needs an explicit period flag.",
    ),
    all_per_topic: bool = typer.Option(
        False,
        "--all-per-topic",
        help="Forum only: one report per topic. Reports land in reports/{chat}/.",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Bypass the analysis cache for this run (still writes a fresh entry)."
    ),
    redact: bool | None = typer.Option(
        None,
        "--redact/--no-redact",
        help="Scrub phone/email/IBAN/card numbers from the LLM prompt. The DB and saved report keep originals.",
    ),
    include_transcripts: bool = typer.Option(
        True,
        "--include-transcripts/--text-only",
        help="Include voice/video transcripts and image descriptions in the analysis (default: on).",
    ),
    min_msg_chars: int | None = typer.Option(
        None,
        "--min-msg-chars",
        help="Skip messages shorter than this many characters (after enrichment).",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help="Comma-separated enrichments: voice, videonote, video, image, doc, link. E.g. --enrich=voice,image,link.",
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Enable every enrichment (voice/videonote/video/image/doc/link). Spendy; use for exploratory runs.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Disable all enrichments for this run, even those that would default on.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmations. Useful for scripting and batch runs.",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help="Batch-analyze every unread chat in this Telegram folder. Case-insensitive on folder title; only meaningful without <ref>.",
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="Abort (or confirm without --yes) if the estimated USD cost exceeds N.",
    ),
    post_saved: bool = typer.Option(
        False,
        "--post-saved",
        help="After analysis, post the result to your Telegram Saved Messages chat. Sugar for --post-to=me.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve, backfill, count, and print a cost estimate. Skips LLM and enrichment — no spend.",
    ),
    cite_context: int = typer.Option(
        0,
        "--cite-context",
        help="Append a Sources section with N messages of context around each citation. 0 = off; capped at 30 citations.",
    ),
    self_check: bool = typer.Option(
        False,
        "--self-check",
        help="Run a cheap-model audit pass that lists unsupported claims. Adds ~10% to cost.",
    ),
    by: str | None = typer.Option(
        None,
        "--by",
        help="Filter to one sender (substring on name or numeric sender_id). Composes with other filters.",
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help="After analysis, post the result to this chat (ref or 'me' for Saved Messages).",
    ),
    repeat_last: bool = typer.Option(
        False,
        "--repeat-last",
        help="Re-use flags from the most recent successful analyze on <ref>. Explicit flags on this run still win.",
    ),
    with_comments: bool = typer.Option(
        False,
        "--with-comments",
        help="For a channel: also include messages from its linked discussion group (comments). No-op for non-channel chats.",
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help="UI language (en, ru, de, …). Drives wizard / banner / saved-report headings. Defaults to [locale] language.",
    ),
    report_language: str | None = typer.Option(
        None,
        "--report-language",
        help="Language the LLM writes the analysis in (en, ru, de, …). Picks the presets/<lang>/ tree. Defaults to [locale] report_language, falling back to --language.",
    ),
    source_language: str | None = typer.Option(
        None,
        "--content-language",
        help="Source-content language hint (en, ru, zh, …). Whisper-style override — empty = LLM auto-detects. Defaults to [locale] content_language.",
    ),
    youtube_source: str = typer.Option(
        "auto",
        "--youtube-source",
        help="YouTube transcript source: auto (captions, fallback to Whisper), captions, or audio (always Whisper).",
    ),
    no_truncation_retry: bool = typer.Option(
        False,
        "--no-truncation-retry",
        "-T",
        help="Don't retry on truncated output. Default: bump max_tokens (capped per model) and re-bill the full prompt.",
    ),
) -> None:
    """Default action: analyze a chat / YouTube video / web page.

    `unread <ref>` is the analyze entry point. Without `<ref>` (and
    without a subcommand), opens the interactive wizard. With a Telegram
    folder (`--folder NAME`) and no `<ref>`, batch-analyzes every chat in
    that folder with unread messages.

    For forum chats: `--thread N` targets one topic, `--all-flat` treats
    the forum as one chat (needs `--last-days` / `--full-history`),
    `--all-per-topic` runs one analysis per topic.

    For Telegram-only setup, use `unread login` (or `unread tg login --force`
    to re-link). The interactive chat picker is `unread tg` — `tg` is a
    magic ref token, not a command.
    """
    setup_logging(verbose=verbose)
    if ctx.invoked_subcommand is not None:
        # A subcommand was matched (`tg`, `ask`, `dump`, `init`, …); let it run.
        # `unread tg` (with no further verb) routes here too — the `tg`
        # subgroup's own callback opens the analyze wizard.
        return
    # Stdin auto-detect: `cat foo.txt | unread` (no ref, non-TTY stdin)
    # routes the piped bytes through the file analyzer. The explicit
    # form is `unread -`; both flow through `cmd_analyze_file` with a
    # sentinel that tells it to read stdin instead of opening a path.
    if ref == "-" or (ref is None and _stdin_has_data()):
        ref = _STDIN_REF_SENTINEL
    # Bare `unread <ref>` no longer falls through to Telegram fuzzy
    # chat-title match — that's a surprising path for users who meant
    # "analyze this string of text". The escape hatch is `unread tg`
    # (the subgroup, opens the picker) which can find chats by title.
    if (
        ref is not None
        and ref != _STDIN_REF_SENTINEL
        and not _looks_like_local_file(ref)
        and not ref.lower().startswith(("http://", "https://", "tg://"))
        and not _is_explicit_telegram_ref(ref)
    ):
        _exit_unrecognized_ref(ref)
    if ref is None:
        # First-run nudge: if the install isn't usable yet (no AI key)
        # AND the user has never run the wizard (no install.toml
        # pointer), offer to run setup now instead of dropping them on
        # the quickstart panel. Once the pointer exists the user has
        # already been through the wizard and made their choices — even
        # if they skipped AI / Telegram, don't re-prompt on every bare
        # `unread`. The status panel below already lists missing pieces
        # and points at `unread init`.
        from unread.core.paths import install_pointer_path

        if _stdin_has_data() is False and _is_uninitialized() and not install_pointer_path().is_file():
            _maybe_offer_init()
            # Either the wizard ran (and we're now configured) or the
            # user said no. Either way, fall through to the quickstart
            # panel below — useful as a reminder of common verbs.
        # Bare `unread` is an orientation panel, not a command — the
        # interactive wizard moved to `unread init`. This keeps the
        # zero-arg invocation cheap and discoverable instead of
        # surprising new users with a credential prompt or wizard.
        _print_quickstart()
        return
    # `unread <ref>` needs ~/.unread/ ready plus (for Telegram refs)
    # an authorized session. Skipped for YouTube / non-Telegram URL
    # refs since those analyzers don't need a Telegram session at all.
    if not _ensure_ready_for_analyze(ref):
        return
    _maybe_warn_subcommand_collision(ref)
    _dispatch_analyze(
        ref=ref,
        thread=thread,
        msg=msg,
        from_msg=from_msg,
        full_history=full_history,
        since=since,
        until=until,
        last_days=last_days,
        last_hours=last_hours,
        last_minutes=last_minutes,
        last_msgs=last_msgs,
        preset=preset,
        prompt_file=prompt_file,
        model=model,
        filter_model=filter_model,
        output=output,
        console_out=console_out,
        save=save,
        no_save=no_save,
        no_console=no_console,
        plain_citations=plain_citations,
        mark_read=mark_read,
        no_cache=no_cache,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        enrich=enrich,
        enrich_all=enrich_all,
        no_enrich=no_enrich,
        yes=yes,
        all_flat=all_flat,
        all_per_topic=all_per_topic,
        folder=folder,
        max_cost=max_cost,
        post_saved=post_saved,
        dry_run=dry_run,
        cite_context=cite_context,
        self_check=self_check,
        by=by,
        post_to=post_to,
        repeat_last=repeat_last,
        with_comments=with_comments,
        language=language,
        report_language=report_language,
        source_language=source_language,
        youtube_source=youtube_source,
        disable_truncation_retry=no_truncation_retry,
    )


# ============================================================== 5.4b Download media


@tg_app.command("download-media", hidden=True)
def download_media(
    ref: str = typer.Argument(
        ...,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "Saves photos/voice/video/documents from this chat to disk."
        ),
    ),
    thread: int | None = typer.Option(None, "--thread", help="Forum-topic id."),
    types: str | None = typer.Option(
        None,
        "--types",
        help=("Comma-separated subset: voice, videonote, video, photo, doc. Default: all five."),
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(
        None,
        "--last-days",
        help="Shortcut for --since now-N (day-granular).",
    ),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help="Shortcut for --since now-N (hour-granular). Wins over --last-days when combined.",
    ),
    last_minutes: int | None = typer.Option(
        None,
        "--last-minutes",
        help="Shortcut for --since now-N (minute-granular). Wins over --last-hours / --last-days when combined.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Base output dir (default: reports/). Files land under reports/<chat-slug>/media/.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Max files to download this run."),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Re-download even if a file for the same msg_id already exists.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview counts + sample without writing."),
) -> None:
    """Download raw media files (photos, voice, video, documents) from a chat.

    Works off messages already in the local DB — run [cyan]unread sync[/] or
    [cyan]unread analyze[/] first if you need the latest messages. Safe to
    re-run: files are skipped when they already exist on disk (pass
    [cyan]--overwrite[/] to force). No OpenAI calls; no cost beyond
    Telegram download bandwidth.
    """
    from unread.media.commands import cmd_download_media

    _run(
        cmd_download_media(
            ref=ref,
            thread=thread,
            types=types,
            since=since,
            until=until,
            last_days=last_days,
            last_hours=last_hours,
            last_minutes=last_minutes,
            output=output,
            limit=limit,
            overwrite=overwrite,
            dry_run=dry_run,
        )
    )


# ============================================================== 5.5 Maintenance


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_stats"))
def stats(
    since: str | None = typer.Option(
        None,
        "--since",
        help="Lower bound: YYYY-MM-DD or relative (e.g. `7d`, `2w`). Default: all-time.",
    ),
    by: str = typer.Option("preset", "--by", help="chat | preset | model | day | kind"),
) -> None:
    """Aggregate API spend, cache hit rate and run counts."""
    from unread.analyzer.commands import cmd_stats

    _run(cmd_stats(since=since, by=by))


@cache_ai_app.command("purge")
def cache_ai_purge(
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Nd / Nw (default: 90d). Mutually exclusive with --all.",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Restrict to one preset (e.g. summary, brief). Mutually exclusive with --all.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Restrict to one model (e.g. gpt-4o-mini). Mutually exclusive with --all.",
    ),
    all_entries: bool = typer.Option(
        False,
        "--all",
        help="Purge every cached entry regardless of age, preset, or model. Mutually exclusive with --older-than / --preset / --model.",
    ),
    vacuum: bool = typer.Option(False, "--vacuum", help="Run VACUUM after purge to reclaim disk."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete cached analysis results by age and filters."""
    if all_entries and (preset is not None or model is not None):
        raise typer.BadParameter("--all is mutually exclusive with --preset / --model.")
    if all_entries and older_than is not None:
        raise typer.BadParameter(
            "--all purges every cached row regardless of age — drop --older-than or drop --all."
        )
    effective_older_than = older_than if older_than is not None else "90d"
    _run(
        _cache_purge(
            effective_older_than,
            preset,
            model,
            vacuum,
            yes,
            all_entries=all_entries,
        )
    )


async def _cache_purge(
    older_than: str,
    preset: str | None,
    model: str | None,
    vacuum: bool,
    yes: bool,
    *,
    all_entries: bool = False,
) -> None:
    settings = get_settings()
    if all_entries:
        days: int | None = None
    else:
        days = _parse_duration_days(older_than)
        if days <= 0:
            console.print(f"[yellow]{_t('cli_skipped_label')}[/] {_t('cli_cache_purge_min_days')}")
            return
    async with open_repo(settings.storage.data_path) as repo:
        preview = await repo.cache_purge_preview(
            older_than_days=days,
            preset=preset,
            model=model,
            breakdown_limit=10,
        )
        if preview["rows"] == 0:
            console.print(f"[yellow]{_t('cli_cache_nothing_to_purge')}[/]")
            return

        # Scope description: "every cached row" for --all, otherwise the
        # filter chain ("older than 90d, preset=summary, model=gpt-…").
        scope_bits: list[str] = []
        if all_entries:
            scope_bits.append(_t("cli_cache_scope_all"))
        else:
            scope_bits.append(_tf("cli_cache_older_than", days=days).rstrip("."))
            if preset:
                scope_bits.append(f"preset={preset}")
            if model:
                scope_bits.append(f"model={model}")
        scope = ", ".join(scope_bits)

        oldest = str(preview["oldest"])[:10] if preview["oldest"] else "—"
        newest = str(preview["newest"])[:10] if preview["newest"] else "—"
        console.print(
            f"[bold]{_t('cli_cache_purge_preview_title')}[/] ({scope}):\n"
            f"  rows to delete:        [red]{preview['rows']:,}[/]\n"
            f"  result text on disk:   {_fmt_bytes(preview['result_bytes'])}\n"
            f"  saved API spend (cum): ${preview['saved_cost_usd']:.4f}\n"
            f"  age range:             {oldest} → {newest}"
        )

        if preview["by_group"]:
            console.print(f"\n[bold]{_t('cli_cache_breakdown_title')}[/]")
            for g in preview["by_group"]:
                label = f"{g['preset']} @ {g['model']}"
                console.print(
                    f"  • {label} — [red]{g['rows']:,}[/] rows "
                    f"[grey70]({_fmt_bytes(g['result_bytes'])}, "
                    f"${g['saved_cost_usd']:.4f} saved)[/]"
                )
            shown = sum(g["rows"] for g in preview["by_group"])
            remaining = max(0, preview["rows"] - shown)
            if remaining > 0:
                console.print(f"  [grey70]{_tf('cli_cache_breakdown_more', n=remaining)}[/]")

        if not yes:
            from unread.util.prompt import confirm as _confirm

            if not _confirm(_t("cli_cache_purge_proceed_q"), default=False):
                console.print(f"[yellow]{_t('cli_aborted')}[/]")
                return

        removed = await repo.cache_purge(older_than_days=days, preset=preset, model=model)
        if all_entries:
            console.print(f"[green]{_t('cli_purged_label')}[/] {_tf('cli_cache_purged_all_msg', n=removed)}")
        else:
            console.print(
                f"[green]{_t('cli_purged_label')}[/] {_tf('cli_cache_purged_msg', n=removed, days=days)}"
            )
        if vacuum:
            reclaimed = await repo.vacuum()
            console.print(
                f"[green]{_t('cli_vacuumed_label')}[/] "
                f"{_tf('cli_db_vacuumed_msg', size=_fmt_bytes(reclaimed))}"
            )


_SOURCE_KIND_HELP = (
    "Source kind to operate on: website (extracted page text), youtube "
    "(metadata + transcript), or file (extracted local-file text). Pass the "
    "value as `website`, `youtube`, or `file`. When omitted, all kinds apply."
)


@cache_sources_app.command("ls")
def cache_sources_ls(
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=_SOURCE_KIND_HELP,
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Max rows to show per kind. Default: 50.",
    ),
) -> None:
    """List cached source rows (websites / YouTube videos / local files).

    These are the per-input caches that let `unread <url>` skip the
    re-fetch / re-extract step on a second run. Distinct from
    `analysis_cache` (the per-LLM-call result cache cleaned by
    `cache purge`) and from Telegram message-text retention
    (`cache tg`). Use `cache sources-purge` to delete entries.
    """
    _run(_cache_sources_ls(kind, limit))


@cache_sources_app.command("purge")
def cache_sources_purge_cmd(
    url: str | None = typer.Option(
        None,
        "--url",
        help="Delete only the row whose canonical URL / file path matches exactly.",
    ),
    domain: str | None = typer.Option(
        None,
        "--domain",
        help="Delete every website-cache row from this domain (e.g. `zh.wikipedia.org`). "
        "No effect on youtube / file kinds.",
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=_SOURCE_KIND_HELP,
    ),
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Age threshold (Nd / Nw, e.g. 30d, 4w). Mutually exclusive with --all.",
    ),
    all_entries: bool = typer.Option(
        False,
        "--all",
        help="Wipe every cached source row of the selected kind(s). "
        "Mutually exclusive with --url / --domain / --older-than.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete cached source rows. Filter by --url, --domain, --kind, --older-than, or --all."""
    if all_entries and (url or domain or older_than):
        raise typer.BadParameter(
            "--all is mutually exclusive with --url / --domain / --older-than. "
            "Pick the explicit filters OR --all, not both."
        )
    if not (all_entries or url or domain or older_than):
        raise typer.BadParameter(
            "No filter set. Pass at least one of --url / --domain / --older-than / --all."
        )
    _run(
        _cache_sources_purge(
            url=url, domain=domain, kind=kind, older_than=older_than, yes=yes, all_entries=all_entries
        )
    )


async def _cache_sources_ls(kind: str | None, limit: int) -> None:
    settings = get_settings()
    from rich.table import Table

    from unread.db.repo import Repo as _Repo

    kinds = (kind,) if kind else _Repo.source_cache_kinds()
    if kind and kind not in _Repo.source_cache_kinds():
        valid = ", ".join(_Repo.source_cache_kinds())
        raise typer.BadParameter(f"Unknown --kind {kind!r}. Valid: {valid}.")

    async with open_repo(settings.storage.data_path) as repo:
        any_rows = False
        for k in kinds:
            rows = await repo.list_source_cache(k, limit=limit)
            counts = await repo.count_source_cache(k)
            total = int(counts.get("rows") or 0)
            console.print(
                f"\n[bold]{k}[/] — {total:,} cached row(s)"
                + (f" (showing {len(rows)})" if total > len(rows) else "")
            )
            if not rows:
                console.print("  [grey70](none)[/]")
                continue
            any_rows = True
            t = Table(show_header=True, header_style="bold")
            t.add_column("fetched")
            t.add_column("id", style="grey70")
            if k == "website":
                t.add_column("domain")
            t.add_column("label")
            for r in rows:
                fetched = (str(r.get("fetched_at") or ""))[:19]
                row_cells = [fetched, str(r.get("id") or "")]
                if k == "website":
                    row_cells.append(str(r.get("domain") or ""))
                row_cells.append(str(r.get("label") or ""))
                t.add_row(*row_cells)
            console.print(t)
        if not any_rows and not kind:
            console.print("\n[grey70]No cached sources. Run `unread <url-or-file>` to populate.[/]")


async def _cache_sources_purge(
    *,
    url: str | None,
    domain: str | None,
    kind: str | None,
    older_than: str | None,
    yes: bool,
    all_entries: bool,
) -> None:
    settings = get_settings()
    from unread.db.repo import Repo as _Repo

    kinds = (kind,) if kind else _Repo.source_cache_kinds()
    if kind and kind not in _Repo.source_cache_kinds():
        valid = ", ".join(_Repo.source_cache_kinds())
        raise typer.BadParameter(f"Unknown --kind {kind!r}. Valid: {valid}.")

    days: int | None = None
    if older_than is not None:
        days = _parse_duration_days(older_than)
        if days <= 0:
            console.print(f"[yellow]{_t('cli_skipped_label')}[/] {_t('cli_cache_purge_min_days')}")
            return

    async with open_repo(settings.storage.data_path) as repo:
        # Preview every selected kind before touching anything so the
        # confirmation prompt summarizes the full blast radius.
        previews: list[tuple[str, dict]] = []
        for k in kinds:
            preview = await repo.count_source_cache(k, url=url, domain=domain, older_than_days=days)
            if preview.get("rows", 0) > 0:
                previews.append((k, preview))
        if not previews:
            console.print("[yellow]Nothing to purge with the given filters.[/]")
            return

        scope_bits: list[str] = []
        if all_entries:
            scope_bits.append("all entries")
        if url:
            scope_bits.append(f"url={url}")
        if domain:
            scope_bits.append(f"domain={domain}")
        if days is not None:
            scope_bits.append(f"older than {days}d")
        if kind:
            scope_bits.append(f"kind={kind}")
        scope = ", ".join(scope_bits) or "(no filter)"

        console.print(f"[bold]Source-cache purge preview[/] ({scope}):")
        total_rows = 0
        for k, p in previews:
            rows = int(p.get("rows") or 0)
            total_rows += rows
            oldest = (str(p.get("oldest") or ""))[:10] or "—"
            newest = (str(p.get("newest") or ""))[:10] or "—"
            console.print(f"  • {k}: [red]{rows:,}[/] row(s)  [grey70](age range: {oldest} → {newest})[/]")
        console.print(f"  total: [red]{total_rows:,}[/] row(s)")

        if not yes:
            from unread.util.prompt import confirm as _confirm

            if not _confirm("Purge the rows above?", default=False):
                console.print(f"[yellow]{_t('cli_aborted')}[/]")
                return

        deleted_total = 0
        for k, _ in previews:
            n = await repo.purge_source_cache(
                k, url=url, domain=domain, older_than_days=days, all_entries=all_entries
            )
            deleted_total += n
            console.print(f"[green]Purged[/] {n:,} {k} row(s).")
        console.print(f"[green]Done.[/] Removed {deleted_total:,} cached source row(s).")


@cache_sources_app.command("stats")
def cache_sources_stats_cmd() -> None:
    """Aggregate counts per source kind: rows + age range."""
    _run(_cache_sources_stats())


async def _cache_sources_stats() -> None:
    from rich.table import Table

    from unread.db.repo import Repo as _Repo

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        t = Table(show_header=True, header_style="bold")
        t.add_column("kind")
        t.add_column("rows", justify="right")
        t.add_column("oldest")
        t.add_column("newest")
        any_rows = False
        for k in _Repo.source_cache_kinds():
            c = await repo.count_source_cache(k)
            rows = int(c.get("rows") or 0)
            oldest = (str(c.get("oldest") or ""))[:10] or "—"
            newest = (str(c.get("newest") or ""))[:10] or "—"
            t.add_row(k, f"{rows:,}", oldest, newest)
            any_rows = any_rows or rows > 0
        console.print(t)
        if not any_rows:
            console.print("[grey70]No cached sources. Run `unread <url-or-file>` to populate.[/]")


@cache_sources_app.command("show")
def cache_sources_show_cmd(
    source_id: str = typer.Argument(
        ..., help="page_id (websites) / video_id (YouTube) / file_id (local files)."
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=_SOURCE_KIND_HELP + " When omitted, all kinds are searched and the first match wins.",
    ),
) -> None:
    """Print a stored source row's metadata + paragraph preview.

    The full extracted text isn't dumped (use `export` for that) — this
    is a compact diagnostic view: where the row came from, when it was
    fetched, what the extractor produced.
    """
    _run(_cache_sources_show(source_id, kind))


async def _cache_sources_show(source_id: str, kind: str | None) -> None:
    import json as _json

    from unread.db.repo import Repo as _Repo

    settings = get_settings()
    if kind and kind not in _Repo.source_cache_kinds():
        valid = ", ".join(_Repo.source_cache_kinds())
        raise typer.BadParameter(f"Unknown --kind {kind!r}. Valid: {valid}.")
    kinds = (kind,) if kind else _Repo.source_cache_kinds()

    async with open_repo(settings.storage.data_path) as repo:
        row: dict | None = None
        matched_kind: str | None = None
        for k in kinds:
            row = await repo.get_source_cache(k, source_id)
            if row is not None:
                matched_kind = k
                break
        if row is None:
            console.print(f"[red]No cached source found for id `{source_id}`[/]")
            raise typer.Exit(1)

        # Pretty-print the row's interesting columns; paragraphs is
        # always huge, so summarize length + show first/last paragraph
        # as a sanity check that the extraction looks right.
        console.print(f"[bold]{matched_kind}[/]  id=[grey70]{source_id}[/]")
        for col in (
            "url",
            "abs_path",
            "name",
            "title",
            "site_name",
            "channel_title",
            "author",
            "published",
            "upload_date",
            "language",
            "word_count",
            "duration_sec",
            "view_count",
            "fetched_at",
            "transcribed_at",
            "extractor",
            "transcript_source",
            "content_hash",
        ):
            val = row.get(col)
            if val is None or val == "":
                continue
            console.print(f"  {col:18} {val}")

        # Paragraphs preview (websites + files) or transcript snippet (youtube).
        body_field = "paragraphs_json" if matched_kind in {"website", "file"} else "transcript"
        body = row.get(body_field)
        if body:
            if matched_kind in {"website", "file"}:
                try:
                    paragraphs = list(_json.loads(body))
                except Exception:
                    paragraphs = []
                console.print(f"  paragraphs         {len(paragraphs):,}")
                if paragraphs:
                    console.print(
                        f"\n  [grey70]first[/]  {paragraphs[0][:200]}{'…' if len(paragraphs[0]) > 200 else ''}"
                    )
                    if len(paragraphs) > 1:
                        last = paragraphs[-1]
                        console.print(f"  [grey70]last[/]   {last[:200]}{'…' if len(last) > 200 else ''}")
            else:
                snippet = str(body)[:400]
                tail = "…" if len(body) > 400 else ""
                console.print(f"\n  [grey70]transcript[/] ({len(body):,} chars):")
                console.print(f"  {snippet}{tail}")


@cache_sources_app.command("export")
def cache_sources_export_cmd(
    output: Path = typer.Option(
        ..., "--output", "-o", help="Output path. JSONL is the only supported format."
    ),
    kind: str | None = typer.Option(None, "--kind", help=_SOURCE_KIND_HELP),
    include_paragraphs: bool = typer.Option(
        False,
        "--include-paragraphs",
        help="Include the extracted body / transcript in the dump. Off by default — the dump is intended as a metadata-only inventory.",
    ),
) -> None:
    """Dump cached source rows to JSONL for backup / inspection.

    One JSON object per line, fields per kind. Body / transcript text is
    omitted unless `--include-paragraphs` is passed (so the file stays a
    grep-friendly inventory by default).
    """
    _run(_cache_sources_export(output, kind, include_paragraphs))


async def _cache_sources_export(output: Path, kind: str | None, include_paragraphs: bool) -> None:
    import json as _json

    from unread.db.repo import Repo as _Repo

    settings = get_settings()
    if kind and kind not in _Repo.source_cache_kinds():
        valid = ", ".join(_Repo.source_cache_kinds())
        raise typer.BadParameter(f"Unknown --kind {kind!r}. Valid: {valid}.")
    kinds = (kind,) if kind else _Repo.source_cache_kinds()

    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with open_repo(settings.storage.data_path) as repo:
        with output.open("w", encoding="utf-8") as f:
            for k in kinds:
                # Pull rows in chunks so giant caches don't blow memory.
                # `list_source_cache` already returns newest-first; we
                # raise the limit very high since we're streaming to disk.
                rows = await repo.list_source_cache(k, limit=100_000)
                for r in rows:
                    full = await repo.get_source_cache(k, str(r["id"]))
                    if full is None:
                        continue
                    if not include_paragraphs:
                        full = {
                            kk: vv
                            for kk, vv in full.items()
                            if kk not in {"paragraphs_json", "transcript", "transcript_timed_json"}
                        }
                    full["_kind"] = k
                    f.write(_json.dumps(full, ensure_ascii=False, default=str) + "\n")
                    written += 1
    console.print(f"[green]Wrote[/] {written:,} cached-source row(s) to {output}.")


# ----------------------------- TG cache (`messages` table) -----------------------------


@cache_tg_app.command("ls")
def cache_tg_ls_cmd(
    limit: int = typer.Option(50, "--limit", help="Max chats to show."),
) -> None:
    """List synced Telegram chats with message counts (newest activity first).

    Shows what's currently in the local `messages` cache: per-chat
    message totals, how many still carry text vs. just metadata, how
    many have a transcript attached, and the oldest / newest message
    date. Use `cache tg show <chat_id>` for one chat's detail.
    """
    _run(_cache_tg_ls(limit))


async def _cache_tg_ls(limit: int) -> None:
    from rich.table import Table

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        overview = await repo.tg_overview()
        if overview["messages"] == 0:
            console.print("[yellow]No Telegram messages cached.[/] Run `unread sync` to populate.")
            return
        rows = await repo.tg_chats_summary(limit=limit)
        oldest = str(overview["oldest"] or "")[:10] or "—"
        newest = str(overview["newest"] or "")[:10] or "—"
        console.print(
            f"[bold]messages[/] — {overview['messages']:,} row(s) across "
            f"{overview['chats']:,} chat(s) — age range {oldest} → {newest}"
        )
        t = Table(show_header=True, header_style="bold")
        t.add_column("chat_id", style="grey70")
        t.add_column("title")
        t.add_column("messages", justify="right")
        t.add_column("text", justify="right")
        t.add_column("transcripts", justify="right")
        t.add_column("oldest")
        t.add_column("newest")
        for r in rows:
            o = (str(r["oldest"] or ""))[:10] or "—"
            n = (str(r["newest"] or ""))[:10] or "—"
            t.add_row(
                str(r["chat_id"]),
                str(r["title"] or ""),
                f"{r['messages']:,}",
                f"{r['with_text']:,}",
                f"{r['with_transcript']:,}",
                o,
                n,
            )
        console.print(t)
        if overview["chats"] > len(rows):
            console.print(
                f"[grey70](showing top {len(rows)} of {overview['chats']:,} chats; use --limit to expand)[/]"
            )


@cache_tg_app.command("stats")
def cache_tg_stats_cmd() -> None:
    """One-shot summary of the local Telegram cache.

    Aggregate row counts (total / with-text / with-transcript), the
    chat count, and the oldest / newest message dates. Companion to
    `cache tg ls` (which does per-chat detail).
    """
    _run(_cache_tg_stats())


async def _cache_tg_stats() -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        overview = await repo.tg_overview()
        if overview["messages"] == 0:
            console.print("[yellow]No Telegram messages cached.[/] Run `unread sync` to populate.")
            return
        oldest = str(overview["oldest"] or "")[:10] or "—"
        newest = str(overview["newest"] or "")[:10] or "—"
        console.print(
            f"[bold]messages[/]      [red]{overview['messages']:,}[/] row(s)\n"
            f"[bold]chats[/]         {overview['chats']:,}\n"
            f"[bold]with text[/]     {overview['with_text']:,}\n"
            f"[bold]transcripts[/]   {overview['with_transcript']:,}\n"
            f"[bold]oldest[/]        {oldest}\n"
            f"[bold]newest[/]        {newest}"
        )


@cache_tg_app.command("show")
def cache_tg_show_cmd(
    chat_id: int = typer.Argument(..., help="Numeric chat id (use `tg describe` to look up)."),
) -> None:
    """Print one chat's cached-message stats: counts, age range, first / last."""
    _run(_cache_tg_show(chat_id))


async def _cache_tg_show(chat_id: int) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        chat = await repo.get_chat(chat_id)
        title = (chat or {}).get("title") or ""
        stats = await repo.chat_stats(chat_id)
        if stats.get("count", 0) == 0:
            console.print(f"[yellow]No cached messages for chat {chat_id}[/]")
            return
        console.print(f"[bold]chat_id[/]      {chat_id}" + (f"  [grey70]({title})[/]" if title else ""))
        for k in ("count", "first_msg_id", "last_msg_id", "first_date", "last_date"):
            v = stats.get(k)
            if v is None:
                continue
            console.print(f"[bold]{k:12}[/] {v}")


@cache_tg_app.command("export")
def cache_tg_export_cmd(
    output: Path = typer.Option(..., "--output", "-o", help="Output path (JSONL)."),
    chat_id: int | None = typer.Option(None, "--chat", help="Restrict to a single chat id."),
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Only export messages older than Nd / Nw (handy for archiving before `cache tg purge`).",
    ),
) -> None:
    """Dump cached Telegram messages to JSONL.

    One JSON object per line, columns from the `messages` table. For
    user-facing report exports use `unread dump <ref>` instead — this
    command targets backup / migration / inspection of the raw cache
    rows themselves.
    """
    _run(_cache_tg_export(output, chat_id, older_than))


async def _cache_tg_export(output: Path, chat_id: int | None, older_than: str | None) -> None:
    import json as _json
    from datetime import UTC, datetime, timedelta

    settings = get_settings()
    until_dt: datetime | None = None
    if older_than:
        days = _parse_duration_days(older_than)
        if days <= 0:
            console.print(f"[yellow]{_t('cli_skipped_label')}[/] {_t('cli_cache_purge_min_days')}")
            return
        until_dt = datetime.now(UTC) - timedelta(days=days)

    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with open_repo(settings.storage.data_path) as repo:
        # iter_messages requires a chat_id; for cross-chat export, walk
        # chats from the cache summary and stream each one's rows.
        chats: list[int] = []
        if chat_id is not None:
            chats = [chat_id]
        else:
            chats = [r["chat_id"] for r in await repo.tg_chats_summary(limit=100_000)]

        with output.open("w", encoding="utf-8") as f:
            for cid in chats:
                async for msg in repo.iter_messages(cid, until=until_dt):
                    payload = {
                        "chat_id": msg.chat_id,
                        "msg_id": msg.msg_id,
                        "thread_id": msg.thread_id,
                        "date": msg.date.isoformat() if msg.date else None,
                        "sender_id": msg.sender_id,
                        "sender_name": msg.sender_name,
                        "text": msg.text,
                        "transcript": getattr(msg, "transcript", None),
                        "media_type": getattr(msg, "media_type", None),
                        "fwd_from": getattr(msg, "fwd_from", None),
                    }
                    f.write(_json.dumps(payload, ensure_ascii=False, default=str) + "\n")
                    written += 1
    console.print(f"[green]Wrote[/] {written:,} message(s) to {output}.")


@cache_ai_app.command("stats")
def cache_ai_stats_cmd(
    since: str | None = typer.Option(
        None,
        "--since",
        help="Lower bound for the prompt-cache hit-rate breakdown: YYYY-MM-DD. Default: all-time.",
    ),
) -> None:
    """Show analysis cache size, age range, per-(preset, model) breakdown, and OpenAI prompt-cache hit rate."""
    _run(_cache_stats(since))


async def _cache_stats(since: str | None) -> None:
    from rich.table import Table

    settings = get_settings()
    since_dt = parse_ymd(since) if since else None
    async with open_repo(settings.storage.data_path) as repo:
        s = await repo.cache_stats()
        eff_rows = await repo.cache_effectiveness(since=since_dt)
    if s["rows"] == 0:
        console.print(f"[yellow]{_t('cli_cache_empty')}[/]")
    else:
        summary = _tf(
            "cli_cache_summary",
            rows=s["rows"],
            size=_fmt_bytes(s["result_bytes"]),
            saved=f"{s['saved_cost_usd']:.4f}",
            oldest=s["oldest"],
            newest=s["newest"],
        )
        console.print(f"[bold]analysis_cache[/] — {summary}")
        t = Table(title=_t("cli_cache_by_group_title"), show_lines=False)
        t.add_column(_t("cli_cache_col_preset"))
        t.add_column(_t("cli_cache_col_model"))
        t.add_column(_t("cli_cache_col_rows"), justify="right")
        t.add_column(_t("cli_cache_col_size"), justify="right")
        t.add_column(_t("cli_cache_col_saved"), justify="right")
        for r in s["by_group"]:
            t.add_row(
                str(r["preset"]),
                str(r["model"]),
                str(r["rows"]),
                _fmt_bytes(int(r["result_bytes"])),
                f"${float(r['saved_cost_usd']):.4f}",
            )
        console.print(t)

    if not eff_rows:
        console.print(f"[yellow]{_t('cli_no_usage_label')}[/] — {_t('cli_no_usage_hint')}")
        return
    since_suffix = _tf("cli_cache_eff_since", date=since) if since else ""
    t = Table(title=_tf("cli_cache_eff_title", since=since_suffix))
    t.add_column(_t("cli_cache_col_chat_id"))
    t.add_column(_t("cli_cache_col_preset"))
    t.add_column(_t("cli_cache_col_calls"), justify="right")
    t.add_column(_t("cli_cache_col_hit_calls"), justify="right")
    t.add_column(_t("cli_cache_col_hit_rate"), justify="right")
    t.add_column(_t("cli_cache_col_prompt_tok"), justify="right")
    t.add_column(_t("cli_cache_col_cached_tok"), justify="right")
    t.add_column(_t("cli_cache_col_cost"), justify="right")
    for r in eff_rows:
        prompt_tok = int(r["prompt_tokens"] or 0)
        cached_tok = int(r["cached_tokens"] or 0)
        rate_pct = (100.0 * cached_tok / prompt_tok) if prompt_tok else 0.0
        t.add_row(
            str(r["chat_id"]),
            str(r["preset"]),
            str(r["total_calls"]),
            str(r["hit_calls"]),
            f"{rate_pct:.1f}%",
            f"{prompt_tok:,}",
            f"{cached_tok:,}",
            f"${float(r['cost_usd']):.4f}",
        )
    console.print(t)
    console.print(f"[grey70]{_t('cli_cache_eff_hint')}[/]")


@cache_ai_app.command("ls")
def cache_ai_ls_cmd(
    preset: str | None = typer.Option(None, "--preset", help="Filter by preset name."),
    model: str | None = typer.Option(None, "--model", help="Filter by model name."),
    older_than: str | None = typer.Option(None, "--older-than", help="Nd / Nw"),
    limit: int = typer.Option(50, "--limit", help="Max rows to return."),
) -> None:
    """List cache entries (newest first). No result body — use `show` for that."""
    _run(_cache_ls(preset, model, older_than, limit))


async def _cache_ls(
    preset: str | None,
    model: str | None,
    older_than: str | None,
    limit: int,
) -> None:
    from rich.table import Table

    settings = get_settings()
    days = _parse_duration_days(older_than) if older_than else None
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.cache_list(preset=preset, model=model, older_than_days=days, limit=limit)
    if not rows:
        console.print(f"[yellow]{_t('cli_cache_no_matches')}[/]")
        return
    t = Table(show_lines=False)
    t.add_column(_t("cli_cache_col_hash"))
    t.add_column(_t("cli_cache_col_preset"))
    t.add_column(_t("cli_cache_col_model"))
    t.add_column(_t("cli_cache_col_ver"))
    t.add_column(_t("cli_cache_col_size"), justify="right")
    t.add_column(_t("cli_cache_col_cost_short"), justify="right")
    t.add_column(_t("cli_cache_col_created_at"))
    for r in rows:
        t.add_row(
            str(r["batch_hash"])[:10],
            str(r["preset"]),
            str(r["model"]),
            str(r["prompt_version"]),
            _fmt_bytes(int(r["result_bytes"] or 0)),
            f"${float(r['cost_usd'] or 0):.4f}",
            str(r["created_at"]),
        )
    console.print(t)


@cache_ai_app.command("show")
def cache_ai_show_cmd(
    batch_hash: str = typer.Argument(..., help="Full hash or unique prefix."),
) -> None:
    """Print a stored analysis result."""
    _run(_cache_show(batch_hash))


async def _cache_show(batch_hash: str) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        row = await repo.cache_get(batch_hash)
        if row is None:
            # Prefix match fallback — unique prefix only.
            matches = [
                r for r in await repo.cache_list(limit=10_000) if str(r["batch_hash"]).startswith(batch_hash)
            ]
            if len(matches) == 0:
                console.print(f"[red]{_t('cli_cache_no_entry_label')}[/] {batch_hash}.")
                raise typer.Exit(1)
            if len(matches) > 1:
                console.print(
                    f"[red]{_t('cli_cache_ambiguous_label')}[/] — "
                    f"{_tf('cli_cache_ambiguous_msg', n=len(matches))}"
                )
                raise typer.Exit(2)
            row = await repo.cache_get(matches[0]["batch_hash"])
            assert row is not None
    console.print(
        f"[bold]{row['batch_hash']}[/]  preset={row['preset']}  model={row['model']}  "
        f"ver={row['prompt_version']}  cost=${float(row['cost_usd'] or 0):.4f}  "
        f"created={row['created_at']}\n"
    )
    console.print(row["result"])


@cache_ai_app.command("export")
def cache_ai_export_cmd(
    output: Path = typer.Option(
        ..., "--output", "-o", help="File path. Extension picks format if --format omitted."
    ),
    fmt: str | None = typer.Option(None, "--format", help="jsonl | md"),
    preset: str | None = typer.Option(None, "--preset", help="Filter by preset name."),
    model: str | None = typer.Option(None, "--model", help="Filter by model name."),
    older_than: str | None = typer.Option(None, "--older-than", help="Export entries OLDER than this age."),
) -> None:
    """Export cached analyses to jsonl or md before (optionally) purging."""
    _run(_cache_export(output, fmt, preset, model, older_than))


async def _cache_export(
    output: Path,
    fmt: str | None,
    preset: str | None,
    model: str | None,
    older_than: str | None,
) -> None:
    import json

    if fmt is None:
        suffix = output.suffix.lower().lstrip(".")
        fmt = suffix if suffix in {"jsonl", "md"} else "jsonl"
    if fmt not in {"jsonl", "md"}:
        console.print(f"[red]{_t('cli_unknown_format_label')}[/] {_tf('cli_unknown_format_msg', fmt=fmt)}")
        raise typer.Exit(2)

    settings = get_settings()
    days = _parse_duration_days(older_than) if older_than else None
    async with open_repo(settings.storage.data_path) as repo:
        # cache_iter_full streams to keep large result blobs out of one
        # giant list; export wants the full list for its empty-check +
        # double iteration, so materialize here.
        rows = [r async for r in repo.cache_iter_full(preset=preset, model=model, older_than_days=days)]

    if not rows:
        console.print(f"[yellow]{_t('cli_export_no_matches')}[/]")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with output.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    else:  # md
        with output.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(
                    f"## {r['batch_hash']}\n\n"
                    f"- preset: `{r['preset']}`\n"
                    f"- model: `{r['model']}`\n"
                    f"- prompt_version: `{r['prompt_version']}`\n"
                    f"- cost_usd: {r['cost_usd']}\n"
                    f"- created_at: {r['created_at']}\n\n"
                    f"{r['result']}\n\n---\n\n"
                )
    console.print(
        f"[green]{_t('cli_wrote_label')}[/] "
        f"{_tf('cli_export_wrote_msg', n=len(rows), path=str(output), fmt=fmt)}"
    )


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def _parse_duration_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    if s.endswith("w"):
        return int(s[:-1]) * 7
    return int(s)


@cache_tg_app.command("purge")
def cache_tg_purge(
    retention: str | None = typer.Option(
        None,
        "--retention",
        help="Age threshold (Nd / Nw, e.g. 30d, 12w). Telegram messages older than this get their text blanked. Default: 90d. Mutually exclusive with --all.",
    ),
    chat: int | None = typer.Option(
        None,
        "--chat",
        help="Numeric chat id to scope the cleanup to. Mutually exclusive with --all.",
    ),
    all_chats: bool = typer.Option(
        False,
        "--all",
        help="Redact every synced message in every chat regardless of age. Mutually exclusive with --retention / --chat.",
    ),
    keep_transcripts: bool = typer.Option(
        True,
        "--keep-transcripts/--no-keep-transcripts",
        help="Keep voice/video transcripts and image/document descriptions. "
        "`--no-keep-transcripts` blanks those too — irreversible.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Blank out old Telegram message texts; transcripts and analysis cache stay.

    The Telegram-message cache (`messages` table) holds the message
    bodies your sync flow pulled in. This command nulls out the `text`
    column for messages older than `--retention`, keeping the row +
    msg_id + sender + reactions intact (so `analysis_cache` rows that
    cite those msg_ids still resolve to a valid row, just without the
    original text). Voice / video / image / doc enrichments survive
    unless you pass `--no-keep-transcripts`.

    Renamed from `unread cleanup` — sits under `cache` now alongside
    `cache purge` (analysis cache), `cache sources` (per-source caches),
    and the rest.
    """
    if all_chats and chat is not None:
        raise typer.BadParameter("--all and --chat are mutually exclusive.")
    if all_chats and retention is not None:
        raise typer.BadParameter(
            "--all redacts every message regardless of age — drop --retention or drop --all."
        )
    effective_retention = retention if retention is not None else "90d"
    _run(_cleanup(effective_retention, chat, keep_transcripts, yes, all_messages=all_chats))


async def _cleanup(
    retention: str,
    chat: int | None,
    keep_transcripts: bool,
    yes: bool,
    *,
    all_messages: bool = False,
) -> None:
    settings = get_settings()
    days = 0 if all_messages else _parse_duration_days(retention)
    async with open_repo(settings.storage.data_path) as repo:
        preview = await repo.count_redactable_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
            all_messages=all_messages,
        )
        if preview["to_redact"] == 0:
            if preview["messages"] == 0:
                if all_messages:
                    console.print(
                        f"[yellow]{_t('cli_cleanup_nothing')}[/] {_t('cli_cleanup_no_messages_at_all')}"
                    )
                else:
                    console.print(
                        f"[yellow]{_t('cli_cleanup_nothing')}[/] {_tf('cli_cleanup_older_than', days=days)}"
                    )
            else:
                tail = _t("cli_cleanup_transcripts_kept") if keep_transcripts else ""
                if all_messages:
                    console.print(
                        f"[yellow]{_t('cli_cleanup_already_clean_label')}[/] — "
                        f"{_tf('cli_cleanup_already_clean_msg_all', n=preview['messages'], tail=tail)}"
                    )
                else:
                    console.print(
                        f"[yellow]{_t('cli_cleanup_already_clean_label')}[/] — "
                        f"{_tf('cli_cleanup_already_clean_msg', n=preview['messages'], days=days, tail=tail)}"
                    )
            return

        scope = (
            _tf("cli_cleanup_preview_scope_chat", chat=chat)
            if chat is not None
            else _t("cli_cleanup_preview_scope_all")
        )
        transcript_line = (
            f"0 [grey70]{_t('cli_cleanup_kept_label')}[/]"
            if keep_transcripts
            else str(preview["with_transcript"])
        )
        body = _tf(
            "cli_cleanup_preview_lines",
            messages=preview["messages"],
            to_redact=preview["to_redact"],
            with_text=preview["with_text"],
            transcripts=transcript_line,
        )
        age_clause = (
            _t("cli_cleanup_age_all")
            if all_messages
            else _tf("cli_cleanup_older_than", days=days).rstrip(".")
        )
        console.print(f"[bold]{_t('cli_cleanup_preview_title')}[/] ({scope}, {age_clause}):\n{body}")

        # Per-chat breakdown of what's about to disappear. The total preview
        # above is a count-only summary; the breakdown turns "20k rows" into
        # "and here are the chats those rows live in," which is what the user
        # actually needs to decide whether to proceed. Skip when the run is
        # already scoped to one chat (the breakdown would be a single line
        # repeating what `scope` already says).
        if chat is None:
            breakdown = await repo.redactable_breakdown(
                retention_days=days,
                chat_id=chat,
                keep_transcripts=keep_transcripts,
                all_messages=all_messages,
                limit=10,
            )
            if breakdown:
                console.print(f"\n[bold]{_t('cli_cleanup_breakdown_title')}[/]")
                for row in breakdown:
                    label = row["title"] or _tf("cli_cleanup_breakdown_no_title", chat_id=row["chat_id"])
                    span = ""
                    if row["oldest"] and row["newest"]:
                        oldest = str(row["oldest"])[:10]
                        newest = str(row["newest"])[:10]
                        span = f" [grey70]({oldest} → {newest})[/]"
                    console.print(f"  • {label} — [red]{row['rows']:,}[/] rows{span}")
                shown = sum(r["rows"] for r in breakdown)
                remaining = max(0, preview["to_redact"] - shown)
                if remaining > 0:
                    console.print(f"  [grey70]{_tf('cli_cleanup_breakdown_more', n=remaining)}[/]")

        if not yes:
            from unread.util.prompt import confirm as _confirm

            if not _confirm(_t("cli_cleanup_proceed_q"), default=False):
                console.print(f"[yellow]{_t('cli_aborted')}[/]")
                return

        redacted = await repo.redact_old_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
            all_messages=all_messages,
        )
        tail = _t("cli_redacted_transcripts_kept") if keep_transcripts else ""
        if all_messages:
            console.print(
                f"[green]{_t('cli_redacted_label')}[/] {_tf('cli_redacted_msg_all', n=redacted, tail=tail)}"
            )
        else:
            console.print(
                f"[green]{_t('cli_redacted_label')}[/] "
                f"{_tf('cli_redacted_msg', n=redacted, days=days, tail=tail)}"
            )


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_ask"))
def ask(
    ref: str | None = typer.Argument(
        None,
        autocompletion=_complete_ref,
        help=(
            "Chat reference: @user, t.me link (incl. topic links like "
            "t.me/c/<id>/<topic>), fuzzy title, or numeric id. Pass `tg` "
            "to open the interactive chat picker. Mutually exclusive with "
            "--chat / --folder / --global."
        ),
    ),
    question: str | None = typer.Argument(
        None,
        help=(
            "Free-form question, in any language. Omit when ref is `tg` and the wizard will prompt for it."
        ),
    ),
    chat: str | None = typer.Option(
        None,
        "--chat",
        help="Restrict search to one chat (@user / link / fuzzy title / numeric id).",
    ),
    thread: int | None = typer.Option(
        None,
        "--thread",
        help="Forum-topic id (only meaningful with --chat).",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help="Restrict search to chats in this Telegram folder (case-insensitive substring).",
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(
        None,
        "--last-days",
        help="Restrict to messages from the last N days. Mutually exclusive with --since/--until.",
    ),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help=(
            "Restrict to messages newer than N hours ago. Mutually "
            "exclusive with --since/--until; if combined with "
            "--last-days, --last-hours wins (more specific)."
        ),
    ),
    last_minutes: int | None = typer.Option(
        None,
        "--last-minutes",
        help=(
            "Restrict to messages newer than N minutes ago. Mutually "
            "exclusive with --since/--until; wins over --last-hours / "
            "--last-days when combined (more specific)."
        ),
    ),
    limit: int = typer.Option(
        200,
        "--limit",
        help="Max messages to retrieve. Higher = better recall, more cost.",
    ),
    model: str | None = typer.Option(None, "--model", help="Override the answering model."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Save the answer to a file (markdown). Without --output the answer prints to terminal.",
    ),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Force terminal rendering even when --output is also set.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help=(
            "Pull new messages from Telegram (incremental from each chat's local "
            "max msg_id) before retrieval. Requires --chat or --folder."
        ),
    ),
    show_retrieved: bool = typer.Option(
        False,
        "--show-retrieved",
        help="Print the retrieved messages with their scores before the LLM call (debug).",
    ),
    rerank: bool | None = typer.Option(
        None,
        "--rerank/--no-rerank",
        help=(
            "Two-stage retrieval: keyword pool → cheap-model rerank → flagship answer. "
            "Default from [ask].rerank_enabled in config (true). Saves ~5-10× per question "
            "on media-heavy chats by feeding the flagship a smaller, better-ranked set."
        ),
    ),
    global_scope: bool = typer.Option(
        False,
        "--global",
        "-g",
        help=(
            "Search every synced chat in the local DB (no Telegram round-trips, "
            "no wizard). The previous default of `unread ask Q` (no scope) — now "
            "moved here so the new default opens the wizard."
        ),
    ),
    no_followup: bool = typer.Option(
        False,
        "--no-followup",
        help=(
            "Skip the post-answer 'Continue chatting?' prompt. Use in scripts / "
            "cron / non-interactive contexts."
        ),
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help=(
            "Use OpenAI-embeddings retrieval (cosine over a precomputed index) "
            "instead of keyword LIKE. Run `--build-index` first per chat/folder. "
            "Catches paraphrase ('the DB' → migration discussion) that keyword misses."
        ),
    ),
    build_index: bool = typer.Option(
        False,
        "--build-index",
        help=(
            "Embed every not-yet-indexed message in the scoped chat(s) and exit. "
            "Idempotent — re-runs only fill gaps. Required once per chat before "
            "`--semantic`. Cheap: ~$0.02 per 1M tokens at text-embedding-3-small."
        ),
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help=(
            "Abort if the estimated USD cost exceeds N. The estimate counts the "
            "exact prompt tokens (no _AVG_TOKENS_PER_MSG rounding) so it tracks "
            "media-heavy chats. Pass with --yes to abort silently."
        ),
    ),
    with_comments: bool = typer.Option(
        False,
        "--with-comments",
        help=(
            "When --chat is a channel: also retrieve from its linked "
            "discussion group (comments). Both ranges of messages share "
            "the answer. No-op when scope is global, a folder, or a "
            "non-channel chat."
        ),
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Comma-separated media enrichments to run BEFORE retrieval: "
            "voice, videonote, video, image, doc, link. "
            "Overrides config defaults for this run. "
            "Example: --enrich=voice,image,link"
        ),
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Enable every enrichment (voice/videonote/video/image/doc/link) before retrieval. Spendy.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Disable all enrichments for this run, even those that would default on.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the over-budget confirmation prompt (combined with --max-cost).",
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help=(
            "UI language (en, ru, …). Drives wizard labels and status messages. "
            "Defaults to [locale] language in config."
        ),
    ),
    report_language: str | None = typer.Option(
        None,
        "--report-language",
        help=(
            "Language the LLM writes the answer in (en, ru, …). Drives the "
            "system prompt + labels sent to the model. Defaults to "
            "[locale] report_language, falling back to --language."
        ),
    ),
    source_language: str | None = typer.Option(
        None,
        "--content-language",
        help=(
            "Source-content language hint (en, ru, zh, …). Whisper-style "
            "override — empty = LLM auto-detects from the cited messages. "
            "Defaults to [locale] content_language."
        ),
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help=(
            "Advance Telegram's read marker after the answer. Only meaningful "
            "with a single-chat scope (positional <ref> or --chat); silent "
            "no-op for --folder / --global. Default: don't mark."
        ),
    ),
) -> None:
    """Answer a question about your synced Telegram archive.

    Positional order: ref first, then question. Pass `tg` as the ref to
    open the interactive chat picker (the wizard prompts for the question
    if you didn't supply one). Without a ref or a scope flag, ask refuses
    to guess — same rule as `unread <ref>` and `unread dump <ref>`.

    Examples:
      unread ask @somegroup "what did Bob say about migration?"
      unread ask https://t.me/c/3865481227/4 "open Qs?"          # incl. topic
      unread ask tg                                              # picker + prompts
      unread ask tg "what did Bob say?"                          # picker, question pre-filled
      unread ask --folder Work "..." --last-days 7
      unread ask --global "..."                                  # all synced, no wizard
    """
    language, report_language, source_language = _validate_lang_flags(
        language, report_language, source_language
    )
    # Pre-TG dispatch: file / YouTube / website refs route to dedicated
    # adapters (mirrors cmd_dump's shape) so non-TG sources never trigger
    # a Telegram session open. The detection helpers (_looks_like_local_file,
    # is_youtube_url, is_website_url) are the same ones cmd_dump uses, so
    # all three top-level ref-takers (analyze, dump, ask) recognize the
    # same set of ref shapes.

    # Stdin normalization — match the analyze entry point's behavior:
    # `unread ask -` and bare `unread ask "Q"` with piped stdin both route
    # through the file dispatcher with the <stdin> sentinel ref. Skipped
    # when the user already specified a scope (--chat/--folder/--global)
    # or asked for the picker.
    no_scope_set = chat is None and folder is None and not global_scope
    if ref == "-" or (ref is None and no_scope_set and _stdin_has_data()):
        ref = _STDIN_REF_SENTINEL

    # Hard-reject doc-shaped refs combined with TG-only scope flags.
    # `cmd_dump` does the same for its YouTube/website branches via
    # `telegram_only_flags`; for ask we centralize the check on the
    # entry guard.
    if ref and ref != TG_INTERACTIVE_REF:
        from unread.website.urls import is_telegram_url as _is_tg_url_pre
        from unread.website.urls import is_website_url as _is_web_pre
        from unread.youtube.urls import is_youtube_url as _is_yt_pre

        is_doc_ref = (
            ref == _STDIN_REF_SENTINEL
            or _looks_like_local_file(ref)
            or _is_yt_pre(ref)
            or (_is_web_pre(ref) and not _is_tg_url_pre(ref))
        )
        if is_doc_ref and (chat is not None or folder is not None or global_scope):
            conflicting = []
            if chat is not None:
                conflicting.append("--chat")
            if folder is not None:
                conflicting.append("--folder")
            if global_scope:
                conflicting.append("--global")
            raise typer.BadParameter(
                f"Cannot combine a doc ref ({ref!r}) with {', '.join(conflicting)}. "
                "A doc ref already names the source; pick one scope."
            )

        if ref == _STDIN_REF_SENTINEL or _looks_like_local_file(ref):
            from unread.ask.sources.file import cmd_ask_file

            _run(
                cmd_ask_file(
                    ref,
                    question,
                    model=model,
                    output=output,
                    console_out=console_out,
                    max_cost=max_cost,
                    yes=yes,
                    language=language,
                    report_language=report_language,
                    source_language=source_language,
                    no_followup=no_followup,
                    semantic=semantic,
                    build_index=build_index,
                    rerank=rerank,
                    limit=limit,
                    show_retrieved=show_retrieved,
                )
            )
            return
        from unread.youtube.urls import is_youtube_url

        if is_youtube_url(ref):
            from unread.ask.sources.youtube import cmd_ask_youtube

            _run(
                cmd_ask_youtube(
                    ref,
                    question,
                    model=model,
                    output=output,
                    console_out=console_out,
                    max_cost=max_cost,
                    yes=yes,
                    language=language,
                    report_language=report_language,
                    source_language=source_language,
                    no_followup=no_followup,
                    semantic=semantic,
                    build_index=build_index,
                    rerank=rerank,
                    limit=limit,
                    show_retrieved=show_retrieved,
                )
            )
            return
        from unread.website.urls import is_telegram_url, is_website_url

        if is_website_url(ref) and not is_telegram_url(ref):
            from unread.ask.sources.website import cmd_ask_website

            _run(
                cmd_ask_website(
                    ref,
                    question,
                    model=model,
                    output=output,
                    console_out=console_out,
                    max_cost=max_cost,
                    yes=yes,
                    language=language,
                    report_language=report_language,
                    source_language=source_language,
                    no_followup=no_followup,
                    semantic=semantic,
                    build_index=build_index,
                    rerank=rerank,
                    limit=limit,
                    show_retrieved=show_retrieved,
                )
            )
            return

    # Fall through to the Telegram-archive ask (existing path).
    from unread.ask.commands import cmd_ask

    _run(
        cmd_ask(
            question=question,
            ref=ref,
            chat=chat,
            thread=thread,
            folder=folder,
            global_scope=global_scope,
            since=since,
            until=until,
            last_days=last_days,
            last_hours=last_hours,
            last_minutes=last_minutes,
            limit=limit,
            model=model,
            output=output,
            console_out=console_out,
            refresh=refresh,
            show_retrieved=show_retrieved,
            rerank=rerank,
            no_followup=no_followup,
            semantic=semantic,
            build_index=build_index,
            max_cost=max_cost,
            with_comments=with_comments,
            enrich=enrich,
            enrich_all=enrich_all,
            no_enrich=no_enrich,
            yes=yes,
            language=language,
            report_language=report_language,
            source_language=source_language,
            mark_read=mark_read,
        )
    )


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_prompt"))
def prompt(
    text: str = typer.Argument(
        ...,
        help="Free-form prompt sent straight to the configured AI provider.",
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the answering model."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Save answer to a markdown file (default: render to terminal).",
    ),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Force terminal rendering even when --output is also set.",
    ),
    report_language: str | None = typer.Option(
        None,
        "--report-language",
        help=(
            "Answer language hint (en, ru, …). Becomes a one-line `Respond in <lang>.` "
            "system message. Defaults to [locale] report_language; empty = LLM auto-detects."
        ),
    ),
    max_tokens: int = typer.Option(
        2000,
        "--max-tokens",
        help="Cap output tokens (the orchestrator may auto-retry on truncation).",
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="Abort if the estimated USD cost exceeds N.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the over-budget confirmation prompt (combined with --max-cost).",
    ),
) -> None:
    """Send a plain prompt to the configured AI — no retrieval, no archive, no Telegram.

    The only context attached is an optional one-line `Respond in <lang>.`
    system message driven by --report-language (or [locale] report_language).
    Cost flows through the same usage_log as analyze/ask under
    `phase=prompt`, so `unread stats --by kind` will list it.

    Examples:
      unread prompt "say hi in one word"
      unread prompt --report-language ru "what is 2+2"
      unread prompt -o /tmp/p.md "explain CRDT in 2 lines"
    """
    _, report_language, _ = _validate_lang_flags(None, report_language, None)
    from unread.ai.prompt import cmd_prompt

    _run(
        cmd_prompt(
            prompt=text,
            model=model,
            output=output,
            console_out=console_out,
            report_language=report_language,
            max_tokens=max_tokens,
            max_cost=max_cost,
            yes=yes,
        )
    )


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_settings"))
def settings() -> None:
    """Open the interactive settings editor.

    Single panel covering every persistable override: languages,
    models, enrichment defaults, analysis tuning. "Show effective" and
    "Reset all overrides" live as rows inside the menu — no separate
    sub-commands.
    """
    from unread.settings.commands import cmd_settings

    _run(cmd_settings())


reports_app = _UnreadTyper(help=_t("cmd_reports"), no_args_is_help=True, cls=_UnreadGroup)
app.add_typer(reports_app, name="reports", rich_help_panel=PANEL_MAINT)


# `unread security ...` — credential-store inspection / migration.
# Registered here (not in a stub command body) because Typer needs the
# subapp constructed at module-load time so `unread --help` lists it.
from unread.security.commands import register as _register_security_commands  # noqa: E402

_register_security_commands(app, PANEL_MAINT)


# `unread completion ...` — shell tab-completion install / show.
# Wraps Typer's `--install-completion` / `--show-completion` flags as
# a subcommand group so they don't show up on the root callback's
# flag list (`unread help flags`).
from unread.completion.commands import register as _register_completion_commands  # noqa: E402

_register_completion_commands(app, PANEL_MAINT)


@reports_app.command("prune")
def reports_prune(
    older_than: str = typer.Option("30d", "--older-than", help="Nd / Nw"),
    root: Path | None = typer.Option(
        None,
        "--root",
        help="Reports root directory (default: ~/.unread/reports).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be pruned, take no action."),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Hard-delete instead of moving to <root>/.trash/<ts>/. Irreversible.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Move (or delete) report files older than --older-than to <root>/.trash/.

    Default behavior: trash them by moving to `<root>/.trash/<ts>/`. The
    `.trash/` subtree is itself ignored when scanning. Run with `--purge`
    to hard-delete (after confirmation, unless `--yes`).
    """
    from unread.core.paths import reports_dir

    resolved_root = root if root is not None else reports_dir()
    _run(_reports_prune(older_than, resolved_root, dry_run, purge, yes))


async def _reports_prune(
    older_than: str,
    root: Path,
    dry_run: bool,
    purge: bool,
    yes: bool,
) -> None:
    import shutil
    import time

    days = _parse_duration_days(older_than)
    if days <= 0:
        console.print(f"[yellow]{_t('cli_skipped_label')}[/] {_t('cli_prune_min_days')}")
        return
    if not root.exists():
        console.print(
            f"[yellow]{_t('cli_prune_no_root_label')}[/] {_tf('cli_prune_no_root_msg', path=str(root))}"
        )
        return
    cutoff = time.time() - days * 86400
    trash_root = root / ".trash"
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Don't prune the trash, the slash, or hidden dotfiles inside the
        # tree (e.g. .gitkeep — the user may want those preserved).
        if trash_root in p.parents or p.name.startswith("."):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                candidates.append(p)
        except OSError:
            continue
    if not candidates:
        console.print(f"[grey70]{_tf('cli_prune_nothing_old', days=days, root=str(root))}[/]")
        return
    total_bytes = sum(p.stat().st_size for p in candidates if p.exists())
    verb = (
        _t("cli_prune_verb_would_delete")
        if dry_run and purge
        else (
            _t("cli_prune_verb_would_trash")
            if dry_run
            else (_t("cli_prune_verb_delete") if purge else _t("cli_prune_verb_trash"))
        )
    )
    console.print(
        f"[bold]{verb}[/] "
        f"{_tf('cli_prune_summary', n=len(candidates), size=_fmt_bytes(total_bytes), days=days, root=str(root))}"
    )
    for p in candidates[:20]:
        console.print(f"  {p.relative_to(root)}")
    if len(candidates) > 20:
        console.print(f"  [grey70]{_tf('cli_prune_and_more', n=len(candidates) - 20)}[/]")
    if dry_run:
        return
    if not yes:
        from unread.util.prompt import confirm as _confirm

        if not _confirm(_t("cli_prune_proceed_q"), default=False):
            console.print(f"[yellow]{_t('cli_aborted')}[/]")
            return
    if purge:
        for p in candidates:
            try:
                p.unlink()
            except OSError as e:
                console.print(f"[red]{_t('cli_prune_failed_delete_label')}[/] {p}: {e}")
        console.print(
            f"[green]{_t('cli_prune_deleted_label')}[/] {_tf('cli_prune_deleted_msg', n=len(candidates))}"
        )
        return
    # Trash mode: move to reports/.trash/<ts>/, preserving relative subtree.
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bin_dir = trash_root / stamp
    bin_dir.mkdir(parents=True, exist_ok=True)
    for p in candidates:
        rel = p.relative_to(root)
        target = bin_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(target))
        except OSError as e:
            console.print(f"[red]{_t('cli_prune_failed_move_label')}[/] {p}: {e}")
    console.print(
        f"[green]{_t('cli_prune_trashed_label')}[/] "
        f"{_tf('cli_prune_trashed_msg', n=len(candidates), path=str(bin_dir))}"
    )


@reports_app.command("ls")
def reports_ls(
    root: Path | None = typer.Option(
        None,
        "--root",
        help="Reports root directory (default: ~/.unread/reports).",
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Filter by top-level subfolder (e.g. youtube, website, files, chats).",
    ),
    limit: int = typer.Option(50, "--limit", help="Max rows to show. Default: 50."),
    all_rows: bool = typer.Option(False, "--all", help="Show every row (overrides --limit)."),
    oldest: bool = typer.Option(False, "--oldest", help="Sort oldest-first (default: newest-first)."),
) -> None:
    """List saved reports under the reports root.

    Walks `<root>` recursively, skipping `<root>/.trash/` and dotfiles.
    Newest-first by mtime. Each row gets a stable 8-char `id` (sha1 of
    its path-relative-to-root) — pass it directly to
    `unread reports show <id>` instead of typing the full slug. The
    relative path and any unique substring also work.
    """
    from unread.core.paths import reports_dir

    resolved_root = root if root is not None else reports_dir()
    _reports_ls(resolved_root, kind, limit, all_rows, oldest)


def _report_id(rel_path: Path) -> str:
    """Stable 8-char handle for a report, derived from its path-relative-to-root.

    Deterministic across runs and machines (same path → same id), so
    `ls` output and `show <id>` lookups stay in sync without persisting
    an index.
    """
    import hashlib

    return hashlib.sha1(str(rel_path).encode("utf-8")).hexdigest()[:8]


def _reports_ls(root: Path, kind: str | None, limit: int, all_rows: bool, oldest: bool) -> None:
    from rich.table import Table

    console.print(f"[bold]Reports root[/] [grey70]{root}[/]")
    if not root.exists():
        console.print("[yellow]Folder does not exist yet — run `unread <ref>` to produce a report.[/]")
        return

    base = root / kind if kind else root
    if kind and not base.is_dir():
        console.print(f"[yellow]No reports under `{kind}/` (looked at {base}).[/]")
        return

    files = _collect_report_files(root, base)
    if not files:
        console.print("[grey70](no reports yet)[/]")
        return

    files.sort(key=lambda p: p.stat().st_mtime, reverse=not oldest)
    total = len(files)
    shown_files = files if all_rows or limit <= 0 else files[:limit]

    table = Table(show_header=True, header_style="bold")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("modified", no_wrap=True)
    table.add_column("size", justify="right")
    table.add_column("path")
    for p in shown_files:
        st = p.stat()
        rel = p.relative_to(root)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        table.add_row(_report_id(rel), mtime, _fmt_bytes(st.st_size), str(rel))
    console.print(table)

    shown = len(shown_files)
    tail = " — pass `--all` to show every row" if shown < total else ""
    console.print(f"[grey70]Showing {shown:,} of {total:,} report(s){tail}[/]")


@reports_app.command("show")
def reports_show(
    path: str = typer.Argument(
        ...,
        help=(
            "8-char id from `reports ls`, absolute path, path relative to "
            "the reports root, or a unique filename substring."
        ),
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        help="Reports root directory (default: ~/.unread/reports).",
    ),
    raw: bool = typer.Option(False, "--raw", help="Print raw file contents instead of rendering markdown."),
) -> None:
    """Render a saved report to the terminal.

    The argument resolves in this order: 8-char id (as printed by
    `reports ls`) → absolute path → root-relative path → unique
    substring of any report's relative path. Ambiguous substrings (or
    the rare id collision) list the candidates and exit 2.
    """
    from unread.core.paths import reports_dir

    resolved_root = root if root is not None else reports_dir()
    _reports_show(path, resolved_root, raw)


def _reports_show(needle: str, root: Path, raw: bool) -> None:
    target = _resolve_report_path(needle, root)
    try:
        body = target.read_text(encoding="utf-8")
    except OSError as e:
        console.print(f"[red]Failed to read[/] {target}: {e}")
        raise typer.Exit(1) from e

    if raw or target.suffix.lower() != ".md":
        console.print(body, markup=False, highlight=False)
        return

    from rich.markdown import Markdown
    from rich.rule import Rule

    try:
        rel = target.relative_to(root)
        rid = f"[cyan]{_report_id(rel)}[/]  "
    except ValueError:
        rel = target  # absolute path outside the root — show as-is
        rid = ""
    console.print(Rule(str(rel), style="cyan"))
    console.print(Markdown(body))
    console.print(Rule(style="cyan"))
    console.print(f"{rid}[grey70]Path:[/] {target}")


_REPORT_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _resolve_report_path(needle: str, root: Path) -> Path:
    direct = Path(needle).expanduser()
    if direct.is_absolute() and direct.is_file():
        return direct
    rel_candidate = root / needle
    if rel_candidate.is_file():
        return rel_candidate

    if not root.exists():
        console.print(f"[red]Reports root does not exist:[/] {root}")
        raise typer.Exit(1)

    files = _collect_report_files(root, root)

    # Exact id match first — the cheapest, friendliest handle from `ls`.
    # An id collision (two paths hashing to the same 8 chars) is treated
    # as ambiguous so we never silently pick the wrong report.
    if _REPORT_ID_RE.match(needle):
        id_matches = [p for p in files if _report_id(p.relative_to(root)) == needle]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            _print_ambiguous(needle, id_matches, root, kind="id collision")
            raise typer.Exit(2)
        # No id hit — fall through to substring (could match a hex-y slug).

    matches = [p for p in files if needle in str(p.relative_to(root))]
    if not matches:
        console.print(f"[red]No report matches[/] {needle!r} under {root}.")
        raise typer.Exit(1)
    if len(matches) > 1:
        _print_ambiguous(needle, matches, root, kind="substring")
        raise typer.Exit(2)
    return matches[0]


def _print_ambiguous(needle: str, matches: list[Path], root: Path, *, kind: str) -> None:
    console.print(f"[yellow]Ambiguous {kind}:[/] {needle!r} matches {len(matches)} reports:")
    for p in matches[:20]:
        rel = p.relative_to(root)
        console.print(f"  [cyan]{_report_id(rel)}[/]  {rel}")
    if len(matches) > 20:
        console.print(f"  [grey70]… and {len(matches) - 20} more[/]")
    console.print("[grey70]Pass the full id or relative path to disambiguate.[/]")


def _collect_report_files(root: Path, base: Path) -> list[Path]:
    """List visible report files under `base`, skipping `<root>/.trash/` and dotfiles."""
    trash_root = root / ".trash"
    out: list[Path] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if trash_root in p.parents or p.name.startswith("."):
            continue
        out.append(p)
    return out


@app.command(
    rich_help_panel=PANEL_MAINT,
    help=_t("cmd_watch"),
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def watch(
    ctx: typer.Context,
    interval: str = typer.Option(
        "1h",
        "--interval",
        help="How often to fire the inner command. Accepts Ns / Nm / Nh / Nd / Nw.",
    ),
    max_runs: int | None = typer.Option(
        None,
        "--max-runs",
        help="Stop after N successful runs (handy for testing). None = run forever.",
    ),
) -> None:
    """Run an inner `unread` command on a fixed cadence.

    Walks the wall clock: runs the inner command, sleeps for the interval,
    repeats. Anything after `watch`'s own flags is forwarded verbatim as
    `unread <inner...>`, so any subcommand or root-level analyze ref works.

    Examples:
      unread watch --interval 1h tg chats run
      unread watch --interval 30m @news --preset action_items
      unread watch --interval 6h --max-runs 4 https://example.com/blog
      unread watch --interval 15m -- ask tg "anything urgent today?" --global

    Foreground only — wrap in `tmux` / `nohup`, or hand off to real
    cron / launchd / systemd, if you need persistence. Ctrl-C exits
    cleanly between iterations.

    The inner command runs in a fresh subprocess each time (so an internal
    crash doesn't poison subsequent runs); exit codes are surfaced but
    don't abort the loop unless `--max-runs` is hit.
    """
    inner = ctx.args
    if not inner:
        # No inner command → render this command's help. Mirrors the
        # `no_args_is_help=True` convention used by every sub-group;
        # printing usage is more useful than a one-line error.
        _print_help_for_command(ctx.command, ctx)
        raise typer.Exit(0)
    _run(_watch_loop(interval, max_runs, inner))


async def _watch_loop(interval: str, max_runs: int | None, inner: list[str]) -> None:
    import asyncio as _asyncio
    import os as _os
    import shlex
    import sys as _sys

    from unread.config import dotenv_values as _dotenv_values

    seconds = _parse_duration_seconds(interval)
    if seconds <= 0:
        console.print(f"[red]{_t('cli_watch_interval_positive')}[/]")
        raise typer.Exit(2)

    runs = 0
    cmd = ["unread", *inner]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    console.print(f"[bold cyan]{_tf('cli_watch_watching', interval=interval, cmd=pretty)}[/]")
    # Compose the child env: shell env wins, with the cached .env overlay
    # filling in missing keys. After `fix(config): isolate .env values
    # from os.environ`, the .env values no longer live on os.environ —
    # but the watched re-exec of `unread` still needs them, so we re-
    # union them here explicitly.
    dotenv_overlay = _dotenv_values()
    child_env = {**_os.environ, **{k: v for k, v in dotenv_overlay.items() if k not in _os.environ}}
    # Single Ctrl-C handler covers both phases (child wait / sleep).
    # asyncio.create_subprocess_exec inherits stdin so the child sees
    # SIGINT first; if it handles it cleanly, proc.wait() returns and
    # we just continue. If the user mashes Ctrl-C again during sleep,
    # it propagates as KeyboardInterrupt and we exit.
    try:
        while True:
            runs += 1
            console.print(
                f"\n[bold]{_tf('cli_watch_run_n', n=runs)}[/] "
                f"[grey70]{datetime.now().isoformat(timespec='seconds')}[/]"
            )
            try:
                # asyncio.create_subprocess_exec leaves the event loop
                # responsive while the child runs. The child shares our
                # stdio, so its output / Ctrl-C behavior matches the
                # previous blocking shape.
                proc = await _asyncio.create_subprocess_exec(*cmd, env=child_env)
                return_code = await proc.wait()
                if return_code != 0:
                    console.print(f"[yellow]{_tf('cli_watch_inner_exited', code=return_code)}[/]")
            except FileNotFoundError:
                console.print(f"[red]{_tf('cli_watch_not_on_path', cmd=cmd[0])}[/]")
                raise typer.Exit(2) from None
            if max_runs is not None and runs >= max_runs:
                console.print(f"[grey70]{_tf('cli_watch_max_runs_reached', n=max_runs)}[/]")
                return
            console.print(f"[grey70]{_tf('cli_watch_sleeping', interval=interval)}[/]")
            await _asyncio.sleep(seconds)
    except KeyboardInterrupt:
        console.print(f"\n[yellow]{_t('cli_watch_interrupted')}[/]")
    finally:
        _sys.stdout.flush()


def _parse_duration_seconds(s: str) -> int:
    """Parse `45s`/`5m`/`2h`/`3d`/`1w` into seconds. Raises on garbage."""
    s = s.strip().lower()
    if not s:
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units:
        try:
            return int(s[:-1]) * units[s[-1]]
        except ValueError as e:
            raise typer.BadParameter(_tf("cli_watch_invalid_duration", value=repr(s))) from e
    # Bare integer = seconds.
    try:
        return int(s)
    except ValueError as e:
        raise typer.BadParameter(f"Invalid duration: {s!r}") from e


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_doctor"))
def doctor() -> None:
    """Preflight check: Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk."""
    from unread.tg.commands import cmd_doctor

    _run(cmd_doctor())


@app.command("bug-report", rich_help_panel=PANEL_MAINT, help=_t("cmd_bug_report"))
def bug_report(
    output: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Write the bundle to this file instead of stdout.",
    ),
) -> None:
    """Print a redacted diagnostic bundle for GitHub issues.

    Bundles version, Python/platform, full doctor output, recent log
    lines, and config files with every secret value masked. Safe to
    paste into public issues.
    """
    from unread.diagnostics import build_bug_report

    async def _run_bug_report() -> None:
        text = await build_bug_report()
        if output is not None:
            output.write_text(text, encoding="utf-8")
            console.print(f"[green]Wrote bug report to {output}[/]")
        else:
            # Plain print (not console.print) — avoid Rich markup
            # interpretation on the doctor output / config text.
            sys.stdout.write(text)
            sys.stdout.flush()

    _run(_run_bug_report())


@app.command("update", rich_help_panel=PANEL_MAINT, help=_t("cmd_update"))
def update_cmd(
    check: bool = typer.Option(False, "--check", help=_t("cmd_update_check")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_t("cmd_update_yes")),
) -> None:
    """Check PyPI for a newer release and (optionally) install it."""
    from unread.update import cmd_update

    cmd_update(check=check, yes=yes)


@app.command(
    name="killme",
    rich_help_panel=PANEL_MAINT,
    help="Fully uninstall unread: data, credentials, and binary. Irreversible.",
)
def killme_cmd(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the 'killme' type-in confirmation. Use only in scripted teardowns.",
    ),
) -> None:
    """Permanently remove every artifact this CLI ever wrote.

    Lists exactly what will be deleted (install dir, OS-keychain
    secrets, cached encryption key, `uv tool`-installed binary), then
    asks you to type ``killme`` to confirm. Pass ``--yes`` to skip the
    type-in for scripted teardowns. Nothing goes to a trash — the
    deletes are immediate and final.
    """
    from unread.killme import cmd_killme

    code = cmd_killme(yes=yes)
    if code != 0:
        raise typer.Exit(code)


@backup_app.command("up", help=_t("cmd_backup_up"))
def backup_up(
    output: Path | None = typer.Argument(
        None,
        help="Destination file (default: storage/backups/data-YYYY-MM-DD_HHMMSS.sqlite).",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace the destination file if it already exists.",
    ),
) -> None:
    """Snapshot storage/data.sqlite to a single compact file (uses VACUUM INTO).

    Safe to run while unread is in the middle of a sync — SQLite makes the
    copy consistent without blocking the writer for more than a moment.
    Restore with `unread backup restore <file>`.

    What the backup contains depends on the active credential-storage backend
    (see `unread security status`):

      • plain     — backup includes plaintext API keys + api_id / api_hash.
                    Treat the file like a password; anyone with it has your keys.
      • keystore  — backup does NOT include credentials. They live in the OS
                    keychain, which the backup can't see. Restoring on the same
                    machine still works (keychain is local). Restoring on a
                    different machine boots into a "no credentials" state and
                    you must re-run `unread login` to add keys.
      • pass      — backup includes ciphertext + the install salt + the
                    encrypted Telegram session. Restoring anywhere is fine, but
                    you'll need the passphrase to use it.
    """
    _run(_backup(output, overwrite))


async def _backup(output: Path | None, overwrite: bool) -> None:
    settings = get_settings()
    src = settings.storage.data_path
    if not src.exists():
        console.print(f"[red]{_tf('cli_backup_no_db', path=str(src))}[/]")
        raise typer.Exit(1)
    if output is None:
        from unread.core.paths import default_backups_dir

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output = default_backups_dir() / f"data-{stamp}.sqlite"
    output = output.resolve()
    if output.exists():
        if not overwrite:
            console.print(f"[red]{_tf('cli_backup_already_exists', path=str(output))}[/]")
            raise typer.Exit(2)
        output.unlink()
    async with open_repo(src) as repo:
        size = await repo.backup_to(output)
    console.print(f"[green]{_t('cli_backup_done_label')}[/] {src} → {output} [grey70]({_fmt_bytes(size)})[/]")

    # Per-backend reminder so the user finds out about the
    # portability / sensitivity tradeoff at backup time, not when
    # they try to restore on another machine.
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        BACKEND_PASSPHRASE,
        read_active_backend_sync,
    )

    backend = read_active_backend_sync(src)
    if backend == BACKEND_KEYCHAIN:
        console.print(
            "[yellow]Note:[/] API keys live in your OS keychain — this backup does NOT "
            "include them. Restoring on a different machine will need "
            "[cyan]unread login[/] to re-add credentials."
        )
    elif backend == BACKEND_PASSPHRASE:
        console.print(
            "[grey70]Backup includes ciphertext + install salt; you'll need the "
            "passphrase to use it after restore.[/]"
        )
    else:
        console.print(
            "[yellow]Note:[/] API keys are stored in plaintext — treat this backup "
            "as sensitive. Run [cyan]unread security set keystore[/] to encrypt at rest."
        )


@backup_app.command("restore", help=_t("cmd_backup_restore"))
def backup_restore(
    backup_file: Path = typer.Argument(..., help="Path to a previously-created backup file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the destructive-action prompt."),
) -> None:
    """Replace storage/data.sqlite with a backup. The current DB is moved aside.

    The current DB is renamed to `data-replaced-YYYY-MM-DD_HHMMSS.sqlite`
    next to the original — undo by swapping the names back.
    """
    _run(_restore(backup_file, yes))


async def _restore(backup_file: Path, yes: bool) -> None:
    import shutil

    settings = get_settings()
    dst = settings.storage.data_path
    if not backup_file.exists():
        console.print(f"[red]{_t('cli_restore_not_found_label')}[/] {backup_file}")
        raise typer.Exit(2)
    if not yes:
        from unread.util.prompt import confirm as _confirm

        if not _confirm(
            _tf("cli_restore_confirm_q", dst=str(dst), src=str(backup_file)),
            default=False,
        ):
            console.print(f"[yellow]{_t('cli_aborted')}[/]")
            raise typer.Exit(0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        moved = dst.with_name(f"{dst.stem}-replaced-{stamp}{dst.suffix}")
        dst.rename(moved)
        console.print(f"[grey70]{_tf('cli_restore_moved_db', path=str(moved))}[/]")
    # Also clear -wal / -shm sidecars so the restored DB doesn't pick up
    # transactions from the replaced DB on next open.
    for sidecar in (dst.with_suffix(dst.suffix + "-wal"), dst.with_suffix(dst.suffix + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    shutil.copy2(backup_file, dst)
    console.print(f"[green]{_t('cli_restore_done_label')}[/] {backup_file} → {dst}")


@app.command(hidden=True)
def export(
    chat: int = typer.Option(..., "--chat", help="Numeric chat id to export (required)."),
    fmt: str = typer.Option("md", "--format", help="jsonl | csv | md"),
    output: Path = typer.Option(..., "--output", help="Destination file path (required)."),
    since: str | None = typer.Option(
        None, "--since", help="Lower bound (YYYY-MM-DD). Default: no lower bound."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Upper bound (YYYY-MM-DD). Default: no upper bound."
    ),
) -> None:
    """Export already-synced messages from the local DB to jsonl / csv / md."""
    from unread.export.commands import cmd_export

    _run(cmd_export(chat=chat, fmt=fmt, output=output, since=since, until=until))


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_dump"))
def dump(
    ref: str | None = typer.Argument(
        None,
        autocompletion=_complete_ref,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`unread dump -- -1001234567890 -o out.md`. Omit to pick every "
            "dialog with unread messages (interactive)."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file (single chat) or directory (no-ref mode).",
    ),
    fmt: str = typer.Option("md", "--format", help="md | jsonl | csv"),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days", help="Shortcut for --since now-N."),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help=(
            "Restrict to messages newer than N hours ago. Mutually "
            "exclusive with --since/--until/--full-history; if combined "
            "with --last-days, --last-hours wins (more specific)."
        ),
    ),
    last_minutes: int | None = typer.Option(
        None,
        "--last-minutes",
        help=(
            "Restrict to messages newer than N minutes ago. Mutually "
            "exclusive with --since/--until/--full-history; wins over "
            "--last-hours / --last-days when combined (more specific)."
        ),
    ),
    full_history: bool = typer.Option(False, "--full-history", help="Pull the whole chat."),
    thread: int | None = typer.Option(
        None,
        "--thread",
        help="Forum-topic id. Run `unread topics <ref>` first to list topic ids.",
    ),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Start at this msg_id (or a message link)."),
    join: bool = typer.Option(False, "--join", help="Join via invite link if required."),
    with_transcribe: bool = typer.Option(
        False, "--with-transcribe", help="Transcribe voice/videonote before export (OpenAI Audio)."
    ),
    include_transcripts: bool = typer.Option(
        True,
        "--include-transcripts/--text-only",
        help="Include transcripts in the output (default on).",
    ),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Print the dump to the terminal (pretty markdown) instead of saving a file.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="Save to the default reports/ path (skips the interactive output picker).",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Tri-state: --mark-read advances Telegram's marker; --no-mark-read keeps unread and skips the prompt; no flag → ask interactively.",
    ),
    all_flat: bool = typer.Option(
        False,
        "--all-flat",
        help="Forum only: dump whole forum as one file. Needs an explicit period flag.",
    ),
    all_per_topic: bool = typer.Option(
        False,
        "--all-per-topic",
        help="Forum only: one file per topic. Reports land in reports/{chat}/.",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Comma-separated media enrichments to enable before writing the dump: "
            "voice, videonote, video, image, doc, link. Mirrors analyze's flag."
        ),
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Enable every enrichment before writing the dump.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Disable all enrichments for this dump (raw message text only).",
    ),
    save_media: bool = typer.Option(
        False,
        "--save-media",
        help=(
            "Save raw media files (photo / voice / video / doc) alongside "
            "the text dump in reports/<chat>/[topic]/media/. Same effect "
            "as unread download-media but bundled with the dump run."
        ),
    ),
    save_media_types: str | None = typer.Option(
        None,
        "--save-media-types",
        help=(
            "Comma-separated subset to save (voice, videonote, video, photo, doc). "
            "Default: all. Only meaningful with --save-media."
        ),
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help=(
            "Batch-dump every chat in this Telegram folder (case-insensitive "
            "substring match on folder title). Only meaningful without <ref>. "
            "Currently unread-only — pass period flags only with a single ref."
        ),
    ),
    with_comments: bool = typer.Option(
        False,
        "--with-comments",
        help=(
            "For a Telegram channel: also include linked discussion-group "
            "messages (comments). Same time window, same enrichment opts. "
            "No-op for non-channel chats."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmations (per-topic / batch prompts).",
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help=(
            "UI language for the dumped file's headings (en, ru, …). Defaults to [locale] language in config."
        ),
    ),
    report_language: str | None = typer.Option(
        None,
        "--report-language",
        help=(
            "Report / LLM-output language (en, ru, …). When dumping with image/link "
            "enrichment, this is the language the descriptions come back in. Defaults "
            "to [locale] report_language, falling back to --language."
        ),
    ),
    source_language: str | None = typer.Option(
        None,
        "--content-language",
        help=(
            "Source-content language hint (en, ru, zh, …). Whisper-style override — "
            "empty = LLM auto-detects. Defaults to [locale] content_language."
        ),
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help=(
            "Non-Telegram only. Website: text|full (text-only or text+inlined images). "
            "YouTube: transcript|audio|video. Required in non-TTY runs; "
            "interactive prompt picks one when omitted on a TTY."
        ),
    ),
    youtube_source: str = typer.Option(
        "auto",
        "--youtube-source",
        help=(
            "YouTube transcript-mode only. auto = captions then Whisper fallback "
            "(requires OpenAI key); captions = skip if absent; audio = force Whisper. "
            "Mirrors `unread <yt-url>` (analyze) behavior."
        ),
    ),
    max_images: int = typer.Option(
        50,
        "--max-images",
        help="Website --mode=full only: cap on inlined images downloaded per page.",
    ),
) -> None:
    """Dump chat history to a file. Default window = messages since your Telegram read marker.

    Precedence of start-point flags: --full-history > --from-msg >
    --since/--until/--last-days > (default: unread). `--enrich=...`
    runs the same media pipeline as analyze (voice→transcript,
    photo→description, doc→text, link→summary) and embeds results into
    the saved file. Legacy `--with-transcribe` still works for
    audio-only; it's suppressed when `--enrich` is set. `--save-media`
    additionally saves the raw media bytes next to the text dump.

    Without `<ref>` and with `--folder NAME`: batch-dumps every chat in
    that Telegram folder that has unread messages.
    """
    language, report_language, source_language = _validate_lang_flags(
        language, report_language, source_language
    )
    from unread.export.commands import cmd_dump

    _run(
        cmd_dump(
            ref=ref,
            output=output,
            fmt=fmt,
            since=since,
            until=until,
            last_days=last_days,
            last_hours=last_hours,
            last_minutes=last_minutes,
            full_history=full_history,
            thread=thread,
            from_msg=from_msg,
            join=join,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=console_out,
            save_default=save,
            mark_read=mark_read,
            all_flat=all_flat,
            all_per_topic=all_per_topic,
            enrich=enrich,
            enrich_all=enrich_all,
            no_enrich=no_enrich,
            save_media=save_media,
            save_media_types=save_media_types,
            folder=folder,
            with_comments=with_comments,
            yes=yes,
            language=language,
            report_language=report_language,
            source_language=source_language,
            mode=mode,
            youtube_source=youtube_source,
            max_images=max_images,
        )
    )


# --------------------------------------------------------------- shared utilities


def parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def compute_period(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    # Delegate to the canonical implementation to keep UTC-awareness
    # consistent with how `messages.date` is stored (ISO-UTC strings).
    from unread.core.paths import compute_window

    return compute_window(since, until, last_days)


_NEG_NUM_RE = __import__("re").compile(r"^-\d+$")


def _preprocess_argv(argv: list[str] | None = None) -> list[str]:
    """Let users type bare negative numeric chat ids as positional args.

    `unread analyze -1003865481227` normally fails because Click sees
    `-1003865481227` as a short-option token. Older versions of this
    preprocessor injected `--` in place — which fixed the bare case but
    broke `unread analyze -1003865481227 --all-flat`, because `--` closes
    option parsing and `--all-flat` then becomes an unexpected second
    positional.

    The fix: pull negative-number **positionals** out of the arg list
    and re-append them at the end, prefixed by `--`. Flags in between
    stay in place and get parsed normally. A negative number is
    considered a positional when the token before it is NOT a flag
    (so `--chat -1001234` leaves `-1001234` in place as the value of
    `--chat`, but `analyze -1003… --all-flat` pulls the id to the end).

    If the user already used `--` explicitly, we don't touch argv —
    that's a load-bearing user choice.

    Pure function for testability; `main()` passes `sys.argv` in.
    """
    if argv is None:
        import sys as _sys

        argv = list(_sys.argv)
    if not argv:
        return argv
    rest = argv[1:]
    if "--" in rest:
        return list(argv)  # user supplied explicit separator, respect it

    negs: list[str] = []
    kept: list[str] = []
    for i, tok in enumerate(rest):
        if _NEG_NUM_RE.match(tok):
            prev = rest[i - 1] if i > 0 else ""
            # If the previous token is an option (starts with "-"), this
            # negative number is likely its value (e.g. `--chat -1001234`).
            # Leave it in place. Otherwise it's a positional — move it.
            if prev.startswith("-"):
                kept.append(tok)
            else:
                negs.append(tok)
        else:
            kept.append(tok)
    if not negs:
        return list(argv)
    return [argv[0], *kept, "--", *negs]


# =============================================================== Telegram setup commands


def _init_force_clear_session() -> None:
    """Delete both Telethon session-file variants. Shared by `init` /
    `login --force` so the wipe behavior is identical."""
    settings = get_settings()
    session_path = Path(settings.telegram.session_path)
    for p in (session_path, session_path.with_name(session_path.name + ".session")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
    console.print("[yellow]Existing session removed. Re-running init.[/]")


@app.command(
    name="init",
    # Panel is overridden at help-render time by `_print_help_overview`:
    # uninitialized installs surface `init` under Main (the user needs to run
    # it); initialized installs demote it to Maintenance (it's just a re-link
    # tool at that point). Default here is Main as a safe fallback.
    rich_help_panel=PANEL_MAIN,
    help="Interactive setup — pick install folder, AI provider, optional Telegram login.",
)
def init_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Delete the existing Telegram session file and re-run login.",
    ),
) -> None:
    """Run the full setup wizard: install folder, AI provider + key, Telegram login.

    Each step short-circuits when the value is already configured —
    re-running is safe and only prompts for what's missing. To re-pick
    the install folder, delete `~/.unread/install.toml` first. Use
    `unread login` to re-link Telegram without touching the AI step.
    """
    from unread.tg.commands import cmd_init

    _seed_home_templates()
    if force:
        _init_force_clear_session()
    _run(cmd_init(scope="full"))


@tg_app.command(
    name="login",
    help="Telegram log-in (and re-link with `--force`). Skips the AI-provider step.",
)
def login_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Delete the existing session file and run login from scratch.",
    ),
) -> None:
    """Telegram-only setup: log in (and re-link with `--force`).

    Skips the AI-provider step — useful when you just want to add or
    rotate Telegram credentials. Use `unread init` for the full wizard
    that also asks about an AI provider. The auto-init prompt that
    fires when a Telegram-needing command hits a missing/expired
    session runs this same flow inline.
    """
    from unread.tg.commands import cmd_init

    _seed_home_templates()
    if force:
        _init_force_clear_session()
    _run(cmd_init(scope="telegram_only"))


@tg_app.command(
    name="logout",
    help="Clear the local Telegram session without touching credentials.",
)
def logout_cmd() -> None:
    """Clear the local Telegram session without touching credentials.

    Wipes the on-disk session file (or the encrypted `session_string`
    in the secrets table for the passphrase backend) so the next
    `unread tg login` starts a fresh login. Telegram api_id / api_hash
    stay in place — use `unread security clear` for those.

    Equivalent to `unread tg login --force` minus the immediate re-login
    prompt: useful when you want to deauthorize this device and not
    re-link right away.
    """
    from unread.tg.client import _wipe_local_session
    from unread.tg.session_state import is_session_authorized_sync

    s = get_settings()
    had_session = is_session_authorized_sync(s) or _session_exists()
    _wipe_local_session()
    if had_session:
        console.print("[green]Local Telegram session cleared.[/]")
    else:
        console.print("[grey70]No active session to clear.[/]")
    console.print("[grey70]Run `unread tg login` to log in again.[/]")


# =============================================================== migrate command


# =============================================================== help command


@app.command(
    name="help",
    rich_help_panel=PANEL_MAINT,
    help="Show help. `unread help <cmd>` shows command-specific help.",
)
def help_cmd(
    command: list[str] | None = typer.Argument(
        None,
        help="Subcommand path. Example: `unread help chats add` shows chats-add help. "
        "`unread help flags` shows the flags accepted by `unread <ref>`.",
    ),
) -> None:
    """Show command-specific help in the friendly layout.

    With no args, shows the top-level overview (status → usage → ref
    types → command list). Walks the Click command tree when one or
    more subcommand names are given so deeply nested commands
    (`unread help chats add`) work too. Hidden commands stay reachable
    via this path even though they don't appear in the main listing.

    `unread help flags` is special-cased: there's no `analyze`
    subcommand (the analyze logic IS the root callback), so this
    renders the root callback's params under a `<ref>` label so
    users have one canonical place to discover the flags.
    """
    if not command:
        _print_help_overview()
        return

    root_click = typer.main.get_command(app)
    # `unread help flags` → render the root callback's params with
    # the per-command layout. There's no real `analyze` subcommand;
    # the displayed label is `<ref>` since that's the actual usage.
    if command == ["flags"]:
        root_ctx = click.Context(root_click, info_name="unread")
        _print_help_for_command(root_click, root_ctx, label="<ref>")
        return

    cmd: click.Command = root_click
    cur_ctx = click.Context(root_click, info_name="unread")
    for name in command:
        if not isinstance(cmd, click.Group):
            raise typer.BadParameter(f"`{name}` is not a subcommand of `{cur_ctx.info_name}`.")
        sub = cmd.get_command(cur_ctx, name)
        if sub is None:
            raise typer.BadParameter(f"unknown command: {name}")
        cur_ctx = click.Context(sub, info_name=name, parent=cur_ctx)
        cmd = sub
    if isinstance(cmd, click.Group):
        _print_help_for_group(cmd, cur_ctx)
    else:
        _print_help_for_command(cmd, cur_ctx)


# Names known to Click as direct subcommands of the root. The collision
# warning in `_maybe_warn_subcommand_collision` reads from this set.
_RESERVED_TOP_LEVEL.update(
    {
        "tg",
        "telegram",
        "init",
        "help",
        "describe",
        "sync",
        "chats",
        "cache",
        "stats",
        "ask",
        "dump",
        "settings",
        "reports",
        "watch",
        "doctor",
        "backup",
        "completion",
        "killme",
        # Hidden compat commands — still resolvable, still collide.
        "dialogs",
        "topics",
        "resolve",
        "channel-info",
        "backfill",
        "download-media",
        "export",
    }
)


def main() -> None:
    """Entry point — preprocesses argv, then hands off to Typer."""
    import sys as _sys

    _sys.argv = _preprocess_argv(list(_sys.argv))
    app()


if __name__ == "__main__":
    main()
