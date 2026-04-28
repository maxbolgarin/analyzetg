"""unread CLI (Typer). Commands are wired in later phases; stubs here
declare the final signatures so UX is stable from day one."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import datetime
from pathlib import Path

import click
import typer
from rich.console import Console
from typer.core import TyperGroup

from unread.config import get_settings
from unread.db.repo import apply_db_overrides_sync, open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import setup_logging


class _PreferSubcommandsGroup(TyperGroup):
    """Click group that prefers subcommand routing over the optional
    positional `ref` argument on the root callback.

    The root callback declares `ref: str | None = typer.Argument(None)`.
    Standard Click consumes the first non-option token into `ref` BEFORE
    checking for subcommand matches — so `unread describe` ends up
    invoking analyze with ref="describe" instead of dispatching to the
    describe subcommand. We peel a leading subcommand token out of args
    so the positional sees nothing, then inject the token back into
    `ctx.protected_args` so Group's normal routing fires.

    `unread -- describe` (or `unread tg describe`) explicitly forces
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


# Bootstrap DB-saved overrides into the live settings singleton BEFORE
# Typer constructs the app — Typer reads `help=` strings (and panel
# names) at app-construction time. Without this early sync, `--help`
# would render in the config-file language and ignore `unread settings`.
# A read-only sqlite open is safe (~1ms) and degrades to no-op when the
# DB doesn't exist yet (fresh install).
apply_db_overrides_sync(get_settings())

# Panel names — looked up once at import-time so each Typer-decorated
# command can pin its panel to the right localized header.
PANEL_MAIN = _t("cli_panel_main")
PANEL_SYNC = _t("cli_panel_sync")
PANEL_MAINT = _t("cli_panel_maint")

# `no_args_is_help` removed: the root callback handles the no-arg case
# (opens the analyze wizard). `--help` and the new `help` subcommand are
# the explicit help entry points.
app = typer.Typer(
    name="unread",
    help=_t("cli_app_help"),
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
    cls=_PreferSubcommandsGroup,
    # Click groups default to `allow_interspersed_args=False`, which
    # would reject `unread @somegroup --dry-run` (an option after the
    # positional ref). With our `_PreferSubcommandsGroup` already
    # peeling subcommand tokens explicitly, it's safe to allow
    # interspersed options on the root callback.
    context_settings={"allow_interspersed_args": True},
)

chats_app = typer.Typer(help=_t("cmd_chats"), no_args_is_help=True)
cache_app = typer.Typer(help=_t("cmd_cache"), no_args_is_help=True)
app.add_typer(chats_app, name="chats", rich_help_panel=PANEL_SYNC)
app.add_typer(cache_app, name="cache", rich_help_panel=PANEL_MAINT)

console = Console()


def _run(coro) -> None:
    asyncio.run(coro)


# Names of every Typer command/group on the root app. Used by the root
# callback to warn when `unread <bare-word>` collides with a subcommand
# name (Click resolves subcommands first; the user almost certainly
# meant `unread tg <ref>` to disambiguate).
_RESERVED_TOP_LEVEL: set[str] = set()


def _maybe_warn_subcommand_collision(ref: str | None) -> None:
    """Surface a one-line hint when `ref` shadows a real subcommand.

    The user typed something like `unread describe` intending a chat
    titled "describe" — Click already routed to the subcommand instead.
    They land here only when `ref` slipped through the parser (which
    means they used a non-colliding form). This hook is a future-proof:
    it warns when the value matches anyway, suggesting the `tg` form.
    """
    if ref and ref in _RESERVED_TOP_LEVEL:
        console.print(
            f"[yellow]Note: `{ref}` is also a subcommand name. "
            f"For a chat literally titled '{ref}', use `unread tg {ref}` "
            f"or `unread -- {ref}`.[/]"
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
    """True iff the currently-selected chat provider has its key set.

    Routed by `settings.ai.provider`. Used to gate chat-only commands
    (`analyze`, `ask`) so a Telegram-only or wrong-provider install
    surfaces a focused banner instead of a confusing 401.
    """
    import os as _os

    s = get_settings()
    name = (s.ai.provider or "openai").strip().lower()
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

    `missing` ∈ {"openai", "telegram", "both"} controls the copy. The
    banner always points at `unread tg init` first (the interactive
    wizard handles missing keys without re-prompting for already-set
    ones), and mentions the `~/.unread/.env` non-interactive path as a
    secondary option.
    """
    from unread.core.paths import default_env_path, ensure_unread_home

    ensure_unread_home()
    env_path = default_env_path()
    if missing == "openai":
        title = "OpenAI key missing."
        env_lines = "  OPENAI_API_KEY=sk-…"
    elif missing == "telegram":
        title = "Telegram credentials missing."
        env_lines = "  TELEGRAM_API_ID=…\n  TELEGRAM_API_HASH=…"
    else:
        title = "First-run setup needed."
        env_lines = "  OPENAI_API_KEY=sk-…\n  TELEGRAM_API_ID=…\n  TELEGRAM_API_HASH=…"
    console.print(
        f"[bold yellow]{title}[/]\n"
        f"\n"
        f"Run [cyan]unread tg init[/] to set up your install folder, "
        f"OpenAI key, and (optionally) Telegram login.\n"
        f"\n"
        f"Or, for scripted / non-interactive setup, edit [bold]{env_path}[/] and fill in:\n"
        f"{env_lines}\n"
        f"\n"
        f"Telegram credentials: https://my.telegram.org → API development tools. "
        f"OpenAI: https://platform.openai.com/api-keys."
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
    """
    from shutil import copyfile

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
        copyfile(env_template, env_target)
        with contextlib.suppress(OSError):
            env_target.chmod(0o600)
    if not cfg_target.exists() and cfg_template.exists():
        copyfile(cfg_template, cfg_target)


def _dispatch_analyze(**kwargs) -> None:
    """Shared bridge from the root + tg callbacks to `cmd_analyze`.

    Both the root callback (`unread <ref>`) and the `tg` callback
    (`unread tg <ref>`) collect the same option set and need to dispatch
    to the same analyze pipeline. This helper lives here so the only
    difference between the two callbacks is the auto-init policy.
    """
    from unread.analyzer.commands import cmd_analyze

    save_flag = kwargs.pop("save", False)
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
    return bool(p.suffix and p.is_file())


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
    """True when stdin is piped / redirected (not an interactive TTY).

    `unread` (no args) on a TTY shows the quickstart panel. The same
    invocation with stdin piped (`cat foo.txt | unread`) routes the
    piped bytes through the file analyzer instead — same UX as bare
    `unread <ref>`, just sourcing content from stdin.
    """
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        # Some embedded environments (e.g. older Windows shells) raise
        # on `isatty()`; treating those as "no stdin" preserves the
        # quickstart-panel default.
        return False


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


def _print_quickstart() -> None:
    """Short cheat sheet shown when `unread` is run with no args.

    The full reference lives behind `unread help` / `--help`. This panel
    is just enough to get a first-time user oriented: log in, pick a
    chat, find more.
    """
    console.print(
        "[bold]unread[/] — local Telegram / YouTube / web-page analyzer\n"
        "\n"
        "[bold]Get started[/]\n"
        "  [cyan]unread tg init[/]              interactive setup — pick install folder, OpenAI key, optional Telegram login\n"
        "  [cyan]unread tg[/]                   interactive wizard — pick a chat, run analyzer\n"
        "  [cyan]unread <ref>[/]                analyze a chat, YouTube link, or web page\n"
        "  [cyan]unread tg <ref>[/]             same, plus auto-runs login on first use\n"
        "\n"
        "[bold]Other common commands[/]\n"
        '  [cyan]unread ask[/] [dim]"<question>"[/]      Q&A across your synced archive\n'
        "  [cyan]unread dump <ref>[/]           export chat history to a file\n"
        "  [cyan]unread describe[/]             list dialogs / inspect a chat\n"
        "  [cyan]unread doctor[/]               preflight check (creds, session, ffmpeg, …)\n"
        "  [cyan]unread migrate[/]              move legacy ./storage + ./reports into ~/.unread/\n"
        "\n"
        '[bold]<ref>[/] examples: [dim]@username[/], [dim]t.me/c/123/45[/], [dim]"Fuzzy title"[/], '
        "[dim]-1001234567890[/], [dim]https://youtu.be/...[/], [dim]https://example.com/article[/], "
        "[dim]./report.pdf[/], [dim]-[/] (stdin)\n"
        "\n"
        "[dim]Tip:[/] you can run `unread tg init` without an OpenAI key — "
        "[cyan]dump[/], [cyan]describe[/], [cyan]sync[/], etc. still work.\n"
        "\n"
        "[dim]More:[/] [cyan]unread help[/] · [cyan]unread help <command>[/] · [cyan]unread --help[/]"
    )


def _ensure_ready_for_analyze(ref: str | None) -> bool:
    """Bootstrap `~/.unread/` and Telegram session before any analyze run.

    Called for both `unread <ref>` and `unread tg <ref>`. Analyze always
    needs the *active chat provider's* key (OpenAI / OpenRouter /
    Anthropic / Google / Local-server-credential) — gate on that and
    surface a focused banner pointing at `unread tg init` when missing.

    For Telegram refs (chat / wizard / Telegram URL), if no session
    exists we kick off `cmd_init()` to walk the user through Telegram
    setup. Missing Telegram credentials surface via `build_client`'s
    own friendly banner (in `tg/client.py`) so we don't double-message.

    Returns True if the caller should proceed with analyze, False if
    the caller should stop (a banner has already been printed).
    """
    from unread.tg.commands import cmd_init

    _seed_home_templates()
    if not _active_provider_credentials_present():
        # Raises typer.Exit(1) — analyze is dead in the water without
        # the active provider's key, so we surface the friendly banner
        # + non-zero exit instead of silently returning to the caller.
        _exit_missing_provider_credentials()
    if _looks_like_telegram_ref(ref) and not _session_exists():
        _run(cmd_init())
    return True


def _exit_missing_provider_credentials() -> typer.Exit:
    """Banner + exit for chat commands when the active provider has no key."""
    s = get_settings()
    provider = (s.ai.provider or "openai").strip().lower()
    _print_provider_credentials_banner(provider)
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
        f"[bold yellow]{label} key missing for the active chat provider.[/]\n"
        f"\n"
        f"Run [cyan]unread tg init[/] to add one (or pick a different provider).\n"
        f"\n"
        f"Or, for scripted / non-interactive setup, edit [bold]{env_path}[/] and fill in:\n"
        f"  {env_line}"
    )


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_describe"))
def describe(
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


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_folders"))
def folders() -> None:
    """List your Telegram folders (for use with `analyze --folder NAME` / `dump --folder NAME`)."""
    _run(_list_folders())


# --- Hidden compatibility aliases: the consolidated `describe` absorbs these.
# Kept callable so existing scripts don't break.


@app.command(hidden=True)
def dialogs(
    search: str | None = typer.Option(None, "--search"),
    kind: str | None = typer.Option(None, "--kind"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Deprecated: use `describe` instead."""
    from unread.tg.commands import cmd_dialogs

    _run(cmd_dialogs(search=search, kind=kind, limit=limit))


@app.command(hidden=True)
def topics(
    chat_ref: str | None = typer.Argument(None),
    chat: int | None = typer.Option(None, "--chat"),
) -> None:
    """Deprecated: use `describe <ref>` instead."""
    from unread.tg.commands import cmd_topics

    if chat_ref is None and chat is None:
        console.print(f"[red]{_t('cli_ref_or_chat_required')}[/]")
        raise typer.Exit(2)
    _run(cmd_topics(chat_ref if chat_ref is not None else str(chat)))


@app.command(hidden=True)
def resolve(anything: str = typer.Argument(...)) -> None:
    """Diagnostic: parse a reference and show the resolution path."""
    from unread.tg.commands import cmd_resolve

    _run(cmd_resolve(anything))


@app.command("channel-info", hidden=True)
def channel_info(ref: str = typer.Argument(...)) -> None:
    """Deprecated: use `describe <channel-ref>` instead."""
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
        help="Default preset for `unread chats run` on this sub (summary, action_items, …). Wizard asks if not set.",
    ),
    period: str | None = typer.Option(
        None,
        "--period",
        help="Default period for `unread chats run` on this sub: unread | last24h | last96h | last7 | last30 | last90 | year_start | full. Wizard asks if not set.",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Default enrichments for `unread chats run` on this sub. CSV of "
            "voice,videonote,video,image,doc,link. Empty string disables all. "
            "Unset = use config defaults at run time."
        ),
    ),
    no_mark_read: bool = typer.Option(
        False,
        "--no-mark-read",
        help="Don't advance Telegram's read marker after `unread chats run` analyzes this sub.",
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help="Telegram chat ref to post the report to (`me` for Saved Messages). Used by `unread chats run`.",
    ),
) -> None:
    """Add a subscription (chat / topic / channel with comments).

    Without a `<ref>`, opens the interactive chat picker (same one used by
    `unread analyze`). For a channel, asks whether to also subscribe to its
    linked discussion group; for a forum, asks whether to include every
    topic. CLI flags pre-fill those answers when given.

    The wizard also captures per-subscription defaults consumed by
    `unread chats run` — preset, period, enrich kinds, mark-read, post-to — so a
    later `unread chats run` walks every enabled sub and analyzes each one with
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
    console.print(f"[dim]{_t('cli_folders_use_with')}[/]")


# ================================================================ 5.3 Sync


@app.command(rich_help_panel=PANEL_SYNC, help=_t("cmd_sync"))
def sync(
    chat: int | None = typer.Option(None, "--chat"),
    thread: int | None = typer.Option(None, "--thread"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Incrementally fetch new messages for all (or one) subscriptions."""
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

    `unread chats add` captures per-subscription preset / period / enrich
    kinds / mark-read / post-to. `unread chats run` walks each enabled
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


@app.command(hidden=True)
def backfill(
    chat: int = typer.Option(..., "--chat"),
    from_msg: str = typer.Option(..., "--from-msg"),
    direction: str = typer.Option("back", "--direction", help="back | forward"),
) -> None:
    """One-shot history backfill starting from a specific message.

    Niche helper — most users want `analyze --from-msg <id>` or
    `dump --from-msg <id>` instead.
    """
    from unread.tg.commands import cmd_backfill

    _run(cmd_backfill(chat=chat, from_msg=from_msg, direction=direction))


# =================================================================== 5.4 Analyze


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "A message link like t.me/c/ID/MSG is treated as single-message "
            "mode (analyze just that one message, auto-transcribing voice/video). "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`unread -- -1001234567890`. Omit to pick every dialog "
            "with unread messages (interactive)."
        ),
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
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days"),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help=(
            "Restrict to messages newer than N hours ago. Mutually "
            "exclusive with --since/--until/--full-history; if combined "
            "with --last-days, --last-hours wins (more specific)."
        ),
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Analysis preset (default: 'summary' for chats, 'single_msg' when analyzing one message).",
    ),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    model: str | None = typer.Option(None, "--model"),
    filter_model: str | None = typer.Option(None, "--filter-model"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="[DEPRECATED] Same as --no-save. Reports always render in the terminal now; this flag only skips the file write.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="[DEPRECATED] No-op. Saving is now the default; pass --no-save to opt out.",
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        help="Skip writing the report file. The result still renders in the terminal.",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Tri-state: --mark-read advances Telegram's marker; --no-mark-read explicitly keeps unread and skips the prompt; no flag → ask interactively.",
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    include_transcripts: bool = typer.Option(True, "--include-transcripts/--text-only"),
    min_msg_chars: int | None = typer.Option(None, "--min-msg-chars"),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Comma-separated media enrichments to enable: "
            "voice, videonote, video, image, doc, link. "
            "Overrides config defaults for this run. "
            "Example: --enrich=voice,image,link"
        ),
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
        help="Skip interactive confirmations (per-topic Y/n, batch-of-N-chats Y/n). Useful for scripting or when the prompt-toolkit → typer.confirm handoff acts up in your terminal.",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help=(
            "Batch-analyze all unread chats inside this Telegram folder "
            "(dialog filter). Case-insensitive match on folder title. "
            "Only meaningful without <ref>."
        ),
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help=(
            "Abort if the upper-bound estimated USD cost of this run exceeds "
            "N (estimate uses preset models, message count, and your pricing "
            "table). Pass with --yes to abort silently; without --yes you'll "
            "be asked to confirm an over-budget run."
        ),
    ),
    post_saved: bool = typer.Option(
        False,
        "--post-saved",
        help=(
            "After analysis finishes, also post the result to your Telegram "
            "Saved Messages chat (split into 4096-char chunks if needed). "
            "Markdown-friendly: rendered as monospace by Telegram."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Resolve the chat, run backfill, count messages, print the cost estimate, "
            "and exit before any LLM call. Useful before --enrich-all / --full-history."
        ),
    ),
    cite_context: int = typer.Option(
        0,
        "--cite-context",
        help=(
            "After analysis, append a `## Источники` section to the saved report "
            "with N messages of context around every cited [#msg_id](url). "
            "0 (default) = off; 3 = three before + three after. Capped at 30 citations."
        ),
    ),
    self_check: bool = typer.Option(
        False,
        "--self-check",
        help=(
            "After analysis, run a cheap-model audit pass that lists unsupported "
            "claims under `## Verification`. Adds ~10% to cost. Useful when you'll "
            "act on the report without re-reading the source messages."
        ),
    ),
    by: str | None = typer.Option(
        None,
        "--by",
        help=(
            "Filter to messages from one sender. Substring match on sender_name "
            "(case-insensitive) or numeric sender_id. Composes with all other filters."
        ),
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help=(
            "After analysis, post the result to this chat (any chat ref: @user, "
            "t.me link, fuzzy title, numeric id, or 'me' for Saved Messages). "
            "Generalization of --post-saved (which is now sugar for --post-to=me)."
        ),
    ),
    repeat_last: bool = typer.Option(
        False,
        "--repeat-last",
        help=(
            "Look up the saved flags from the most recent successful analyze on "
            "<ref> and re-use them. Explicit CLI flags on this run still win "
            "(e.g. `--repeat-last --no-cache` to bust the cache while keeping "
            "everything else)."
        ),
    ),
    with_comments: bool = typer.Option(
        False,
        "--with-comments",
        help=(
            "For a Telegram channel: also include messages from its linked "
            "discussion group (comments) in the same analysis. Comments are "
            "pulled for the same time window as the channel posts and go "
            "through the SAME enrichment toggles. The report renders "
            "channel posts and comments as two sections with their own "
            "citation links. No-op for non-channel chats."
        ),
    ),
    language: str | None = typer.Option(
        None,
        "--language",
        help=(
            "Output / report / UI language (en, ru, de, …). Picks the matching "
            "presets/<lang>/ tree, formatter labels, and analysis output language. "
            "Defaults to [locale] language in config (en)."
        ),
    ),
    content_language: str | None = typer.Option(
        None,
        "--content-language",
        help=(
            "Chat content language hint for cost estimation only. Defaults to "
            "--language. Set explicitly when chats are predominantly one language "
            "but the report should be in another."
        ),
    ),
    youtube_source: str = typer.Option(
        "auto",
        "--youtube-source",
        help=(
            "YouTube transcript source: auto (captions, fallback to Whisper), "
            "captions (fail if none), audio (always Whisper). Used only when "
            "<ref> is a YouTube URL."
        ),
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

    For Telegram-only setup, use `unread tg` (auto-runs login on first
    use) or `unread tg init --force` to re-link.
    """
    setup_logging(verbose=verbose)
    if ctx.invoked_subcommand is not None:
        # A subcommand was matched (describe, ask, sync, …); let it run.
        return
    # Same callback is registered on the root app AND the `tg` /
    # `telegram` sub-typer. `info_name` tells us which entrypoint Click
    # resolved — used here to differentiate the no-args UX.
    via_tg = ctx.info_name in ("tg", "telegram")
    # Stdin auto-detect: `cat foo.txt | unread` (no ref, non-TTY stdin)
    # routes the piped bytes through the file analyzer. The explicit
    # form is `unread -`; both flow through `cmd_analyze_file` with a
    # sentinel that tells it to read stdin instead of opening a path.
    if ref == "-" or (ref is None and not via_tg and _stdin_has_data()):
        ref = _STDIN_REF_SENTINEL
    if ref is None and not via_tg:
        # Bare `unread` is an orientation panel, not a command — the
        # interactive wizard moved to `unread tg`. This keeps the
        # zero-arg invocation cheap and discoverable instead of
        # surprising new users with a credential prompt or wizard.
        _print_quickstart()
        return
    # `unread <ref>` and `unread tg [<ref>]` both need ~/.unread/ ready
    # plus (for Telegram refs / wizard) an authorized session. Skipped
    # for YouTube / non-Telegram URL refs since those analyzers don't
    # need a Telegram session at all.
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
        preset=preset,
        prompt_file=prompt_file,
        model=model,
        filter_model=filter_model,
        output=output,
        console_out=console_out,
        save=save,
        no_save=no_save,
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
        content_language=content_language,
        youtube_source=youtube_source,
    )


# ============================================================== 5.4b Download media


@app.command("download-media", hidden=True)
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
    last_days: int | None = typer.Option(None, "--last-days"),
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
            output=output,
            limit=limit,
            overwrite=overwrite,
            dry_run=dry_run,
        )
    )


# ============================================================== 5.5 Maintenance


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_stats"))
def stats(
    since: str | None = typer.Option(None, "--since"),
    by: str = typer.Option("preset", "--by", help="chat | preset | model | day | kind"),
) -> None:
    """Aggregate API spend, cache hit rate and run counts."""
    from unread.analyzer.commands import cmd_stats

    _run(cmd_stats(since=since, by=by))


@cache_app.command("purge")
def cache_purge(
    older_than: str = typer.Option("30d", "--older-than", help="Nd"),
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
    vacuum: bool = typer.Option(False, "--vacuum", help="Run VACUUM after purge to reclaim disk."),
) -> None:
    """Delete cached analysis results by age and filters."""
    _run(_cache_purge(older_than, preset, model, vacuum))


async def _cache_purge(
    older_than: str,
    preset: str | None,
    model: str | None,
    vacuum: bool,
) -> None:
    settings = get_settings()
    days = _parse_duration_days(older_than)
    if days <= 0:
        console.print(f"[yellow]{_t('cli_skipped_label')}[/] {_t('cli_cache_purge_min_days')}")
        return
    async with open_repo(settings.storage.data_path) as repo:
        removed = await repo.cache_purge(older_than_days=days, preset=preset, model=model)
        console.print(
            f"[green]{_t('cli_purged_label')}[/] {_tf('cli_cache_purged_msg', n=removed, days=days)}"
        )
        if vacuum:
            reclaimed = await repo.vacuum()
            console.print(
                f"[green]{_t('cli_vacuumed_label')}[/] "
                f"{_tf('cli_db_vacuumed_msg', size=_fmt_bytes(reclaimed))}"
            )


@cache_app.command("effectiveness")
def cache_effectiveness_cmd(
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
) -> None:
    """Per-(chat, preset) OpenAI prompt-cache hit rate from usage_log.

    Surfaces "what's actually saving money": the server-side prompt cache
    only kicks in when the stable prefix (system + static_context) is
    1024+ tokens AND byte-identical across calls. Low hit rate on a
    high-volume row → check the prompt for entropy in its prefix.
    """
    _run(_cache_effectiveness(since))


async def _cache_effectiveness(since: str | None) -> None:
    from rich.table import Table

    settings = get_settings()
    since_dt = parse_ymd(since) if since else None
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.cache_effectiveness(since=since_dt)
    if not rows:
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
    for r in rows:
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
    console.print(f"[dim]{_t('cli_cache_eff_hint')}[/]")


@cache_app.command("stats")
def cache_stats_cmd() -> None:
    """Show analysis cache size, age range and per-(preset, model) breakdown."""
    _run(_cache_stats())


async def _cache_stats() -> None:
    from rich.table import Table

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        s = await repo.cache_stats()
    if s["rows"] == 0:
        console.print(f"[yellow]{_t('cli_cache_empty')}[/]")
        return
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


@cache_app.command("ls")
def cache_ls_cmd(
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
    older_than: str | None = typer.Option(None, "--older-than", help="Nd / Nw"),
    limit: int = typer.Option(50, "--limit"),
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


@cache_app.command("show")
def cache_show_cmd(
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


@cache_app.command("export")
def cache_export_cmd(
    output: Path = typer.Option(
        ..., "--output", "-o", help="File path. Extension picks format if --format omitted."
    ),
    fmt: str | None = typer.Option(None, "--format", help="jsonl | md"),
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
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
        rows = await repo.cache_iter_full(preset=preset, model=model, older_than_days=days)

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


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_cleanup"))
def cleanup(
    retention: str = typer.Option("90d", "--retention"),
    chat: int | None = typer.Option(None, "--chat"),
    keep_transcripts: bool = typer.Option(True, "--keep-transcripts/--no-keep-transcripts"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Null-out old message texts; keep transcripts/analysis cache."""
    _run(_cleanup(retention, chat, keep_transcripts, yes))


async def _cleanup(retention: str, chat: int | None, keep_transcripts: bool, yes: bool) -> None:
    settings = get_settings()
    days = _parse_duration_days(retention)
    async with open_repo(settings.storage.data_path) as repo:
        preview = await repo.count_redactable_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
        )
        if preview["to_redact"] == 0:
            if preview["messages"] == 0:
                console.print(
                    f"[yellow]{_t('cli_cleanup_nothing')}[/] {_tf('cli_cleanup_older_than', days=days)}"
                )
            else:
                tail = _t("cli_cleanup_transcripts_kept") if keep_transcripts else ""
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
            f"0 [dim]{_t('cli_cleanup_kept_label')}[/]"
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
        console.print(
            f"[bold]{_t('cli_cleanup_preview_title')}[/] ({scope}, "
            f"{_tf('cli_cleanup_older_than', days=days).rstrip('.')}):\n{body}"
        )
        if not yes and not typer.confirm(_t("cli_cleanup_proceed_q"), default=False):
            console.print(f"[yellow]{_t('cli_aborted')}[/]")
            return

        redacted = await repo.redact_old_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
        )
        tail = _t("cli_redacted_transcripts_kept") if keep_transcripts else ""
        console.print(
            f"[green]{_t('cli_redacted_label')}[/] "
            f"{_tf('cli_redacted_msg', n=redacted, days=days, tail=tail)}"
        )


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_ask"))
def ask(
    question: str | None = typer.Argument(
        None, help="Free-form question, in any language. Omit to enter the wizard."
    ),
    ref: str | None = typer.Argument(
        None,
        help=(
            "Optional chat reference: @user, t.me link (incl. topic links like "
            "t.me/c/<id>/<topic>), fuzzy title, or numeric id. "
            "Mutually exclusive with --chat / --folder / --global."
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
    last_days: int | None = typer.Option(None, "--last-days"),
    last_hours: int | None = typer.Option(
        None,
        "--last-hours",
        help=(
            "Restrict to messages newer than N hours ago. Mutually "
            "exclusive with --since/--until; if combined with "
            "--last-days, --last-hours wins (more specific)."
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
            "Language for the answer + UI labels (en, ru, …). Defaults to "
            "[locale] language in config (en). The model also tends to follow "
            "the question's language when it differs."
        ),
    ),
    content_language: str | None = typer.Option(
        None,
        "--content-language",
        help=(
            "Chat content language — drives the system prompt + label "
            "language sent to the LLM. Defaults to --language. Override when "
            "your chat is in a different language than your interface."
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

    Examples:
      unread ask "what did Bob say about migration?" @somegroup
      unread ask "open Qs?" https://t.me/c/3865481227/4         # incl. topic
      unread ask "..." --folder Work --last-days 7
      unread ask                                                 # opens wizard
      unread ask "..." --global                                  # all synced, no wizard
    """
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
            content_language=content_language,
            mark_read=mark_read,
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


reports_app = typer.Typer(help=_t("cmd_reports"), no_args_is_help=True)
app.add_typer(reports_app, name="reports", rich_help_panel=PANEL_MAINT)


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
        console.print(f"[dim]{_tf('cli_prune_nothing_old', days=days, root=str(root))}[/]")
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
        console.print(f"  [dim]{_tf('cli_prune_and_more', n=len(candidates) - 20)}[/]")
    if dry_run:
        return
    if not yes and not typer.confirm(_t("cli_prune_proceed_q"), default=False):
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
        help="How often to fire the inner command. Accepts Nm / Nh / Nd / Nw.",
    ),
    max_runs: int | None = typer.Option(
        None,
        "--max-runs",
        help="Stop after N successful runs (handy for testing). None = run forever.",
    ),
) -> None:
    """Run an inner `unread` command on a fixed cadence.

    `unread watch --interval 1h analyze --folder Work --post-saved` walks the
    wall clock: runs the inner command, sleeps for the interval, repeats.
    Foreground only — run it under `tmux` / `nohup` if you need
    persistence. Ctrl-C exits cleanly between iterations.

    The inner command runs in a fresh subprocess each time (so an internal
    crash doesn't poison subsequent runs); exit codes are surfaced but
    don't abort the loop unless `--max-runs` is hit.
    """
    inner = ctx.args
    if not inner:
        console.print(f"[red]{_t('cli_watch_need_inner')}[/]")
        raise typer.Exit(2)
    _run(_watch_loop(interval, max_runs, inner))


async def _watch_loop(interval: str, max_runs: int | None, inner: list[str]) -> None:
    import asyncio as _asyncio
    import shlex
    import subprocess
    import sys as _sys

    seconds = _parse_duration_seconds(interval)
    if seconds <= 0:
        console.print(f"[red]{_t('cli_watch_interval_positive')}[/]")
        raise typer.Exit(2)

    runs = 0
    cmd = ["unread", *inner]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    console.print(f"[bold cyan]{_tf('cli_watch_watching', interval=interval, cmd=pretty)}[/]")
    # Single Ctrl-C handler covers both phases (subprocess.run / sleep).
    # subprocess.run inherits stdin so child sees the SIGINT first; if
    # the child handles it cleanly, control returns here and we just
    # continue. If the user mashes Ctrl-C again during sleep, it
    # propagates as KeyboardInterrupt and we exit.
    try:
        while True:
            runs += 1
            console.print(
                f"\n[bold]{_tf('cli_watch_run_n', n=runs)}[/] "
                f"[dim]{datetime.now().isoformat(timespec='seconds')}[/]"
            )
            try:
                # subprocess.run blocks the event loop; that's fine — we're
                # not racing anything here, and the inner command may itself
                # spin up its own asyncio loop.
                proc = subprocess.run(cmd, check=False)
                if proc.returncode != 0:
                    console.print(f"[yellow]{_tf('cli_watch_inner_exited', code=proc.returncode)}[/]")
            except FileNotFoundError:
                console.print(f"[red]{_tf('cli_watch_not_on_path', cmd=cmd[0])}[/]")
                raise typer.Exit(2) from None
            if max_runs is not None and runs >= max_runs:
                console.print(f"[dim]{_tf('cli_watch_max_runs_reached', n=max_runs)}[/]")
                return
            console.print(f"[dim]{_tf('cli_watch_sleeping', interval=interval)}[/]")
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


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_backup"))
def backup(
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
    Restore with `unread restore <file>`.
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
    console.print(f"[green]{_t('cli_backup_done_label')}[/] {src} → {output} [dim]({_fmt_bytes(size)})[/]")


@app.command(rich_help_panel=PANEL_MAINT, help=_t("cmd_restore"))
def restore(
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
    if not yes and not typer.confirm(
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
        console.print(f"[dim]{_tf('cli_restore_moved_db', path=str(moved))}[/]")
    # Also clear -wal / -shm sidecars so the restored DB doesn't pick up
    # transactions from the replaced DB on next open.
    for sidecar in (dst.with_suffix(dst.suffix + "-wal"), dst.with_suffix(dst.suffix + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    shutil.copy2(backup_file, dst)
    console.print(f"[green]{_t('cli_restore_done_label')}[/] {backup_file} → {dst}")


@app.command(hidden=True)
def export(
    chat: int = typer.Option(..., "--chat"),
    fmt: str = typer.Option("md", "--format", help="jsonl | csv | md"),
    output: Path = typer.Option(..., "--output"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
) -> None:
    """Export already-synced messages from the local DB to jsonl / csv / md."""
    from unread.export.commands import cmd_export

    _run(cmd_export(chat=chat, fmt=fmt, output=output, since=since, until=until))


@app.command(rich_help_panel=PANEL_MAIN, help=_t("cmd_dump"))
def dump(
    ref: str | None = typer.Argument(
        None,
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
            "Language for formatter labels in the dumped file (en, ru, …). "
            "Defaults to [locale] language in config (en)."
        ),
    ),
    content_language: str | None = typer.Option(
        None,
        "--content-language",
        help=(
            "Chat content language — when set, image/link enricher prompts use this. Defaults to --language."
        ),
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
            content_language=content_language,
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


# =============================================================== Telegram subgroup
# `unread tg` and `unread telegram` mirror the root analyze entry point
# but auto-run login on first use. The shared callback is `_root`
# (registered above on the root app); we register the SAME function as
# this typer's callback so `unread tg <ref>` accepts the full flag set
# without duplicating 30+ option declarations. The branch on
# `ctx.info_name` inside `_root` picks up the auto-init policy.

tg_app = typer.Typer(
    name="tg",
    help="Analyze a Telegram chat (auto-runs `init` on first use). Same flags as `unread <ref>`.",
    invoke_without_command=True,
    add_completion=False,
    rich_markup_mode="rich",
    cls=_PreferSubcommandsGroup,
    context_settings={"allow_interspersed_args": True},
)
tg_app.registered_callback = app.registered_callback  # share the analyze callback


@tg_app.command("init")
def tg_init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Delete the existing session file and run init from scratch.",
    ),
) -> None:
    """First-time Telegram login (and OpenAI key smoke test).

    `--force` removes the saved session before logging in — useful when
    you want to re-link a different Telegram account or your session
    file got into a weird state.
    """
    from unread.tg.commands import cmd_init

    settings = get_settings()
    session_path = Path(settings.telegram.session_path)

    _seed_home_templates()
    if force:
        # Telethon writes either `<name>.sqlite` or `<name>.sqlite.session`
        # depending on platform / version. Wipe both forms.
        for p in (session_path, session_path.with_name(session_path.name + ".session")):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        console.print("[yellow]Existing session removed. Re-running init.[/]")
    _run(cmd_init())


# Register `tg` as the visible primary, `telegram` as a hidden alias
# that resolves to the same callback. Two `add_typer` calls give Typer
# two TyperInfo entries pointing at the same Typer instance — the
# callback (`_root`) is shared, so flags stay in sync automatically.
app.add_typer(tg_app, name="tg", rich_help_panel=PANEL_MAIN)
app.add_typer(tg_app, name="telegram", rich_help_panel=PANEL_MAIN, hidden=True)


# =============================================================== migrate command


@app.command(
    rich_help_panel=PANEL_MAINT,
    help="Move legacy ./storage and ./reports from the current directory into ~/.unread/.",
)
def migrate(
    move: bool = typer.Option(
        False,
        "--move",
        help="Move files instead of copying. Removes the cwd-relative copies after success.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would happen, take no action.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace files in ~/.unread/ if they already exist.",
    ),
) -> None:
    """Migrate a legacy cwd-relative install into ~/.unread/.

    Useful after upgrading from an older `unread` that lived in a cloned
    repo directory. Detects `./.env`, `./config.toml`, `./storage/`, and
    `./reports/` in the working directory and (by default) copies them
    into `~/.unread/`. `--move` removes the cwd-relative copy on success.
    """
    import shutil

    from unread.core.paths import (
        default_config_path,
        default_env_path,
        ensure_unread_home,
        reports_dir,
        storage_dir,
    )

    ensure_unread_home()
    cwd = Path.cwd()

    # Each entry: (label, source path, destination path)
    plan: list[tuple[str, Path, Path]] = [
        (".env", cwd / ".env", default_env_path()),
        ("config.toml", cwd / "config.toml", default_config_path()),
        ("storage/", cwd / "storage", storage_dir()),
        ("reports/", cwd / "reports", reports_dir()),
    ]

    actions: list[tuple[str, Path, Path, str]] = []  # (label, src, dest, action)
    for label, src, dest in plan:
        if not src.exists():
            actions.append((label, src, dest, "skip (source missing)"))
            continue
        if src.resolve() == dest.resolve():
            actions.append((label, src, dest, "skip (already at destination)"))
            continue
        if dest.exists() and not overwrite:
            actions.append((label, src, dest, "skip (destination exists; use --overwrite)"))
            continue
        actions.append((label, src, dest, "MOVE" if move else "COPY"))

    console.print(f"[bold]Migration plan ([dim]home={ensure_unread_home()}[/]):[/]")
    for label, src, dest, action in actions:
        marker = "[yellow]→[/]" if action in ("MOVE", "COPY") else "[dim]·[/]"
        console.print(f"  {marker} {label:<14} {src}  →  {dest}  [{action}]")

    if dry_run:
        console.print("[dim]--dry-run: no changes made.[/]")
        return

    moved_or_copied = 0
    for label, src, dest, action in actions:
        if action not in ("MOVE", "COPY"):
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if src.is_dir():
                if action == "MOVE":
                    shutil.move(str(src), str(dest))
                else:
                    shutil.copytree(src, dest)
            elif action == "MOVE":
                shutil.move(str(src), str(dest))
            else:
                shutil.copy2(src, dest)
            moved_or_copied += 1
            past = "moved" if action == "MOVE" else "copied"
            console.print(f"  [green]✓[/] {label} {past}")
        except Exception as e:
            console.print(f"  [red]×[/] {label}: {e}")

    if moved_or_copied == 0:
        console.print("[dim]Nothing to migrate.[/]")
    else:
        console.print(
            f"\n[green]Migration complete:[/] {moved_or_copied} item(s) "
            f"{'moved' if move else 'copied'} into {ensure_unread_home()}."
        )


# =============================================================== help command


@app.command(
    name="help",
    rich_help_panel=PANEL_MAIN,
    help="Show help. `unread help <cmd>` shows command-specific help.",
)
def help_cmd(
    command: list[str] | None = typer.Argument(
        None,
        help="Subcommand path. Example: `unread help chats add` shows chats-add help.",
    ),
) -> None:
    """Print the same help that `--help` would produce.

    With no args, shows the top-level overview. Walks the Click command
    tree when subcommand names are given so deeply nested commands
    (`unread help chats add`) work too. Hidden commands stay reachable
    via this path even though they don't appear in the main listing.
    """
    import click

    root_click = typer.main.get_command(app)
    cmd: click.Command = root_click
    cur_ctx = click.Context(root_click, info_name="unread")
    for name in command or []:
        if not isinstance(cmd, click.Group):
            raise typer.BadParameter(f"`{name}` is not a subcommand of `{cur_ctx.info_name}`.")
        sub = cmd.get_command(cur_ctx, name)
        if sub is None:
            raise typer.BadParameter(f"unknown command: {name}")
        cur_ctx = click.Context(sub, info_name=name, parent=cur_ctx)
        cmd = sub
    typer.echo(cmd.get_help(cur_ctx))


# Names known to Click as direct subcommands of the root. The collision
# warning in `_maybe_warn_subcommand_collision` reads from this set.
_RESERVED_TOP_LEVEL.update(
    {
        "tg",
        "telegram",
        "help",
        "migrate",
        "describe",
        "folders",
        "sync",
        "chats",
        "cache",
        "stats",
        "ask",
        "dump",
        "cleanup",
        "settings",
        "reports",
        "watch",
        "doctor",
        "backup",
        "restore",
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
