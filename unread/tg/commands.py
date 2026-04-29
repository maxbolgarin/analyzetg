"""CLI command implementations for Telegram navigation and subscriptions."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.models import Subscription
from unread.tg.client import (
    _chat_kind,
    build_client,
    entity_id,
    entity_title,
    entity_username,
    tg_client,
)
from unread.tg.dialogs import get_unread_state
from unread.tg.links import parse
from unread.tg.resolver import resolve
from unread.tg.topics import (
    get_full_channel_info,
    get_linked_chat_id,
    list_forum_topics,
)
from unread.util.logging import get_logger

console = Console()
log = get_logger(__name__)


# --------------------------------------------------------------------- init


async def cmd_init(*, scope: str = "full") -> None:
    """Interactive first-run setup: install folder, AI provider, Telegram login.

    Runs as a pipeline of four conditional steps. Each step gates on
    "is this thing already configured?" so re-running `unread init`
    (or `unread tg init`) is always safe and only prompts for what's
    missing:

      1. **Folder pick** — runs iff `~/.unread/install.toml` is absent.
         User picks default / current dir / custom path / exit.
      2. **AI provider + key** — runs iff `scope == "full"`. Always
         offered on re-runs so the user can change providers / add a
         sibling key / replace a stale key; the menu pre-selects a
         "Keep current" row when the active provider already has a
         key, so re-running `unread init` for Telegram alone is one
         Enter press.
      3. **Telegram credentials** — runs iff api_id/api_hash empty AND
         no valid session. Skippable.
      4. **Telethon auth** — runs iff Telegram creds end the run
         populated AND the session is unauthorized.

    `scope` selects which steps fire. `"full"` (the default, used by
    `unread init`) runs every step. `"telegram_only"` (used by
    `unread tg init`) skips the AI-provider step — convenient when
    the user just wants to (re-)link a Telegram account without
    revisiting credential prompts.

    Persistence is per-step (each value lands in `data.sqlite::secrets`
    immediately) and `reset_settings()` is called between steps so the
    in-memory singleton picks up freshly-written values.
    """
    from unread.config import reset_settings
    from unread.core.paths import (
        default_session_path,
        ensure_unread_home,
        install_pointer_path,
        storage_dir,
    )

    # Step 1: folder pick (only on first run).
    if not install_pointer_path().is_file():
        if not _run_folder_step():
            # User chose "Exit". Bail without writing anything.
            return
        # Folder choice changes `unread_home()` → reload settings so
        # `data_path` / `session_path` resolve to the new location.
        reset_settings()

    # Make sure the storage dir exists for the data DB write below.
    ensure_unread_home()
    storage_dir().mkdir(parents=True, exist_ok=True)

    settings = get_settings()

    # Step 2: AI provider + its API key.
    # Always offered on full-scope re-runs so the user can change
    # providers, add a sibling key (e.g. OpenAI for Whisper alongside
    # Anthropic for chat), or replace a stale key. The provider menu
    # has a "Keep current" entry preselected when a key is already
    # set, so re-running `unread init` to refresh Telegram alone is
    # one Enter press. `tg init` (the telegram-only scope) bypasses
    # this step entirely.
    if scope == "full":
        await _run_provider_step()
        reset_settings()
        settings = get_settings()

    # Step 3: Telegram credentials (skippable). Skip the prompt entirely
    # when an authorized session already exists — the prior install must
    # have had api_id/hash, and there's no need to ask again to re-auth.
    session_path = default_session_path()
    session_present = session_path.exists() or session_path.with_name(session_path.name + ".session").exists()
    if (
        not (settings.telegram.api_id and settings.telegram.api_hash)
        and not session_present
        and await _run_telegram_creds_step()
    ):
        reset_settings()
        settings = get_settings()

    # Step 4: Telethon auth (only if credentials are populated).
    if settings.telegram.api_id and settings.telegram.api_hash:
        await _run_telethon_auth_step(settings)

    # Step 5: optional credential-store hardening — offer to move
    # already-saved keys into the OS keychain. Skipped silently when
    # the host doesn't have a usable keychain (e.g. headless Linux),
    # already-migrated installs, or installs with no secrets yet.
    _run_keychain_step()


# ----- wizard steps -----------------------------------------------------


def _run_folder_step() -> bool:
    """Prompt for the install folder. Returns False on "Exit" / Esc."""
    from pathlib import Path as _Path

    from unread.core.paths import write_install_pointer
    from unread.util.prompt import Choice, ask_text, select

    default_home = _Path.home() / ".unread"
    cwd = _Path.cwd()
    console.print("\n[bold]Welcome to unread.[/]\n")
    console.print("[grey70]Pick where to keep your data — analyses, cache, downloaded media.[/]\n")

    choice = select(
        "Where would you like unread to store its data (storage, reports, cache)?",
        choices=[
            Choice("default", f"Default — {default_home}/"),
            Choice("cwd", f"Current folder — {cwd}/", "creates ./storage and ./reports here"),
            Choice("custom", "Custom path…"),
            Choice("exit", "Exit"),
        ],
        default_value="default",
    )
    if choice in (None, "exit"):
        console.print("[grey70]Setup cancelled. Run `unread init` again when ready.[/]")
        return False
    if choice == "default":
        write_install_pointer(None)
        console.print(f"[green]✓[/] Using default: {default_home}/")
        return True
    if choice == "cwd":
        write_install_pointer(cwd)
        console.print(f"[green]✓[/] Using current folder: {cwd}/")
        return True
    # Custom path: free-form text, with re-prompt on bad / unwritable input.
    while True:
        raw = ask_text("Custom path", default="")
        if raw is None:
            console.print("[grey70]Setup cancelled.[/]")
            return False
        raw = raw.strip()
        if not raw:
            console.print("[yellow]Empty path — try again, or Esc to cancel.[/]")
            continue
        target = _Path(raw).expanduser().resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            console.print(f"[red]Can't create {target}: {e}. Try again.[/]")
            continue
        write_install_pointer(target)
        console.print(f"[green]✓[/] Using custom path: {target}/")
        return True


def _active_provider_has_key(settings) -> bool:  # type: ignore[no-untyped-def]
    """True iff the currently-selected AI provider has a usable API key.

    For "local" the key is optional (most local servers don't check it),
    so this returns True as long as the provider is selected — the
    wizard's provider step is what writes the choice.
    """
    name = (settings.ai.provider or "openai").strip().lower()
    if name == "openai":
        return bool(settings.openai.api_key)
    if name == "openrouter":
        return bool(settings.openrouter.api_key)
    if name == "anthropic":
        return bool(settings.anthropic.api_key)
    if name == "google":
        return bool(settings.google.api_key)
    # Local mode is "configured" once `ai.provider == "local"` is
    # persisted — the base_url has a sensible default and most
    # servers don't enforce a key. Unknown provider names fall through
    # to False so the wizard re-prompts.
    return name == "local"


# Provider-selection menu metadata. Keep in sync with `unread.ai.providers`.
# Each entry: (numeric label, display name, secrets-key-or-None, info_url, hint).
_PROVIDER_CHOICES: tuple[tuple[str, str, str | None, str, str], ...] = (
    (
        "1",
        "openai",
        "openai.api_key",
        "https://platform.openai.com/api-keys",
        "Chat + audio (Whisper) + embeddings + vision. The only provider with audio.",
    ),
    (
        "2",
        "openrouter",
        "openrouter.api_key",
        "https://openrouter.ai/keys",
        "Many models via one key. Chat + image (model-dependent). No audio.",
    ),
    (
        "3",
        "anthropic",
        "anthropic.api_key",
        "https://console.anthropic.com/settings/keys",
        "Claude (Sonnet / Haiku). Chat + image / file. No audio / embeddings.",
    ),
    (
        "4",
        "google",
        "google.api_key",
        "https://aistudio.google.com/app/apikey",
        "Gemini (2.5 Flash / Flash Lite). Chat + image / file. No audio.",
    ),
    (
        "5",
        "local",
        None,  # no API key — base_url + (optional) placeholder key
        "",
        "Self-hosted (Ollama / LM Studio / vLLM). Chat + image if the model supports it.",
    ),
)


async def _run_provider_step() -> None:
    """Pick a chat provider, persist the choice, and collect its API key.

    Arrow-driven: ↑/↓ navigate, Enter selects, Esc raises (cancels the
    whole wizard — Esc = Ctrl-C). On re-runs, the menu offers a "Keep
    current" row at the top, pre-selected when the active provider
    already has a key — so the common case (re-running `unread init`
    to relink Telegram) is one Enter away. Persists the provider
    choice to `app_settings::ai.provider` and the key to
    `data.sqlite::secrets`.
    """
    from unread.util.prompt import Choice, ask_text, confirm, select, separator

    console.print(
        "\n[bold]Audio transcription (Whisper) is OpenAI-only.[/]\n"
        "[grey70]The other providers handle chat + image / file analysis "
        "(image support depends on the chosen model). You can pick OpenAI now "
        "and switch later, or pick another and add an OpenAI key alongside via "
        "`unread init` if you need transcription.[/]\n"
    )

    settings = get_settings()
    current_provider = (settings.ai.provider or "openai").strip().lower()
    has_key_now = _active_provider_has_key(settings)

    provider_choices: list = []
    if has_key_now:
        # Re-run path: let the user keep what's already wired up by
        # pressing Enter on the first row.
        provider_choices.append(
            Choice(
                "__keep__", f"Keep current — {current_provider} (key set)", "press Enter to skip this step"
            )
        )
        provider_choices.append(separator())
    provider_choices.extend(Choice(name, name, hint) for _num, name, _key, _url, hint in _PROVIDER_CHOICES)
    chosen = select(
        "Which AI provider do you want to use?",
        choices=provider_choices,
        default_value="__keep__" if has_key_now else current_provider,
    )
    if chosen == "__keep__":
        console.print(f"[grey70]Keeping current provider: {current_provider}.[/]")
        return
    match = next((c for c in _PROVIDER_CHOICES if c[1] == chosen), None)
    if match is None:
        return  # defensive — select() guarantees we got a known value
    _num, provider_name, secret_key, info_url, _hint = match

    async with open_repo(settings.storage.data_path) as repo:
        await repo.set_app_setting("ai.provider", provider_name)
        if provider_name == "local":
            current = settings.local.base_url
            url_in = ask_text(
                f"Local server base URL (default: {current})",
                default="",
            )
            url = (url_in or "").strip()
            if url:
                await repo.set_app_setting("local.base_url", url)
            console.print("[green]✓[/] Local provider configured — no API key needed.")
            return

        # If the picked provider already has a key stored, ask before
        # overwriting it — common case when the user picked the same
        # provider again just to look around.
        existing_key = _provider_key_value(settings, provider_name)
        if existing_key and not confirm(
            f"{provider_name} already has a key. Replace it?",
            default=False,
        ):
            console.print(f"[grey70]Kept existing {provider_name} key.[/]")
            return

        prompt_label = f"{provider_name} API key"
        if info_url:
            console.print(f"\n[bold]{prompt_label}[/] ([grey70]{info_url}[/])")
        else:
            console.print(f"\n[bold]{prompt_label}[/]")
        console.print(
            "  [grey70]Press Enter to skip — `dump`, `describe`, `sync`, etc. still work without it.[/]"
        )
        raw_in = ask_text("Key", default="", password=True)
        raw = (raw_in or "").strip()
        if not raw:
            console.print(
                "[grey70]Skipped — `analyze` / `ask` will require an AI key. "
                "Re-run `unread init` to add one later.[/]"
            )
            return
        assert secret_key is not None  # guaranteed by _PROVIDER_CHOICES
        await repo.put_secrets({secret_key: raw})
    console.print("[green]✓[/] Saved.")

    # Smoke test only for OpenAI (cheap `models.list()`). Other providers'
    # smoke is the first real chat completion.
    if provider_name == "openai":
        await _smoke_test_openai(raw)


def _provider_key_value(settings, name: str) -> str:  # type: ignore[no-untyped-def]
    """Return the stored key for `name`, or empty string if unset."""
    name = name.strip().lower()
    if name == "openai":
        return settings.openai.api_key or ""
    if name == "openrouter":
        return settings.openrouter.api_key or ""
    if name == "anthropic":
        return settings.anthropic.api_key or ""
    if name == "google":
        return settings.google.api_key or ""
    return ""


async def _smoke_test_openai(api_key: str) -> None:
    """1-token validation of an OpenAI key. Failures don't undo the save."""
    settings = get_settings()
    console.print("  Running OpenAI smoke test…")
    try:
        from openai import AsyncOpenAI

        oai = AsyncOpenAI(api_key=api_key, timeout=settings.openai.request_timeout_sec)
        await asyncio.wait_for(oai.models.list(), timeout=15)
        console.print("[green]✓[/] OpenAI smoke test passed.")
    except Exception as e:
        console.print(
            f"[yellow]OpenAI smoke test failed: {e}.[/] "
            "[grey70]Saved anyway — you can re-run `unread tg init` to update.[/]"
        )


async def _run_telegram_creds_step() -> bool:
    """Optional Telegram-credentials prompt. Returns True if creds were saved."""
    from unread.util.prompt import ask_text, confirm

    console.print("\n[bold]Telegram login (optional).[/]")
    console.print(
        "[grey70]Needed to analyze chats / channels / groups. Skip if you only want "
        "YouTube, web pages, or local files.[/]\n"
    )
    if not confirm("Set up Telegram login now?", default=False):
        console.print(
            "[grey70]Skipped — Telegram-based commands (describe, dump, sync, …) "
            "won't be available. Re-run `unread init` (or `unread tg init`) to add credentials later.[/]"
        )
        return False

    api_id = 0
    while not api_id:
        raw = ask_text(
            "Telegram api_id (https://my.telegram.org → API development tools)",
            default="",
        )
        if raw is None:
            console.print("[grey70]Cancelled — Telegram credentials not saved.[/]")
            return False
        api_id_raw = raw.strip()
        if not api_id_raw:
            console.print("[yellow]api_id is required — try again, or Esc to bail.[/]")
            continue
        try:
            api_id = int(api_id_raw)
        except ValueError:
            console.print("[yellow]api_id must be an integer (digits only).[/]")
            api_id = 0

    api_hash = ""
    while not api_hash:
        raw = ask_text("Telegram api_hash", default="", password=True)
        if raw is None:
            console.print("[grey70]Cancelled — Telegram credentials not saved.[/]")
            return False
        api_hash = raw.strip()
        if not api_hash:
            console.print("[yellow]api_hash is required — try again, or Esc to bail.[/]")

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        await repo.put_secrets({"telegram.api_id": str(api_id), "telegram.api_hash": api_hash})
    console.print("[green]✓[/] Saved.")
    return True


def _run_keychain_step() -> None:
    """Offer to move saved credentials from data.sqlite into the OS keychain.

    Default-yes on macOS / Windows (where the native store is reliable
    and unlocked when the user logs in), default-no on Linux (Secret
    Service requires a session bus and is missing on headless boxes).
    Silent no-op when:
      - we're not on a real TTY (tests, scripted invocations) — the
        offer requires interactive consent and silently migrating is
        worse UX than not offering;
      - the active backend is already ``keychain`` (idempotent re-runs);
      - the keychain backend is unavailable on this host;
      - no slots are populated yet.
    """
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        keychain_available,
        keychain_describe,
        read_active_backend_sync,
    )
    from unread.util.prompt import _can_interact

    if not _can_interact():
        return

    settings = get_settings()
    db_path = settings.storage.data_path
    backend = read_active_backend_sync(db_path)
    if backend == BACKEND_KEYCHAIN:
        return
    if not keychain_available():
        return
    # Probe for any populated secret. The function is sync and cheap,
    # so don't open an aiosqlite connection just for the count.
    from unread.db.repo import read_data_db_secrets_sync

    rows = read_data_db_secrets_sync(db_path)
    if not any((rows.get(k) or "") for k in rows):
        return

    from unread.util.prompt import confirm

    console.print("\n[bold]Secure storage (optional).[/]")
    console.print(
        f"  [grey70]Your saved API keys can be moved into the {keychain_describe()} "
        "(encrypted at rest, unlocked when you log in). The on-disk DB row gets blanked.[/]"
    )
    # We've already passed `keychain_available()`, which means the OS
    # store responded to a probe call. Recommend it across every
    # platform that gets this far — including Linux desktops with a
    # running Secret Service. Headless Linux / containers don't reach
    # this prompt at all (the gate above returns early).
    if not confirm(
        "Move credentials into the system keychain now?",
        default=True,
    ):
        console.print(
            "[grey70]Skipped — credentials stay in the data DB. "
            "Run `unread security migrate --to keychain` later to change your mind.[/]"
        )
        return

    from unread.security.commands import cmd_migrate

    try:
        cmd_migrate(BACKEND_KEYCHAIN)
    except typer.Exit:
        # `cmd_migrate` already printed a useful error message before
        # raising; swallow the Exit so the wizard finishes cleanly
        # rather than aborting the user's whole setup over one
        # keychain hiccup.
        return


async def _run_telethon_auth_step(settings) -> None:  # type: ignore[no-untyped-def]
    """Run Telethon's interactive login if the session isn't already authorized."""
    from unread.util.prompt import ask_text as _ask_text

    client = build_client(settings)

    def _phone() -> str:
        # Telethon expects a non-empty string; Esc returns None which we
        # surface as an empty string so Telethon's own validation kicks
        # in and re-asks.
        return _ask_text(_t("init_phone_prompt")) or ""

    def _code() -> str:
        return _ask_text(_t("init_login_code_prompt")) or ""

    def _password() -> str:
        # Telethon retries this callback on PasswordHashInvalidError,
        # so a wrong 2FA password only reprompts the 2FA step.
        return _ask_text(_t("init_2fa_prompt"), password=True) or ""

    await client.connect()
    try:
        if await client.is_user_authorized():
            console.print(f"[green]{_t('doctor_session_authorized')}[/]")
        else:
            await client.start(phone=_phone, code_callback=_code, password=_password)
            console.print(f"[green]{_t('doctor_logged_in')}[/]")
    finally:
        await client.disconnect()


# --------------------------------------------------------------------- doctor


async def cmd_doctor() -> None:
    """Run a battery of health checks and print a per-line status report.

    No mutations, no expensive calls: each check has a hard cap on time/cost.
    Designed so a user pasting the output into a bug report is enough for
    triage.
    """
    import os
    import shutil
    from pathlib import Path as _Path

    settings = get_settings()
    ok = "[green]OK[/]"
    warn = "[yellow]WARN[/]"
    fail = "[red]FAIL[/]"
    statuses: list[str] = []

    def _line(status: str, label: str, detail: str = "") -> None:
        console.print(f"  {status:<24} {label}{(' — ' + detail) if detail else ''}")
        statuses.append(status)

    console.print(f"[bold]{_t('tg_doctor_banner')}[/]")

    # Header: version + Python so a pasted bug report carries enough
    # context to pin down regressions.
    import platform as _platform

    from unread import __version__ as _unread_version

    _line(ok, "unread version", _unread_version)
    _line(ok, "python", f"{_platform.python_version()} on {_platform.platform()}")

    # 1. Config files (resolved under ~/.unread/, override via UNREAD_HOME / UNREAD_CONFIG_PATH)
    from unread.core.paths import default_config_path, default_env_path, unread_home

    env_path = default_env_path()
    cfg_path = _Path(os.environ.get("UNREAD_CONFIG_PATH") or default_config_path())
    if env_path.exists():
        _line(ok, ".env present", str(env_path))
    else:
        _line(warn, ".env missing", f"expected at {env_path}")
    if cfg_path.exists():
        _line(ok, "config.toml present", str(cfg_path))
    else:
        _line(warn, "config.toml missing", f"expected at {cfg_path}")

    # Legacy install detection — surface a single actionable hint when the
    # user upgraded but their old cwd-relative install is still around.
    home = unread_home()
    cwd = _Path.cwd()
    legacy_data = cwd / "storage" / "data.sqlite"
    legacy_env = cwd / ".env"
    if not home.exists() and (legacy_data.exists() or legacy_env.exists()):
        _line(
            warn,
            "legacy install detected",
            f"found {cwd}/storage or {cwd}/.env — run `unread migrate` to move into {home}",
        )

    # Install-pointer drift — `~/.unread/install.toml` says the data
    # lives in a directory that's no longer there. Without this hint
    # the user gets a fresh, empty DB on next run.
    from unread.core.paths import install_pointer_drift

    has_drift, drift_hint = install_pointer_drift()
    if has_drift:
        _line(fail, "install-pointer drift", drift_hint)

    # 2. Secrets resolved
    if settings.telegram.api_id and settings.telegram.api_hash:
        # Don't print the api_id value — it's a stable account fingerprint
        # and shows up in pasted bug reports / screenshots. The presence
        # check is enough for triage.
        _line(ok, "telegram credentials", "api_id + api_hash present")
    else:
        _line(fail, "telegram credentials missing", "set TELEGRAM_API_ID / TELEGRAM_API_HASH in .env")
    if settings.openai.api_key:
        _line(ok, "OPENAI_API_KEY present")
    else:
        _line(fail, "OPENAI_API_KEY missing", "set in .env")

    # 3. ffmpeg
    ffmpeg_path = shutil.which(settings.media.ffmpeg_path) or shutil.which("ffmpeg")
    if ffmpeg_path:
        _line(ok, "ffmpeg on PATH", ffmpeg_path)
    else:
        _line(
            warn,
            "ffmpeg not found",
            "voice/videonote/video enrichment will skip; install ffmpeg or set [media] ffmpeg_path",
        )

    # 3b. yt-dlp (YouTube analysis)
    try:
        import yt_dlp  # type: ignore[import-not-found]

        _line(ok, "yt-dlp installed", getattr(yt_dlp, "__version__", "?"))
    except ImportError:
        _line(
            warn,
            "yt-dlp not installed",
            "`unread analyze <youtube-url>` will fail; run `uv sync` to install",
        )

    # 4. Storage paths + disk
    storage_dir = settings.storage.data_path.parent
    if storage_dir.exists():
        try:
            usage = shutil.disk_usage(storage_dir)
            free_gb = usage.free / 1024**3
            if free_gb < 0.5:
                _line(fail, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
            elif free_gb < 5.0:
                _line(warn, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
            else:
                _line(ok, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
        except OSError as e:
            _line(warn, "disk usage check failed", str(e)[:100])
    else:
        _line(warn, "storage dir missing", f"will be created on first write: {storage_dir}")

    # 5. DB integrity + size
    db_path = settings.storage.data_path
    if db_path.exists():
        try:
            from unread.db.repo import open_repo as _open_repo

            async with _open_repo(db_path) as repo:
                cur = await repo._conn.execute("PRAGMA integrity_check")
                row = await cur.fetchone()
                await cur.close()
                verdict = (row["integrity_check"] if row else "?") if row is not None else "?"
                # Cache-size advisory — daily users accumulate hundreds of
                # MB in `analysis_cache` over months. We don't auto-trim
                # here (no surprise destructive ops) but surface a hint
                # the moment it becomes worth running `unread cache trim`.
                cache_summary = await repo.cache_stats()
            size_mb = db_path.stat().st_size / 1024**2
            if verdict == "ok":
                _line(ok, "DB integrity", f"{db_path} ({size_mb:.1f} MB)")
            else:
                _line(fail, "DB integrity check failed", str(verdict)[:200])
            cache_rows = int(cache_summary.get("rows") or 0)
            cache_bytes = int(cache_summary.get("result_bytes") or 0)
            if cache_bytes > 100 * 1024 * 1024:  # 100 MB
                _line(
                    warn,
                    "analysis cache large",
                    f"{cache_rows} rows / {cache_bytes / 1024**2:.0f} MB — "
                    "consider `unread cache trim --keep-days 30`",
                )
            elif cache_rows > 0:
                _line(ok, "analysis cache size", f"{cache_rows} rows / {cache_bytes / 1024**2:.1f} MB")
        except Exception as e:
            _line(fail, "DB open failed", str(e)[:200])
    else:
        _line(warn, "DB not yet created", str(db_path))

    # 5b. Write permissions on storage dir. Catches the case where the
    # user's `~/.unread/storage/` ended up read-only (sudo install,
    # restored from a backup with wrong perms, etc) — every write would
    # silently fail with the same opaque "attempt to write a readonly
    # database" message, so flag it up front.
    storage_dir = settings.storage.data_path.parent
    if storage_dir.exists():
        sentinel = storage_dir / ".doctor_write_test"
        try:
            sentinel.write_bytes(b"")
            sentinel.unlink()
            _line(ok, "storage writable", str(storage_dir))
        except OSError as e:
            _line(
                fail,
                "storage not writable",
                f"{storage_dir} — {e.strerror or str(e)[:80]}; chmod / chown the directory",
            )

    # 5c. Storage permissions hardening. The DB is unencrypted; the only
    # protection against another user on the same workstation reading
    # secrets is filesystem mode. `ensure_unread_home()` chmods 0o700 on
    # init, but pre-existing installs may carry 0o755. Skip on Windows
    # (POSIX mode bits don't translate cleanly).
    if os.name == "posix" and storage_dir.exists():
        try:
            mode = storage_dir.stat().st_mode & 0o777
            db_mode = db_path.stat().st_mode & 0o777 if db_path.exists() else None
            problems: list[str] = []
            if mode & 0o077:  # group or other have any access
                problems.append(f"dir mode {oct(mode)} — expect 0o700")
            if db_mode is not None and (db_mode & 0o077):
                problems.append(f"data.sqlite mode {oct(db_mode)} — expect 0o600")
            if problems:
                fix_cmd = f"chmod 700 {storage_dir} && chmod 600 {db_path}"
                _line(
                    warn,
                    "storage permissions overpermissive",
                    "; ".join(problems) + f" — fix: {fix_cmd}",
                )
            else:
                _line(ok, "storage permissions", "0o700 / 0o600 (private)")
        except OSError as e:
            _line(warn, "storage permission check failed", str(e)[:100])

    # 5d. Backup-path guard. The DB is plaintext; if `~/.unread/` is
    # parented under a synced cloud directory (iCloud Drive, Dropbox,
    # OneDrive, Google Drive) every saved API key and chat message
    # ends up replicated off-device. Surface as a warn — some users
    # genuinely want this (e.g. multi-machine personal use), so we
    # don't fail.
    try:
        from unread.core.paths import unread_home

        home_path = unread_home().resolve()
        home_str = str(home_path)
        sync_parents: list[tuple[str, str]] = [
            ("iCloud Drive", str((_Path.home() / "Library/Mobile Documents/com~apple~CloudDocs").resolve())),
            ("Dropbox", str((_Path.home() / "Dropbox").resolve())),
            ("Google Drive", str((_Path.home() / "Google Drive").resolve())),
            ("OneDrive", str((_Path.home() / "OneDrive").resolve())),
        ]
        hits = [
            name
            for name, prefix in sync_parents
            if home_str.startswith(prefix + os.sep) or home_str == prefix
        ]
        if hits:
            advice = (
                f"under {', '.join(hits)} — plaintext secrets and chat cache will be replicated. "
                "Move the install (`unread tg init` lets you pick a folder) "
                "or exclude it from the sync app."
            )
            if sys.platform == "darwin":
                advice += f" On macOS: `tmutil addexclusion {home_path}` keeps it out of Time Machine."
            _line(warn, "install under cloud sync", advice)
        else:
            _line(ok, "install location", f"{home_path} (not under a known sync folder)")
    except Exception as e:
        _line(warn, "install-path check failed", str(e)[:100])

    # 5e. Full-disk encryption hint. Best-effort, posix only — the
    # signal isn't reliable across distros, so a missing FDE result
    # is a warn, not a fail. The intent is to remind users that
    # filesystem permissions don't help once a disk is read on
    # another machine.
    import subprocess as _subprocess

    if sys.platform == "darwin":
        try:
            res = _subprocess.run(
                ["fdesetup", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            out = (res.stdout or "").strip()
            if "FileVault is On" in out:
                _line(ok, "FileVault", "On (disk encrypted at rest)")
            elif "FileVault is Off" in out:
                _line(
                    warn,
                    "FileVault disabled",
                    "disk is not encrypted — turn on in System Settings → Privacy & Security → FileVault",
                )
            else:
                _line(warn, "FileVault status unknown", out[:80] or "fdesetup gave no output")
        except (OSError, _subprocess.SubprocessError) as e:
            _line(warn, "FileVault check failed", str(e)[:100])
    elif sys.platform.startswith("linux"):
        try:
            crypttab = _Path("/etc/crypttab")
            if crypttab.is_file() and any(
                line.strip() and not line.strip().startswith("#")
                for line in crypttab.read_text().splitlines()
            ):
                _line(ok, "disk encryption", "/etc/crypttab has entries (likely LUKS)")
            else:
                _line(
                    warn,
                    "disk encryption status unknown",
                    "no /etc/crypttab entries found — confirm LUKS / dm-crypt is enabled if you carry this disk",
                )
        except OSError as e:
            _line(warn, "disk encryption check failed", str(e)[:100])

    # 6. Telegram session liveness
    session_path = settings.telegram.session_path
    # Telethon appends `.session` when the configured path doesn't already
    # end with it, so check both forms before declaring the file missing.
    session_with_suffix = session_path.with_name(session_path.name + ".session")
    session_present = session_path.exists() or session_with_suffix.exists()
    actual_session = session_path if session_path.exists() else session_with_suffix
    if not session_present:
        _line(warn, "Telegram session missing", f"run `unread init` (expected {session_path})")
    elif settings.telegram.api_id and settings.telegram.api_hash:
        try:
            client = build_client(settings)
            await asyncio.wait_for(client.connect(), timeout=10)
            try:
                authorized = await client.is_user_authorized()
            finally:
                await client.disconnect()
            if authorized:
                _line(ok, "Telegram session", f"authorized ({actual_session})")
            else:
                _line(fail, "Telegram session", "not authorized — run `unread init`")
        except Exception as e:
            _line(warn, "Telegram session check failed", str(e)[:200])

    # 7. OpenAI key liveness — special-cased because OpenAI also backs
    # Whisper / vision / embeddings even when the chat provider is
    # something else.
    if settings.openai.api_key:
        try:
            from openai import AsyncOpenAI

            oai = AsyncOpenAI(
                api_key=settings.openai.api_key,
                timeout=settings.openai.request_timeout_sec,
            )
            await asyncio.wait_for(oai.models.list(), timeout=10)
            _line(ok, "OpenAI API reachable")
        except Exception as e:
            _line(warn, "OpenAI API check failed", str(e)[:200])

    # 7b. Active chat provider reachability. If the user picked
    # Anthropic / Google / OpenRouter / local, ping that endpoint too.
    # We don't burn an LLM call — just instantiate the provider, which
    # validates the key + base URL via the SDK constructor.
    chat_provider = (settings.ai.provider or "openai").lower()
    if chat_provider != "openai":
        try:
            from unread.ai.providers import make_chat_provider

            make_chat_provider(settings)
            _line(ok, "chat provider configured", chat_provider)
        except Exception as e:
            _line(
                fail,
                f"chat provider {chat_provider!r} not configured",
                str(e)[:200],
            )

    # 8. Presets
    try:
        from unread.analyzer.prompts import PRESETS

        if PRESETS:
            _line(ok, "presets loaded", f"{len(PRESETS)} ({', '.join(sorted(PRESETS))})")
        else:
            _line(warn, "no presets loaded", "expected presets/*.md")
    except Exception as e:
        _line(fail, "preset load failed", str(e)[:200])

    # 9. Pricing coverage — chat AND audio. Missing the audio entry was
    # invisible until now: voice transcription would silently drop cost
    # accounting on `unread stats`.
    pricing = settings.pricing
    chat_referenced = {
        settings.openai.chat_model_default,
        settings.openai.filter_model_default,
        settings.enrich.vision_model,
    }
    chat_referenced.discard(None)
    chat_missing = [m for m in chat_referenced if m and m not in pricing.chat]
    audio_model = settings.openai.audio_model_default
    audio_missing = bool(audio_model and audio_model not in pricing.audio)
    if chat_missing or audio_missing:
        bits: list[str] = []
        if chat_missing:
            bits.append(f'[pricing.chat."{chat_missing[0]}"]')
        if audio_missing:
            bits.append(f'[pricing.audio]."{audio_model}"')
        _line(
            warn,
            "pricing entries missing",
            f"add {' / '.join(bits)} to config.toml; cost stats will under-report",
        )
    else:
        _line(ok, "pricing covers default models")

    # Summary
    fails = sum(1 for s in statuses if "FAIL" in s)
    warns = sum(1 for s in statuses if "WARN" in s)
    if fails:
        console.print(f"[bold red]{_tf('doctor_summary_failed', fails=fails, warns=warns)}[/]")
        raise typer.Exit(1)
    if warns:
        console.print(f"[bold yellow]{_tf('doctor_summary_warned', warns=warns)}[/]")
    else:
        console.print(f"[bold green]{_t('doctor_all_ok')}[/]")


# ------------------------------------------------------------------- dialogs


async def cmd_dialogs(search: str | None, kind: str | None, limit: int) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        table = Table(title="Telegram dialogs", show_lines=False)
        for col in ("id", "kind", "title", "username", "unread"):
            table.add_column(col)

        shown = 0
        async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
            entity = d.entity
            k = _chat_kind(entity)
            t = entity_title(entity)
            u = entity_username(entity)
            if kind and k != kind:
                continue
            if search:
                hay = f"{t or ''} {u or ''}".lower()
                if search.lower() not in hay:
                    continue
            await repo.upsert_chat(entity_id(entity), k, title=t, username=u)
            table.add_row(
                str(entity_id(entity)),
                k,
                t or "",
                f"@{u}" if u else "",
                str(getattr(d, "unread_count", 0)),
            )
            shown += 1
            if shown >= limit:
                break
        console.print(table)
        console.print(f"[grey70]{_tf('tg_n_rows', n=shown)}[/]")


# ------------------------------------------------------------------- describe


DEFAULT_KINDS = ("forum", "supergroup", "group")


async def cmd_describe(
    ref: str | None,
    *,
    kind: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    show_all: bool = False,
) -> None:
    """Overview of dialogs, or details about one chat.

    With no ref and no filter flags, opens an interactive picker so you
    can choose a chat and see its details. With filter flags (--all /
    --kind / --search / --limit) or a ref, behaves non-interactively.
    """
    # No ref and no filters → interactive chat picker.
    has_filters = bool(kind or search or limit or show_all)
    if ref is None and not has_filters:
        from unread.interactive import run_interactive_describe

        await run_interactive_describe()
        return

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        if ref is None:
            await _describe_overview(
                client,
                repo,
                kind=kind,
                search=search,
                limit=limit,
                show_all=show_all,
            )
            return
        await _describe_one(client, repo, ref)


async def _describe_overview(
    client,
    repo,
    *,
    kind: str | None,
    search: str | None,
    limit: int | None,
    show_all: bool,
) -> None:
    from unread.tg.folders import chat_folder_index

    console.print(f"[grey70]{_t('tg_listing_dialogs')}[/]")
    folder_idx = await chat_folder_index(client)
    rows: list[tuple] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = d.entity
        k = _chat_kind(entity)
        t = entity_title(entity)
        u = entity_username(entity)
        eid = entity_id(entity)
        unread = int(getattr(d, "unread_count", 0) or 0)

        # Apply filters BEFORE hitting the DB — saves N queries.
        if kind and k != kind:
            continue
        if search:
            hay = f"{t or ''} {u or ''}".lower()
            if search.lower() not in hay:
                continue
        if not show_all:
            if k not in DEFAULT_KINDS:
                continue
            if unread <= 0:
                continue

        stats = await repo.chat_stats(eid)
        folders_str = ", ".join(folder_idx.get(eid, []))
        rows.append((unread, eid, k, t or "", u or "", stats["count"], stats["date_max"], folders_str))

    rows.sort(key=lambda r: (-r[0], -r[5]))
    if limit:
        rows = rows[:limit]

    # Title hint reflects the filter state.
    desc_parts = []
    if show_all:
        desc_parts.append("all")
    else:
        desc_parts.append("unread")
        if kind is None:
            desc_parts.append("forums/groups/supergroups")
    if kind:
        desc_parts.append(f"kind={kind}")
    if search:
        desc_parts.append(f"search={search!r}")
    title = "Dialogs (" + ", ".join(desc_parts) + ")"

    table = Table(title=title)
    for col in ("id", "kind", "title", "username", "unread", "stored", "last_msg", "folder"):
        table.add_column(col)
    for unread, eid, k, t, u, stored, dmax, folders_str in rows:
        table.add_row(
            str(eid),
            k,
            t,
            f"@{u}" if u else "",
            str(unread) if unread else "",
            str(stored) if stored else "",
            dmax.strftime("%Y-%m-%d %H:%M") if dmax else "",
            folders_str,
        )
    console.print(table)
    hint_parts = [_tf("tg_n_rows", n=len(rows))]
    if not show_all:
        hint_parts.append(_t("tg_dialogs_default_filter"))
        hint_parts.append(_t("tg_dialogs_pass_all"))
    console.print(f"[grey70]{'. '.join(hint_parts)}.[/]")
    console.print(f"[grey70]{_t('tg_describe_hint')}[/]")


async def _describe_one(client, repo, ref: str) -> None:
    resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
    chat_id = resolved.chat_id
    kind = resolved.kind

    # Pull live dialog-level state (unread, read marker, last message date).
    unread_count, read_marker = await get_unread_state(client, chat_id)
    last_msg_date = await _fetch_last_msg_date(client, chat_id)

    # Header
    badge = f"[bold]{resolved.title or chat_id}[/]"
    console.print(f"\n{badge} [grey70]{_tf('tg_describe_id_kind', chat_id=chat_id, kind=kind)}[/]")

    # --- Left/right-ish labeled properties
    def _row(label: str, value: str | None, *, dim_label: bool = True) -> None:
        if value is None or value == "":
            return
        label_fmt = f"[grey70]{label:>14}:[/]" if dim_label else f"{label:>14}:"
        console.print(f"  {label_fmt} {value}")

    if resolved.username:
        _row("username", f"@{resolved.username} — https://t.me/{resolved.username}")
    # Telegram folders the chat is explicitly listed in (rule-based folders
    # are not expanded — see tg/folders.py).
    try:
        from unread.tg.folders import chat_folder_index

        idx = await chat_folder_index(client)
        folders_for_chat = idx.get(chat_id, [])
        if folders_for_chat:
            _row("folder", ", ".join(folders_for_chat))
    except Exception as e:
        log.debug("describe.folder_lookup_failed", err=str(e)[:100])
    _row("unread", str(unread_count) if unread_count else None)
    _row(
        "read marker",
        f"msg_id > {read_marker}" if read_marker and unread_count else None,
    )
    if last_msg_date:
        _row("last message", last_msg_date.strftime("%Y-%m-%d %H:%M"))

    # Channel/supergroup/forum extended info
    info: dict = {}
    if kind in ("channel", "supergroup", "forum"):
        try:
            info = await get_full_channel_info(client, chat_id)
        except Exception as e:
            log.warning("describe.full_channel_failed", err=str(e)[:200])
            info = {}

        # Kind details
        type_bits = []
        if info.get("broadcast"):
            type_bits.append("broadcast")
        if info.get("megagroup"):
            type_bits.append("megagroup")
        if info.get("forum"):
            type_bits.append("forum")
        if info.get("verified"):
            type_bits.append("[green]verified[/]")
        if info.get("scam"):
            type_bits.append("[red]scam[/]")
        if info.get("restricted"):
            type_bits.append("[yellow]restricted[/]")
        if type_bits:
            _row("type", " ".join(type_bits))

        # Participants & moderation
        parts = info.get("participants_count")
        online = info.get("online_count")
        if parts is not None:
            val = f"{parts:,}"
            if online:
                val += f" ([green]{online}[/] online)"
            _row("participants", val)
        if info.get("admins_count"):
            _row("admins", str(info["admins_count"]))
        if info.get("banned_count"):
            _row("banned", str(info["banned_count"]))

        # Links, discussion, pin, slowmode
        if info.get("invite_link"):
            _row("invite link", info["invite_link"])
        elif resolved.username:
            pass  # already shown above as username link
        if info.get("linked_chat_id"):
            _row("linked chat", str(info["linked_chat_id"]))
        if info.get("pinned_msg_id"):
            pin_link = _msg_link(resolved.username, chat_id, info["pinned_msg_id"])
            _row("pinned msg", pin_link)
        slow = info.get("slowmode_seconds")
        if slow:
            _row("slow mode", f"{slow}s between messages")

        if info.get("about"):
            # Split "about" on blank lines so long descriptions stay readable.
            first = info["about"].splitlines()[0]
            if len(info["about"]) > 200:
                first = first[:200] + "…"
            _row("about", first)

    # Forums → topics table
    if kind == "forum":
        topics = await list_forum_topics(client, chat_id)
        if topics:
            tt = Table(title=f"Topics ({len(topics)})")
            for col in ("id", "title", "unread", "top_msg", "stored", "closed", "pinned"):
                tt.add_column(col)
            for tp in topics:
                st = await repo.chat_stats(chat_id, thread_id=tp.topic_id)
                tt.add_row(
                    str(tp.topic_id),
                    tp.title,
                    str(tp.unread_count) if tp.unread_count else "",
                    str(tp.top_message or ""),
                    str(st["count"]) if st["count"] else "",
                    "yes" if tp.closed else "",
                    "yes" if tp.pinned else "",
                )
            console.print(tt)

    # Local DB stats
    stats = await repo.chat_stats(chat_id)
    if stats["count"]:
        dmin = stats["date_min"].strftime("%Y-%m-%d %H:%M") if stats["date_min"] else "—"
        dmax = stats["date_max"].strftime("%Y-%m-%d %H:%M") if stats["date_max"] else "—"
        console.print(
            f"\n[bold]Local DB[/]: {stats['count']} message(s), from [cyan]{dmin}[/] to [cyan]{dmax}[/]"
        )
        top = await repo.top_senders(chat_id, limit=5)
        if top:
            console.print(f"[bold]{_t('tg_top_senders_label')}[/]:")
            for row in top:
                console.print(f"  {row['sender_name']} — {row['count']}")
    else:
        console.print(f"\n[grey70]{_t('tg_no_messages_local')}[/]")


async def _fetch_last_msg_date(client, chat_id: int):
    """Fetch the date of the most recent message in the chat. Returns datetime or None."""
    try:
        async for m in client.iter_messages(chat_id, limit=1):
            return getattr(m, "date", None)
    except Exception as e:
        log.debug("describe.last_msg_failed", chat_id=chat_id, err=str(e)[:200])
    return None


def _msg_link(username: str | None, chat_id: int, msg_id: int) -> str:
    """Render a t.me link to a specific message (prefer @username form)."""
    if username:
        return f"{msg_id} — https://t.me/{username}/{msg_id}"
    if chat_id < 0 and abs(chat_id) > 1_000_000_000_000:
        internal = abs(chat_id) - 1_000_000_000_000
        return f"{msg_id} — https://t.me/c/{internal}/{msg_id}"
    return str(msg_id)


# -------------------------------------------------------------------- topics


async def cmd_topics(chat_ref: str) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        ref = await resolve(client, repo, chat_ref, prompt_choice=_tui_choose)
        if ref.kind not in ("forum", "supergroup", "channel"):
            console.print(f"[yellow]{_tf('tg_not_a_forum', title=ref.title)}[/]")
            raise typer.Exit(1)
        topics = await list_forum_topics(client, ref.chat_id)
        t = Table(title=f"Forum topics: {ref.title}")
        t.add_column("id")
        t.add_column("title")
        t.add_column("closed")
        t.add_column("pinned")
        for x in topics:
            t.add_row(str(x.topic_id), x.title, "yes" if x.closed else "", "yes" if x.pinned else "")
        console.print(t)
        console.print(f"[grey70]{_tf('tg_n_topics', n=len(topics))}[/]")


# -------------------------------------------------------------------- resolve


async def cmd_resolve(ref: str) -> None:
    settings = get_settings()
    parsed = parse(ref)
    console.print(f"[bold]{_t('tg_resolve_parsed_label')}[/] {parsed}")
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        try:
            resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
            console.print(f"[bold green]{_t('tg_resolve_done_label')}[/] {resolved}")
        except Exception as e:
            console.print(f"[red]{_t('tg_resolve_failed_label')}[/] {e}")


# --------------------------------------------------------------- channel-info


async def cmd_channel_info(ref: str) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
        info = await get_full_channel_info(client, resolved.chat_id)
        console.print(
            f"[bold]{resolved.title}[/] "
            f"{_tf('tg_describe_id_kind_inline', chat_id=resolved.chat_id, kind=resolved.kind)}"
        )
        console.print(_tf("tg_describe_participants", n=info["participants_count"]))
        console.print(_tf("tg_describe_linked_chat_id", id=info["linked_chat_id"]))
        if info.get("about"):
            console.print(_tf("tg_describe_about", text=info["about"]))


# ------------------------------------------------------------------- chats.*


async def cmd_chats_add(
    *,
    ref: str | None,
    from_date: str | None,
    from_msg: str | None,
    last: int | None,
    full_history: bool,
    thread: int | None,
    all_topics: bool,
    with_comments: bool,
    join: bool,
    no_transcribe: bool,
    preset: str | None = None,
    period: str | None = None,
    enrich: str | None = None,
    no_mark_read: bool = False,
    post_to: str | None = None,
) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        # No ref → interactive picker (any dialog, not just unread). After
        # picking, ask the kind-specific questions inline so a user with
        # no flags ends up with a sensible subscription.
        if ref is None:
            from unread.interactive import _pick_chat as _picker

            # Pre-load the set of already-subscribed chat ids so the
            # picker can flag them with a `★`. Helps the user see at a
            # glance which dialogs are already in `unread chats list`.
            existing_subs = await repo.list_subscriptions(enabled_only=False)
            subscribed_ids = {int(s.chat_id) for s in existing_subs}

            picked = await _picker(
                client,
                offer_all_unread=False,
                subscribed_ids=subscribed_ids,
            )
            if picked is None or not isinstance(picked, dict):
                console.print(f"[grey70]{_t('cancelled')}[/]")
                return
            ref = str(picked["chat_id"])
            if int(picked["chat_id"]) in subscribed_ids:
                console.print(
                    f"[yellow]→ {picked.get('title') or picked['chat_id']} is already "
                    "subscribed.[/] Continuing — re-running `add` is idempotent and lets "
                    "you bolt on extras (e.g. comments / topics) without removing the existing sub."
                )
            # Forum / channel toggles get asked here unless the CLI
            # already set them. Routed through `prompt.confirm` so the
            # keys / styling match every other interactive screen.
            from unread.util.prompt import confirm as _confirm

            kind = picked.get("kind")
            if kind == "channel" and not with_comments:
                # Brief explainer so the choice is informed: comments
                # live in a separate Telegram chat (the linked discussion
                # group). Saying yes here creates a SECOND subscription
                # for that group. `unread chats run` then folds the comments
                # into the same report as the channel — one analysis per
                # channel, not two — see `--with-comments` semantics.
                console.print(
                    "[grey70]→ Channels store posts; user comments live in a "
                    "linked discussion group (a separate Telegram chat). "
                    "Saying yes adds a sibling subscription for that group; "
                    "`unread chats run` will merge channel posts + comments "
                    "into ONE report (not two).[/]"
                )
                with_comments = _confirm(
                    "Also subscribe to this channel's linked discussion group (comments)?",
                    default=True,
                )
            if kind == "forum" and not all_topics and thread is None:
                all_topics = _confirm(
                    "Forum: subscribe to every topic (recommended)?",
                    default=True,
                )
            # `unread chats run` settings: preset, period, enrich, mark_read.
            # These get persisted on the subscription so `unread chats run`
            # can walk every enabled sub and analyze each one with
            # its own settings without re-prompting. Each step skips
            # itself when the matching CLI flag was set.
            #
            # Reuse the analyze wizard's pickers wholesale — same
            # labels, same keybindings (arrow-toggle, Enter, ESC),
            # same defaults — so users don't have to relearn anything
            # between `unread analyze` and `unread chats add`.
            from unread.interactive import BACK as _BACK
            from unread.interactive import (
                _pick_enrich,
                _pick_mark_read,
                _pick_period,
                _pick_preset,
            )

            def _bail() -> None:
                console.print(f"[grey70]{_t('cancelled')}[/]")

            if preset is None:
                picked_preset = await _pick_preset()
                if picked_preset is None or picked_preset is _BACK:
                    _bail()
                    return
                preset = picked_preset
            if period is None:
                # `static_only=True` hides custom-range / from-msg —
                # neither makes sense as a persisted recurring period.
                period_result = await _pick_period(static_only=True)
                if period_result is None or period_result is _BACK:
                    _bail()
                    return
                period = period_result[0]
            if enrich is None:
                enrich_pick = await _pick_enrich()
                if enrich_pick is None or enrich_pick is _BACK:
                    _bail()
                    return
                # Empty list = explicitly "no enrichment". The persisted
                # column is a CSV; "" represents "off everywhere".
                enrich = ",".join(enrich_pick)
                # Legacy `--no-transcribe` keeps mirroring the
                # voice/videonote choice so older log lines stay
                # consistent with the new picker's outcome.
                no_transcribe = "voice" not in enrich_pick and "videonote" not in enrich_pick
            if not no_mark_read:
                mr_result = await _pick_mark_read(default=True)
                if mr_result is None or mr_result is _BACK:
                    _bail()
                    return
                no_mark_read = not bool(mr_result)
            # post_to: where to deliver the report after `unread chats run`
            # analyzes this sub. Three sensible defaults:
            #   - "No"          → save to reports/<chat>/ only
            #   - "Saved Msgs"  → send to your own Telegram Saved Messages
            #   - "Custom"      → text-input for any chat ref (@channel,
            #                     numeric id, t.me link, fuzzy title)
            # Resolution happens at run time via tg/resolver, so any
            # form `--post-to` accepts works here too.
            if post_to is None:
                from unread.util.prompt import Choice as _PromptChoice
                from unread.util.prompt import ask_text as _ask_text
                from unread.util.prompt import select as _select

                post_choice = _select(
                    "After analyze, post the report to a Telegram chat?",
                    choices=[
                        _PromptChoice(value="no", label="No — save to reports/ only"),
                        _PromptChoice(
                            value="me",
                            label="Saved Messages (recommended for personal digests)",
                        ),
                        _PromptChoice(value="custom", label="Custom chat / channel…"),
                    ],
                    default_value="no",
                )
                if post_choice is None:
                    _bail()
                    return
                if post_choice == "me":
                    post_to = "me"
                elif post_choice == "custom":
                    post_ref = _ask_text(
                        "Post-to ref (@channel, t.me link, numeric id, or fuzzy title — blank to skip)",
                        default="",
                    )
                    if post_ref is None:
                        _bail()
                        return
                    post_ref = post_ref.strip()
                    post_to = post_ref or None

        resolved = await resolve(client, repo, ref, join=join, prompt_choice=_tui_choose)

        from_msg_id = _parse_from_msg(from_msg)
        from_dt = datetime.strptime(from_date, "%Y-%m-%d") if from_date else None
        if full_history:
            from_dt = datetime(1970, 1, 1)
            from_msg_id = None

        # Settings that apply to every Subscription built below — keep
        # them DRY so adding a new field doesn't require touching three
        # constructors.
        run_settings = {
            "preset": preset or "summary",
            "period": period or "unread",
            "enrich_kinds": enrich,  # None = config defaults; "" = no enrichment
            "mark_read": not no_mark_read,
            "post_to": post_to,
        }

        # Base subscription for the chat timeline
        subs_to_add: list[Subscription] = []
        base_thread = thread or 0
        base_source = _source_kind_for(resolved.kind)
        base = Subscription(
            chat_id=resolved.chat_id,
            thread_id=base_thread,
            title=resolved.title,
            source_kind=base_source,
            start_from_msg_id=from_msg_id,
            start_from_date=from_dt,
            transcribe_voice=not no_transcribe,
            transcribe_videonote=not no_transcribe,
            transcribe_video=False,
            **run_settings,
        )
        subs_to_add.append(base)

        if all_topics and resolved.kind in ("forum", "supergroup"):
            topics = await list_forum_topics(client, resolved.chat_id)
            for t in topics:
                subs_to_add.append(
                    Subscription(
                        chat_id=resolved.chat_id,
                        thread_id=t.topic_id,
                        title=f"{resolved.title} / {t.title}",
                        source_kind="topic",
                        start_from_msg_id=None,
                        start_from_date=from_dt,
                        transcribe_voice=not no_transcribe,
                        transcribe_videonote=not no_transcribe,
                        transcribe_video=False,
                        **run_settings,
                    )
                )

        if with_comments and resolved.kind == "channel":
            linked = await get_linked_chat_id(client, resolved.chat_id)
            if linked is None:
                console.print(
                    f"[yellow]{_t('tg_channel_label')}[/] {_tf('tg_channel_no_linked', title=resolved.title)}"
                )
            else:
                # Record the linked chat id on the channel row, create discussion sub.
                await repo.upsert_chat(
                    resolved.chat_id,
                    resolved.kind,
                    title=resolved.title,
                    username=resolved.username,
                    linked_chat_id=linked,
                )
                try:
                    linked_entity = await client.get_entity(linked)
                    linked_title = entity_title(linked_entity)
                except Exception:
                    linked_title = None
                # Comments sub piggybacks on the channel's settings; the
                # channel's analyze run pulls comments inline via
                # `--with-comments` so the comments sub itself isn't
                # analyzed independently. Storing matching settings keeps
                # the row self-consistent if the user later disables the
                # parent and runs the comments group on its own.
                subs_to_add.append(
                    Subscription(
                        chat_id=linked,
                        thread_id=0,
                        title=linked_title or f"{resolved.title} (comments)",
                        source_kind="comments",
                        start_from_date=from_dt,
                        transcribe_voice=not no_transcribe,
                        transcribe_videonote=not no_transcribe,
                        transcribe_video=False,
                        **run_settings,
                    )
                )

        for s in subs_to_add:
            await repo.upsert_subscription(s)
        console.print(f"[green]{_t('tg_added_label')}[/] {_tf('tg_added_msg', n=len(subs_to_add))}")
        for s in subs_to_add:
            console.print(
                _tf(
                    "tg_added_sub_line",
                    chat_id=s.chat_id,
                    thread_id=s.thread_id,
                    kind=s.source_kind,
                    title=s.title,
                )
            )

        # Note --last: we apply it by pulling last N messages immediately at next sync;
        # we record start_from_msg_id = (top_msg_id - last) after the first sync pass.
        if last is not None:
            console.print(f"[grey70]{_tf('tg_last_take_effect', value=last)}[/]")
            _hint_last_sync(subs_to_add, last)


def _hint_last_sync(subs: list[Subscription], last: int) -> None:
    # Marker for sync.py: if start_from_msg_id/date are None and a "last" hint
    # is present, we fetch the latest message id and set start_from_msg_id =
    # top_msg_id - last. Delegated to sync.
    for s in subs:
        if s.start_from_msg_id is None and s.start_from_date is None:
            # Encode hint via negative number; sync.py will interpret this.
            s.start_from_msg_id = -int(last)


def _source_kind_for(kind: str) -> str:
    if kind == "channel":
        return "channel"
    if kind == "forum":
        return "chat"
    return "chat"


def _parse_from_msg(value: str | None) -> int | None:
    """Accept either a bare int or a Telegram link pointing at a message."""
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    p = parse(value)
    return p.msg_id


async def _comments_index(repo, subs: list[Subscription]) -> dict[int, dict]:
    """Return per-(channel chat_id) info about its linked comments sub.

    Output keys per channel chat_id (when applicable):
      - "linked_chat_id": int (the discussion group's id)
      - "linked_sub": Subscription | None — the sub for that group, or
        None if the channel has a linked group but the user hasn't
        subscribed to comments yet.

    For a comments sub (source_kind == "comments"), returns a separate
    "comments_for" map keyed by the comments sub's chat_id pointing back
    at the channel chat_id. Both keyspaces are merged in the caller.
    """
    # Map subscription chat_ids → Subscription so we can look up siblings.
    by_chat = {int(s.chat_id): s for s in subs}
    # Pull the channel rows we care about so we know each channel's
    # linked_chat_id without round-tripping Telegram.
    channel_ids = [int(s.chat_id) for s in subs if s.source_kind == "channel"]
    info: dict[int, dict] = {}
    for cid in channel_ids:
        row = await repo.get_chat(cid)
        linked = (row or {}).get("linked_chat_id")
        if linked is None:
            continue
        info[cid] = {
            "linked_chat_id": int(linked),
            "linked_sub": by_chat.get(int(linked))
            if by_chat.get(int(linked)) and by_chat[int(linked)].source_kind == "comments"
            else None,
        }
    # Reverse map so a comments-row can render a back-reference.
    reverse: dict[int, int] = {}
    for cid, meta in info.items():
        if meta.get("linked_sub") is not None:
            reverse[int(meta["linked_chat_id"])] = cid
    return {"by_channel": info, "by_comments": reverse}


def _comments_label(s: Subscription, idx: dict) -> str:
    """Render the value for the `comments` column for one subscription.

    - Channel sub: "✓ <linked title>" if its discussion group is also
      subscribed; "available" if the channel has a linked group but the
      user hasn't subscribed yet; "—" if the channel has no linked group.
    - Comments sub: "↑ for <channel title>" (back-reference).
    - Other kinds: "—".
    """
    by_channel: dict = idx.get("by_channel", {})
    by_comments: dict = idx.get("by_comments", {})
    if s.source_kind == "channel":
        meta = by_channel.get(int(s.chat_id))
        if meta is None:
            return "—"
        linked_sub = meta.get("linked_sub")
        if linked_sub is not None:
            return f"✓ {(linked_sub.title or '').strip() or 'comments'}"
        return "available"
    if s.source_kind == "comments":
        parent_id = by_comments.get(int(s.chat_id))
        if parent_id is None:
            return "↑ (orphan)"
        return f"↑ for {parent_id}"
    return "—"


# ----------------------------------------------------------- sync / backfill


async def cmd_sync(chat: int | None, thread: int | None, dry_run: bool) -> None:
    from unread.tg.sync import sync_subscription

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        subs = await repo.list_subscriptions(enabled_only=True)
        if chat is not None:
            subs = [s for s in subs if s.chat_id == chat and (thread is None or s.thread_id == thread)]
        if not subs:
            console.print(f"[yellow]{_t('tg_no_matching_subs')}[/]")
            return
        total = 0
        for s in subs:
            added = await sync_subscription(client, repo, s, dry_run=dry_run)
            console.print(
                f"  [cyan]sync[/] chat={s.chat_id} thread={s.thread_id} -> "
                f"{'would fetch' if dry_run else 'fetched'} {added} new msgs"
            )
            total += added
        console.print(f"[green]{_t('tg_done_label')}[/] {_tf('tg_done_n_msgs', n=total)}")


async def cmd_backfill(chat: int, from_msg: str, direction: str) -> None:
    from unread.tg.sync import backfill as run_backfill

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        msg_id = _parse_from_msg(from_msg)
        if msg_id is None:
            console.print(f"[red]{_t('tg_from_msg_must_be_link_or_id')}[/]")
            raise typer.Exit(1)
        count = await run_backfill(client, repo, chat_id=chat, from_msg_id=msg_id, direction=direction)
        console.print(
            f"[green]{_t('tg_backfilled_label')}[/] "
            f"{_tf('tg_backfilled_msg', n=count, chat=chat, direction=direction)}"
        )


# -------------------------------------------------------- interactive helpers


def _tui_choose(candidates: list) -> int | None:
    """Callable passed to resolver for ambiguous fuzzy matches."""
    if not sys.stdin.isatty():
        return None
    console.print(f"[yellow]{_t('tg_resolve_multiple_candidates')}[/]")
    for i, c in enumerate(candidates):
        console.print(
            _tf(
                "tg_resolve_candidate_line",
                i=i,
                title=c.title,
                username=c.username or "",
                score=c.score,
                kind=c.kind,
            )
        )
    try:
        raw = typer.prompt(_t("tg_resolve_index_prompt"), default="0")
        return int(raw)
    except (ValueError, EOFError):
        return None


def _sub_detail_panel(sub: Subscription, comments: str) -> Table:
    """Build a vertical key/value table summarizing one subscription.

    Shown after the user picks a subscription in `cmd_chats_manage` so
    they see the full state (preset, period, enrich, mark-read, post-to,
    transcribe flags, start cursor, comments link) before deciding what
    to do with it. Far easier to scan than the wide multi-column list.
    """
    enrich_display = sub.enrich_kinds if sub.enrich_kinds is not None else "(config defaults)"
    if enrich_display == "":
        enrich_display = "none"
    transcribe = ",".join(
        k
        for k, v in [
            ("voice", sub.transcribe_voice),
            ("vnote", sub.transcribe_videonote),
            ("video", sub.transcribe_video),
        ]
        if v
    )
    if sub.start_from_msg_id is not None:
        start = f"msg≥{sub.start_from_msg_id}"
    elif sub.start_from_date is not None:
        start = sub.start_from_date.strftime("%Y-%m-%d")
    else:
        start = "—"
    rows = [
        ("title", sub.title or "—"),
        ("chat_id", str(sub.chat_id)),
        ("thread_id", str(sub.thread_id)),
        ("kind", sub.source_kind),
        ("enabled", "yes" if sub.enabled else "no"),
        ("preset", sub.preset or "summary"),
        ("period", sub.period or "unread"),
        ("enrich", enrich_display),
        ("mark_read", "yes" if sub.mark_read else "no"),
        ("post_to", sub.post_to or "—"),
        ("comments", comments),
        ("transcribe", transcribe or "—"),
        ("start", start),
        ("added_at", sub.added_at.strftime("%Y-%m-%d %H:%M") if sub.added_at else "—"),
    ]
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    for k, v in rows:
        t.add_row(k, v)
    return t


async def cmd_chats_manage() -> None:
    """Single interactive panel: pick a sub, view its details, act on it.

    Opens with just the subscription picker (one line per sub showing
    state + title + kind + comments link). Picking one prints a vertical
    detail panel for that subscription and then offers the action menu
    (toggle on/off, remove keeping messages, remove + purge). Loops
    until `← Done` / Ctrl-C / ESC. The only `chats` subcommands today
    are `add`, `manage`, and `run`.
    """
    from unread.interactive import _expand_printable_for_search

    _expand_printable_for_search()

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        while True:
            subs = await repo.list_subscriptions(enabled_only=False)
            if not subs:
                console.print(f"[yellow]{_t('tg_chats_no_subs')}[/] {_t('tg_chats_use_add')}")
                return
            idx = await _comments_index(repo, subs)

            def _label(s: Subscription, _idx: dict = idx) -> str:
                state = _t("tg_sub_state_on") if s.enabled else _t("tg_sub_state_off")
                title = s.title or str(s.chat_id)
                kind_bit = s.source_kind
                if s.thread_id:
                    kind_bit += " " + _tf("tg_sub_thread_label", id=s.thread_id)
                comments = _comments_label(s, _idx)
                comments_bit = f"  {comments}" if comments and comments != "—" else ""
                return f"{state}  {title}  ({kind_bit}){comments_bit}"

            # Encode the (chat_id, thread_id) tuple as a `cid:tid` string
            # so it survives the prompt.select round-trip (which returns
            # strings only). `__done__` sentinel for the "← Done" row.
            from unread.util.prompt import Choice as _PromptChoice
            from unread.util.prompt import select as _select

            _DONE = "__done__"
            mgr_choices = [
                _PromptChoice(value=f"{int(s.chat_id)}:{int(s.thread_id)}", label=_label(s)) for s in subs
            ]
            mgr_choices.append(_PromptChoice(value=_DONE, label=_t("tg_chats_done_label")))

            picked = _select(_tf("tg_chats_manage_q", n=len(subs)), choices=mgr_choices)
            if picked is None or picked == _DONE:
                return
            chat_id_s, thread_id_s = picked.split(":", 1)
            chat_id = int(chat_id_s)
            thread_id = int(thread_id_s)
            sub = await repo.get_subscription(chat_id, thread_id)
            if not sub:
                console.print(f"[red]{_t('tg_sub_gone')}[/] chat={chat_id} thread={thread_id}")
                continue

            # Show the per-sub detail panel before the action menu so
            # the user has the full picture (preset / period / enrich /
            # transcribe / start cursor / etc.) instead of squinting at
            # a wide table row.
            console.print()
            console.print(_sub_detail_panel(sub, _comments_label(sub, idx)))
            console.print()

            # Per-sub action menu. Toggle label flips with current state
            # so the choice reads as the verb the user is invoking.
            # `value="back"` (not None) for the back row — same questionary
            # gotcha as above.
            toggle_label = _t("tg_sub_action_disable") if sub.enabled else _t("tg_sub_action_enable")
            action = _select(
                _tf("tg_sub_what_next_q", title=sub.title or sub.chat_id),
                choices=[
                    _PromptChoice(value="toggle", label=toggle_label),
                    _PromptChoice(value="remove_keep", label=_t("tg_sub_action_remove_keep")),
                    _PromptChoice(value="remove_purge", label=_t("tg_sub_action_remove_purge")),
                    _PromptChoice(value="back", label=_t("tg_sub_back_label")),
                ],
            )
            if action is None or action == "back":
                continue
            if action == "toggle":
                await repo.set_subscription_enabled(chat_id, thread_id, not sub.enabled)
                done_key = "tg_sub_disabled" if sub.enabled else "tg_sub_enabled"
                console.print(f"[green]{_tf(done_key, chat_id=chat_id, thread_id=thread_id)}[/]")
            elif action in ("remove_keep", "remove_purge"):
                purge = action == "remove_purge"
                # Confirmation guard for purge — irreversible.
                if purge:
                    from unread.util.prompt import confirm as _confirm

                    confirmed = _confirm(
                        _tf("tg_sub_purge_confirm_q", chat_id=chat_id),
                        default=False,
                    )
                    if not confirmed:
                        console.print(f"[grey70]{_t('tg_sub_purge_skipped')}[/]")
                        continue
                await repo.remove_subscription(chat_id, thread_id, purge_messages=purge)
                console.print(
                    f"[green]{_tf('tg_sub_removed', chat_id=chat_id, thread_id=thread_id, purge=purge)}[/]"
                )
