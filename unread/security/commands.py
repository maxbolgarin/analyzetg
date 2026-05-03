"""Implementation of the `unread security` subcommand group."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from unread.config import get_settings
from unread.db._keys import SECRET_KEYS
from unread.secrets_backend import (
    BACKEND_DB,
    BACKEND_KEYCHAIN,
    BACKEND_PASSPHRASE,
    KEYCHAIN_SERVICE,
    is_valid_backend,
    keychain_available,
    keychain_delete,
    keychain_describe,
    keychain_read,
    keychain_write,
    read_active_backend_sync,
)
from unread.util.logging import get_logger

console = Console()
log = get_logger(__name__)


# Sorted view of the allowlist, used wherever we iterate slot names so
# `unread security status` always prints them in a stable order.
_SLOTS: tuple[str, ...] = tuple(sorted(SECRET_KEYS))


def _run_async(coro):
    """Run ``coro`` to completion whether or not an outer loop is active.

    Same pattern as :func:`unread.util.prompt._run_questionary`: when
    we're called from inside `asyncio.run` (e.g. the wizard's
    `_run_keychain_step` reaches `cmd_migrate` via the keychain
    opt-in), ``asyncio.run`` here would blow up with "cannot be called
    from a running event loop". A one-shot worker thread gets its own
    loop and the parent stays unaware.
    """
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _set_active_backend_sync(db_path: Path, name: str) -> None:
    """Write the active-backend choice to ``app_settings`` synchronously.

    Used by `migrate`, which must commit BEFORE any subsequent read of
    ``read_secrets`` would otherwise consult the wrong store. Using
    aiosqlite would force every caller into an async context just to
    flip a single key.
    """
    if not is_valid_backend(name):
        raise ValueError(f"unknown backend: {name!r}")
    import sqlite3
    from datetime import UTC, datetime

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("secrets.backend", name, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def cmd_status() -> None:
    """Print active backend, filesystem perms, FDE state, slot inventory."""
    settings = get_settings()
    db_path = settings.storage.data_path
    backend = read_active_backend_sync(db_path)

    console.print("[bold]unread security status[/]")
    console.print(f"  active backend  : [cyan]{backend}[/]")
    console.print(f"  install dir     : {db_path.parent.parent}")

    if backend == BACKEND_KEYCHAIN:
        if keychain_available():
            console.print(f"  keychain backend: {keychain_describe()}")
        else:
            console.print(
                "  [red]keychain backend marked active but unavailable on this host[/] — "
                "run `unread security migrate --to db` to recover"
            )

    # Per-slot presence. Empty slots are normal (e.g. user only set
    # OpenAI, not Anthropic) so missing isn't an error.
    console.print("")
    console.print("[bold]Slot inventory[/]")
    for key in _SLOTS:
        present_db = _slot_present_db(db_path, key)
        present_kc = keychain_read(key) if keychain_available() else None
        marks = []
        if present_db:
            marks.append("[green]db[/]")
        if present_kc:
            marks.append("[green]keychain[/]")
        if not marks:
            marks.append("[dim]empty[/]")
        console.print(f"  {key:<24} {' + '.join(marks)}")

    # Filesystem mode + FDE rehash from `unread doctor` so a user
    # checking just security doesn't have to run two commands.
    if os.name == "posix":
        try:
            home = db_path.parent.parent
            console.print("")
            console.print("[bold]Filesystem[/]")

            def _check_mode(path: Path, expect_mask: int, expect_label: str) -> None:
                try:
                    if not path.exists():
                        return
                    mode = path.stat().st_mode & 0o777
                    bad = (mode & 0o077) != 0
                    symbol = "[yellow]warn[/]" if bad else "[green]ok[/]"
                    console.print(f"  {symbol} {path} mode {oct(mode)} (expect {expect_label})")
                except OSError:
                    pass

            # Pre-prod review: cmd_status used to report only the home
            # dir mode, missing the case where data.sqlite or the
            # Telethon session file was world-readable on its own (e.g.
            # restored from a backup that flattened modes). Check each
            # sensitive artifact individually so the user sees exactly
            # which file needs `chmod 600`.
            _check_mode(home, 0o700, "0o700")
            _check_mode(db_path, 0o600, "0o600")
            settings = get_settings()
            session_path = settings.telegram.session_path
            for cand in (
                session_path,
                session_path.with_name(session_path.name + ".session"),
            ):
                _check_mode(cand, 0o600, "0o600")
        except OSError:
            pass

    if sys.platform == "darwin":
        import subprocess

        try:
            from unread.util.subprocess_env import clean_subprocess_env

            res = subprocess.run(
                ["fdesetup", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=clean_subprocess_env(),
            )
            out = (res.stdout or "").strip()
            console.print("")
            console.print("[bold]Disk encryption[/]")
            if "FileVault is On" in out:
                console.print("  [green]ok[/] FileVault is On")
            elif "FileVault is Off" in out:
                console.print(
                    "  [yellow]warn[/] FileVault is Off — turn on in "
                    "System Settings → Privacy & Security → FileVault"
                )
            else:
                console.print(f"  [dim]unknown[/] fdesetup: {out[:60] or 'no output'}")
        except (OSError, subprocess.SubprocessError):
            pass


def _slot_present_db(db_path: Path, key: str) -> bool:
    """True iff the DB ``secrets`` table has a non-empty row for ``key``."""
    if not db_path.is_file():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return False
    try:
        cur = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,))
        row = cur.fetchone()
    except sqlite3.Error:
        conn.close()
        return False
    conn.close()
    return bool(row and row[0])


def cmd_migrate(target: str) -> None:
    """Copy slot values to the target backend, blank the source, flip the active flag.

    Idempotent: re-running with ``--to keychain`` after a successful
    migration is a no-op (every slot is already in the keychain and
    its DB row is already empty). The user-visible report makes the
    end state obvious.
    """
    target = target.strip().lower()
    if target not in (BACKEND_DB, BACKEND_KEYCHAIN):
        console.print(f"[red]Unknown target backend: {target!r}.[/] Use one of: db, keychain.")
        raise typer.Exit(1)

    if target == BACKEND_KEYCHAIN and not keychain_available():
        console.print(
            "[red]No usable keychain backend on this host.[/]\n"
            f"  Resolved to: {keychain_describe()}\n"
            "  On macOS the expected backend is keyring.backends.macOS.Keyring.\n"
            "  Most common cause: the [bold]unread[/] tool install predates the\n"
            "  keyring dependency. Re-install the global CLI:\n"
            "      [cyan]uv tool install --editable . --reinstall[/]\n"
            "  On Linux this backend requires a running Secret Service\n"
            "  (gnome-keyring / KWallet)."
        )
        raise typer.Exit(1)

    settings = get_settings()
    db_path = settings.storage.data_path
    if not db_path.is_file():
        console.print(f"[red]No data DB at {db_path}.[/] Run `unread init` first.")
        raise typer.Exit(1)

    # Hard guard against migrating ciphertext into a backend that
    # can't decrypt it. The bug we're protecting against: an install
    # that was on `passphrase` accidentally runs `migrate --to
    # keychain` (or the wizard's auto-offer fired before the bug
    # was fixed) — which would copy `$u1$...` blobs into the
    # keychain as if they were plaintext. The next read would
    # return ciphertext where the caller expected an api_id.
    current_backend = read_active_backend_sync(db_path)
    if current_backend == BACKEND_PASSPHRASE:
        console.print(
            "[red]Refusing to migrate while the active backend is `passphrase`.[/]\n"
            "  Migrating ciphertext rows into a backend that can't decrypt them\n"
            "  would corrupt the install. To switch from passphrase to another\n"
            "  backend, use [cyan]unread security set "
            f"{'plain' if target == BACKEND_DB else 'keystore'}[/]\n"
            "  (it downgrades cleanly first), or [cyan]unread security recover[/]\n"
            "  if you've already hit the bug."
        )
        raise typer.Exit(1)

    if target == BACKEND_KEYCHAIN:
        moved, skipped = _migrate_db_to_keychain(db_path)
        _set_active_backend_sync(db_path, BACKEND_KEYCHAIN)
        console.print(f"[green]Moved[/] {moved} slot(s) to {keychain_describe()}.")
        if skipped:
            console.print(f"[dim]Skipped[/] {skipped} empty slot(s).")
        console.print("Active backend now: [cyan]keychain[/]. The DB rows have been blanked.")
    else:
        moved, skipped = _migrate_keychain_to_db(db_path)
        _set_active_backend_sync(db_path, BACKEND_DB)
        console.print(f"[green]Moved[/] {moved} slot(s) to data.sqlite::secrets.")
        if skipped:
            console.print(f"[dim]Skipped[/] {skipped} empty keychain slot(s).")
        console.print("Active backend now: [cyan]db[/]. Keychain entries removed.")


def _migrate_db_to_keychain(db_path: Path) -> tuple[int, int]:
    """Copy each non-empty DB row into the keychain; blank the DB row on success."""
    from unread.db.repo import read_data_db_secrets_sync
    from unread.security.crypto import is_encrypted

    rows = read_data_db_secrets_sync(db_path)
    # Defensive: refuse to migrate any encrypted rows. The caller
    # (`cmd_migrate`) should already guard via the backend flag, but
    # this catches the case where `secrets.backend` is out of sync
    # with the on-disk content.
    encrypted_slots = [k for k, v in rows.items() if v and is_encrypted(v)]
    if encrypted_slots:
        console.print(
            f"[red]Refusing to migrate ciphertext rows: {', '.join(sorted(encrypted_slots))}.[/]\n"
            "  Run [cyan]unread security recover[/] first to restore plaintext."
        )
        raise typer.Exit(1)
    moved = 0
    skipped = 0
    failures: list[str] = []
    for key in _SLOTS:
        value = rows.get(key) or ""
        if not value:
            skipped += 1
            continue
        if not keychain_write(key, value):
            failures.append(key)
            continue
        moved += 1

    if failures:
        # Don't blank the DB rows for slots that didn't make it into
        # the keychain — that would lose data. Surface the failures
        # before flipping the active flag so the user can decide.
        console.print(f"[red]Keychain write failed for {len(failures)} slot(s): {', '.join(failures)}[/]")
        console.print("[red]Aborting migration to avoid data loss.[/] DB rows kept intact.")
        raise typer.Exit(1)

    # All keychain writes succeeded — clear the DB rows so a reader
    # without keychain access (e.g. a misconfigured cron) doesn't
    # silently keep using stale plaintext copies. We use an async
    # `Repo` here because the schema enforces the allowlist on the
    # write path.
    if moved:
        _run_async(_clear_db_secrets(db_path, [k for k in _SLOTS if rows.get(k)]))
    return moved, skipped


async def _clear_db_secrets(db_path: Path, keys: list[str]) -> None:
    from unread.db.repo import open_repo

    async with open_repo(db_path) as repo:
        for key in keys:
            await repo.delete_secret(key)


def _migrate_keychain_to_db(db_path: Path) -> tuple[int, int]:
    """Copy each non-empty keychain slot into the DB; remove the keychain entry on success."""
    from unread.security.crypto import is_encrypted

    moved = 0
    skipped = 0
    payload: dict[str, str] = {}
    encrypted_slots: list[str] = []
    for key in _SLOTS:
        value = keychain_read(key)
        if not value:
            skipped += 1
            continue
        if is_encrypted(value):
            encrypted_slots.append(key)
            continue
        payload[key] = value
    if encrypted_slots:
        console.print(
            f"[red]Refusing to migrate ciphertext keychain entries: "
            f"{', '.join(sorted(encrypted_slots))}.[/]\n"
            "  Run [cyan]unread security recover[/] first to restore plaintext."
        )
        raise typer.Exit(1)

    if payload:
        _run_async(_put_db_secrets(db_path, payload))
        for key in payload:
            keychain_delete(key)
            moved += 1
    return moved, skipped


async def _put_db_secrets(db_path: Path, values: dict[str, str]) -> None:
    from unread.db.repo import open_repo

    async with open_repo(db_path) as repo:
        await repo.put_secrets(values)


# --- passphrase backend ---------------------------------------------------


def _prompt_passphrase(*, confirm: bool, label: str = "passphrase") -> str:
    """Read a passphrase from the TTY. Twice when ``confirm`` is True."""
    import getpass

    if not sys.stdin.isatty():
        raise typer.Exit(
            "Cannot read a passphrase non-interactively. Use `unread security unlock` from a TTY first."
        )
    while True:
        first = getpass.getpass(f"New {label}: ").strip()
        if not first:
            console.print("[yellow]Empty passphrase rejected. Try again or Ctrl-C to abort.[/]")
            continue
        if not confirm:
            return first
        again = getpass.getpass(f"Confirm {label}: ").strip()
        if again != first:
            console.print("[yellow]Mismatch. Try again.[/]")
            continue
        return first


def _read_one_passphrase() -> str:
    """Single-shot passphrase prompt without confirmation. For unlock/rotate-old."""
    import getpass

    if not sys.stdin.isatty():
        raise typer.Exit("Cannot read a passphrase non-interactively. Re-run from a TTY.")
    pw = getpass.getpass("unread passphrase: ").strip()
    if not pw:
        raise typer.Exit("Empty passphrase. Aborting.")
    return pw


def _convert_sqlite_session_to_string(settings) -> str:  # type: ignore[no-untyped-def]
    """Load the on-disk SQLiteSession (if any) and return its `StringSession.save()` value.

    Returns "" when no session file exists. Used by ``upgrade`` to
    move an authorized Telegram session out of the plaintext SQLite
    file into the encrypted ``telegram.session_string`` slot.
    """
    from telethon.sessions import SQLiteSession, StringSession

    session_path = settings.telegram.session_path
    candidates = [session_path, session_path.with_name(session_path.name + ".session")]
    if not any(c.exists() for c in candidates):
        return ""
    sql_session = SQLiteSession(str(session_path))
    try:
        # Telethon serializes the in-memory session state — this only
        # touches RAM, no network. Empty string when the session file
        # exists but isn't authorized.
        return StringSession.save(sql_session) or ""
    finally:
        sql_session.close()


def cmd_upgrade() -> None:
    """Switch the active backend to ``passphrase``: encrypt every slot under a passphrase.

    Re-reads the current backend, captures every plaintext value
    (DB rows / keychain entries), generates a fresh install salt,
    derives a key, and re-writes everything as ciphertext. Also
    converts the on-disk Telethon SQLiteSession into an encrypted
    StringSession and removes the plaintext session file. Aborts
    cleanly on any error before flipping the active flag, so a
    half-finished upgrade never leaves the user locked out.
    """
    from unread.security.crypto import (
        SALT_LEN,
        derive_key,
        encrypt_with_key,
        remember_key_for_salt,
    )

    settings = get_settings()
    db_path = settings.storage.data_path
    if not db_path.is_file():
        console.print(f"[red]No data DB at {db_path}. Run `unread init` first.[/]")
        raise typer.Exit(1)

    current_backend = read_active_backend_sync(db_path)
    if current_backend == BACKEND_PASSPHRASE:
        console.print("[yellow]Backend is already `passphrase`.[/] Use `rotate-passphrase` to change it.")
        raise typer.Exit(0)

    # Capture plaintext snapshot from the CURRENT backend before we
    # touch anything. read_secrets is the single source of truth for
    # backend dispatch so we get keychain values transparently.
    from unread.secrets import read_secrets

    plaintext = {k: v for k, v in read_secrets(settings).items() if v}
    if not plaintext:
        console.print(
            "[yellow]No saved credentials to encrypt.[/] Run `unread init` to add an API key first."
        )
        raise typer.Exit(0)

    console.print("[bold]Set a passphrase for unread.[/]")
    console.print(
        "[grey70]This protects your API keys and Telegram session at rest. "
        "There is no recovery if you forget it.[/]\n"
    )
    passphrase = _prompt_passphrase(confirm=True)

    salt = os.urandom(SALT_LEN)
    console.print("Deriving key (Scrypt; ~100 ms)…")
    key = derive_key(passphrase, salt)
    remember_key_for_salt(salt, key)

    # Encrypt every slot using the v2 envelope (slot name bound as
    # AEAD AAD). Without that binding, an attacker who can edit the DB
    # can swap the openai.api_key ciphertext into the
    # telegram.api_hash row and the AEAD still verifies.
    encrypted: dict[str, str] = {}
    for slot, value in plaintext.items():
        if slot not in SECRET_KEYS or slot == "telegram.session_string":
            continue
        encrypted[slot] = encrypt_with_key(value, key, salt=salt, slot_name=slot)

    # Convert the on-disk session file to an encrypted string.
    session_str = _convert_sqlite_session_to_string(settings)
    if session_str:
        encrypted["telegram.session_string"] = encrypt_with_key(
            session_str, key, salt=salt, slot_name="telegram.session_string"
        )

    # Persist atomically: salt + ciphertexts AND backend flag in one
    # aiosqlite transaction. Pre-prod review: the previous flow had
    # three separate transactions (salt+ciphertext via aiosqlite,
    # backend flag via sync sqlite3, keychain delete) — a SIGKILL
    # between them left an unreadable install or a stale backend
    # pointing at no ciphertext. The combined transaction means
    # there's no intermediate state where readers see one but not the
    # other.
    _run_async(_persist_upgrade(db_path, salt, encrypted, target_backend=BACKEND_PASSPHRASE))

    # Best-effort cleanup of the plaintext side-channels.
    if current_backend == BACKEND_KEYCHAIN:
        for slot in SECRET_KEYS:
            keychain_delete(slot)

    # Drop the plaintext on-disk session file — its content is now
    # safely under `telegram.session_string`. Same files we look at
    # in `revoke-session`.
    session_path = settings.telegram.session_path
    for candidate in (session_path, session_path.with_name(session_path.name + ".session")):
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError as e:
            console.print(f"[yellow]Could not remove {candidate}:[/] {e}")

    # Pre-prod review: do NOT auto-cache the derived key on disk after
    # `upgrade`. The previous behavior (`ttl_seconds=None`) wrote the
    # master key under `~/.unread/.runtime/key` forever, on macOS /
    # Windows persisted across reboots and into the user's `~/`
    # backup. Anyone reading the file got every secret. Users who want
    # the convenience can opt in explicitly via `unread security
    # unlock --keep 30m` once `upgrade` finishes.

    console.print(f"\n[green]✓ Encrypted {len(encrypted)} slot(s) with your passphrase.[/]")
    console.print(
        "Active backend now: [cyan]passphrase[/]. "
        "Run [cyan]unread security unlock --keep 30m[/] for cross-shell convenience, "
        "or [cyan]unread security lock[/] to wipe the in-process key."
    )


async def _persist_upgrade(
    db_path: Path,
    salt: bytes,
    encrypted: dict[str, str],
    *,
    target_backend: str | None = None,
) -> None:
    """Write the install salt + ciphertext slots + backend flag atomically.

    `target_backend` is the new value of `app_settings::secrets.backend`.
    Passing it (instead of flipping the flag in a follow-up sync sqlite
    call) means a SIGKILL between the writes can't leave readers seeing
    a backend pointer that doesn't match the on-disk ciphertext shape.

    Implementation note: the high-level `set_app_setting` /
    `put_secrets` helpers each call `self._conn.commit()` internally,
    which would terminate our outer BEGIN IMMEDIATE. Issue the writes
    directly so all of them land in one commit (or roll back together).
    """
    import base64
    from datetime import UTC, datetime

    from unread.db._keys import OVERRIDE_KEYS, SECRET_KEYS
    from unread.db.repo import open_repo

    salt_b64 = base64.urlsafe_b64encode(salt).rstrip(b"=").decode("ascii")
    now_iso = datetime.now(UTC).isoformat()

    # Allowlist enforcement (same as the helpers do at their boundaries).
    for slot in encrypted:
        if slot not in SECRET_KEYS:
            raise ValueError(f"unknown secret key: {slot!r}; allowed: {sorted(SECRET_KEYS)}")
    if "security.kdf_salt" not in OVERRIDE_KEYS:
        raise ValueError("'security.kdf_salt' not in OVERRIDE_KEYS allowlist — schema drift")
    if target_backend is not None and "secrets.backend" not in OVERRIDE_KEYS:
        raise ValueError("'secrets.backend' not in OVERRIDE_KEYS allowlist — schema drift")

    async with open_repo(db_path) as repo:
        conn = repo._conn  # type: ignore[attr-defined]
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                ("security.kdf_salt", salt_b64, now_iso),
            )
            if encrypted:
                rows = [(k, v, now_iso) for k, v in encrypted.items() if v]
                if rows:
                    await conn.executemany(
                        """
                        INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value=excluded.value,
                            updated_at=excluded.updated_at
                        """,
                        rows,
                    )
            if target_backend is not None:
                await conn.execute(
                    """
                    INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    ("secrets.backend", target_backend, now_iso),
                )
            await conn.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                await conn.rollback()
            raise


def cmd_rotate_passphrase() -> None:
    """Re-encrypt every slot under a new passphrase. Old one is required to decrypt first."""
    settings = get_settings()
    db_path = settings.storage.data_path
    if read_active_backend_sync(db_path) != BACKEND_PASSPHRASE:
        console.print("[red]Backend is not `passphrase`.[/] Run `unread security upgrade` first.")
        raise typer.Exit(1)

    from unread.security.crypto import (
        SALT_LEN,
        derive_key,
        encrypt_with_key,
        forget_cached_key,
        forget_process_keys,
        remember_key_for_salt,
    )

    # Validate the OLD passphrase by decrypting the existing slots
    # via the normal read path. If it's wrong, this raises.
    console.print("Confirm the current passphrase:")
    _ = _read_one_passphrase()  # cache it via _ensure_passphrase below

    # Hijack the in-process passphrase cache so read_secrets uses what
    # the user just typed instead of re-prompting.
    import unread.secrets as _secrets

    _secrets._PROCESS_PASSPHRASE = _  # type: ignore[attr-defined]

    from unread.secrets import read_secrets

    plaintext = {k: v for k, v in read_secrets(settings).items() if v}
    if not plaintext:
        console.print("[red]Decryption returned no values — passphrase wrong, or DB empty.[/]")
        raise typer.Exit(1)
    # Pull the encrypted session string separately (read_secrets only
    # surfaces fields that map to settings; the session string isn't one).
    from unread.security.passphrase import read_session_string_sync

    session_str = read_session_string_sync(db_path)

    console.print("[bold]Set a new passphrase.[/]")
    new_passphrase = _prompt_passphrase(confirm=True)

    new_salt = os.urandom(SALT_LEN)
    console.print("Deriving new key…")
    new_key = derive_key(new_passphrase, new_salt)

    encrypted: dict[str, str] = {}
    for slot, value in plaintext.items():
        if slot not in SECRET_KEYS or slot == "telegram.session_string":
            continue
        # v2 envelope (slot bound) — same upgrade path as cmd_upgrade.
        encrypted[slot] = encrypt_with_key(value, new_key, salt=new_salt, slot_name=slot)
    if session_str:
        encrypted["telegram.session_string"] = encrypt_with_key(
            session_str, new_key, salt=new_salt, slot_name="telegram.session_string"
        )

    # Backend was already `passphrase` (gate above); no flip needed.
    _run_async(_persist_upgrade(db_path, new_salt, encrypted))

    # Refresh the in-process key cache with the new key. Do NOT
    # auto-write the cross-invocation (on-disk) cache — same reasoning
    # as `cmd_upgrade`: on macOS/Windows it would persist across
    # reboot. The user can opt in via `unread security unlock` if
    # they want cross-shell convenience.
    forget_process_keys()
    remember_key_for_salt(new_salt, new_key)
    _secrets._PROCESS_PASSPHRASE = new_passphrase  # type: ignore[attr-defined]
    forget_cached_key()

    console.print(f"[green]✓ Re-encrypted {len(encrypted)} slot(s) with the new passphrase.[/]")
    console.print(
        "  [grey70]Run [cyan]unread security unlock --keep 30m[/] to cache the new key for cross-shell use.[/]"
    )


def cmd_downgrade() -> None:
    """Decrypt every slot back to plaintext and switch backend to ``db``.

    Telegram session string is dropped — the user must re-run
    ``unread init`` to re-authenticate. (We could write the
    session back to a SQLiteSession on disk, but the safer behavior
    is to force a fresh login when downgrading from encrypted-at-rest
    storage; an attacker who got the user to run downgrade shouldn't
    get a working session out of it.)
    """
    settings = get_settings()
    db_path = settings.storage.data_path
    if read_active_backend_sync(db_path) != BACKEND_PASSPHRASE:
        console.print("[red]Backend is not `passphrase`.[/] Nothing to downgrade.")
        raise typer.Exit(1)

    console.print(
        "[yellow]Downgrade will write your API keys back to the data DB in plaintext.[/]\n"
        "[grey70]A backup of `data.sqlite` will then leak them again. "
        "Continue only if you understand the tradeoff.[/]"
    )
    if not typer.confirm("Proceed with downgrade?", default=False):
        raise typer.Exit(0)

    pw = _read_one_passphrase()
    import unread.secrets as _secrets

    _secrets._PROCESS_PASSPHRASE = pw  # type: ignore[attr-defined]

    from unread.secrets import read_secrets

    plaintext = {k: v for k, v in read_secrets(settings).items() if v}
    if not plaintext:
        console.print("[red]Decryption returned nothing — passphrase wrong.[/]")
        raise typer.Exit(1)

    _run_async(_persist_downgrade(db_path, plaintext))
    _set_active_backend_sync(db_path, BACKEND_DB)

    from unread.security.crypto import forget_cached_key, forget_process_keys

    forget_process_keys()
    forget_cached_key()
    _secrets._PROCESS_PASSPHRASE = None  # type: ignore[attr-defined]

    console.print(f"[green]✓ Wrote {len(plaintext)} plaintext slot(s).[/]")
    console.print("Active backend now: [cyan]db[/].")
    console.print("[yellow]The Telegram session was discarded — run `unread init` to re-authenticate.[/]")


async def _persist_downgrade(db_path: Path, plaintext: dict[str, str]) -> None:
    from unread.db.repo import open_repo

    async with open_repo(db_path) as repo:
        # Wipe encrypted session string; re-auth required after downgrade.
        await repo.delete_secret("telegram.session_string")
        # Drop the install salt — backend is no longer passphrase.
        await repo.delete_app_setting("security.kdf_salt")  # type: ignore[attr-defined]
        # Replace ciphertext rows with plaintext.
        write_back = {k: v for k, v in plaintext.items() if k != "telegram.session_string"}
        if write_back:
            await repo.put_secrets(write_back)


def cmd_unlock(keep: str | None = None) -> None:
    """Cache the derived key on disk so subsequent commands don't re-prompt.

    ``keep`` accepts None (no expiry; until ``lock``), or a humane
    duration like ``30m`` / ``2h`` / ``1d``. The cache file is mode
    0600 in ``$XDG_RUNTIME_DIR/unread/`` (Linux tmpfs) or
    ``~/.unread/.runtime/`` (everywhere else).
    """
    settings = get_settings()
    db_path = settings.storage.data_path
    if read_active_backend_sync(db_path) != BACKEND_PASSPHRASE:
        console.print("[red]Backend is not `passphrase`. Nothing to unlock.[/]")
        raise typer.Exit(1)

    from unread.security.crypto import derive_key, store_cached_key
    from unread.security.passphrase import read_install_salt

    salt = read_install_salt(db_path)
    if salt is None:
        console.print("[red]Install salt missing — re-run `unread security upgrade`.[/]")
        raise typer.Exit(1)

    pw = _read_one_passphrase()
    key = derive_key(pw, salt)

    # Validate: try to decrypt one stored slot. Cheap roundtrip;
    # fails fast on a typo'd passphrase rather than silently caching
    # a useless key.
    import unread.secrets as _secrets
    from unread.security.passphrase import ensure_install_key as _ensure  # noqa: F401

    _secrets._PROCESS_PASSPHRASE = pw  # type: ignore[attr-defined]
    from unread.secrets import read_secrets

    try:
        if not read_secrets(settings):
            console.print("[red]Passphrase did not decrypt any slot.[/]")
            raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Decryption failed:[/] {e}")
        raise typer.Exit(1) from e

    # Default TTL when --keep is omitted: pre-prod review flagged the
    # old "until lock" default as a security regression (file lives on
    # persistent disk on macOS/Windows). Users who want a longer
    # window can pass `--keep 8h` etc.; --keep until-lock is still
    # available for power users via the explicit `--keep until-lock`
    # spelling parsed by `_parse_keep`.
    from unread.security.crypto import DEFAULT_KEY_CACHE_TTL_SEC

    ttl = _parse_keep(keep) if keep else DEFAULT_KEY_CACHE_TTL_SEC
    path = store_cached_key(key, salt, ttl_seconds=ttl)
    if ttl is None:
        suffix = " (until `unread security lock`)"
    elif keep:
        suffix = f" (expires in {keep})"
    else:
        suffix = f" (expires in {DEFAULT_KEY_CACHE_TTL_SEC // 60} minutes — default)"
    console.print(f"[green]✓ Cached key at {path}[/]{suffix}")


def _parse_keep(spec: str) -> int | None:
    """Parse '30m' / '2h' / '1d' into seconds. Default unit: minutes.

    `until-lock` returns None — caller stores the key with no expiry
    (cache lives until `unread security lock` or reboot). Power-user
    only because the cache file is on persistent disk on macOS/Windows.
    """
    s = spec.strip().lower()
    if not s:
        raise typer.BadParameter("empty --keep value")
    if s in {"until-lock", "until_lock", "forever", "session"}:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    last = s[-1]
    if last in units:
        try:
            return int(s[:-1]) * units[last]
        except ValueError:
            raise typer.BadParameter(f"can't parse --keep={spec!r}") from None
    try:
        return int(s) * 60
    except ValueError:
        raise typer.BadParameter(f"can't parse --keep={spec!r}") from None


def cmd_lock() -> None:
    """Wipe the on-disk key cache. Subsequent commands re-prompt."""
    from unread.security.crypto import forget_cached_key, forget_process_keys

    removed = forget_cached_key()
    forget_process_keys()
    if removed:
        console.print("[green]✓ Locked.[/] Next command will prompt for the passphrase.")
    else:
        console.print("[dim]Already locked (no cached key).[/]")


# --- unified `set` ---------------------------------------------------------


# User-facing aliases for the three backends. Stored verbatim in
# ``app_settings::secrets.backend`` only after the alias has been
# resolved to the canonical name — the DB never sees ``plain`` /
# ``keystore`` / ``pass``. Keeping the aliases at the CLI boundary
# lets us rename the user-facing surface in the future without
# rewriting every install.
_BACKEND_ALIASES: dict[str, str] = {
    # canonical
    BACKEND_DB: BACKEND_DB,
    BACKEND_KEYCHAIN: BACKEND_KEYCHAIN,
    BACKEND_PASSPHRASE: BACKEND_PASSPHRASE,
    # short / friendly
    "plain": BACKEND_DB,
    "plaintext": BACKEND_DB,
    "keystore": BACKEND_KEYCHAIN,
    "keyring": BACKEND_KEYCHAIN,
    "pass": BACKEND_PASSPHRASE,
    "encrypted": BACKEND_PASSPHRASE,
}

_BACKEND_DISPLAY: dict[str, str] = {
    BACKEND_DB: "plain (data.sqlite, plaintext)",
    BACKEND_KEYCHAIN: "keystore (OS keychain, encrypted at rest)",
    BACKEND_PASSPHRASE: "pass (passphrase-encrypted, includes Telegram session)",
}


def cmd_set(target: str) -> None:
    """One-shot backend switcher: ``unread security set {plain|keystore|pass}``.

    Resolves the user-facing alias, looks at the current backend,
    and routes to the right combination of ``migrate`` / ``upgrade`` /
    ``downgrade`` to land on the requested target. No-op when the
    target equals the current backend. Refuses unsupported transitions
    cleanly (e.g. asking for ``keystore`` on a host without a usable
    OS keychain) before changing any state.
    """
    raw = (target or "").strip().lower()
    if raw not in _BACKEND_ALIASES:
        accepted = ", ".join(sorted({*_BACKEND_ALIASES}))
        console.print(f"[red]Unknown backend: {target!r}.[/]\n  Accepted: {accepted}")
        raise typer.Exit(1)
    canonical = _BACKEND_ALIASES[raw]

    settings = get_settings()
    db_path = settings.storage.data_path
    if not db_path.is_file():
        console.print(f"[red]No data DB at {db_path}.[/] Run `unread init` first.")
        raise typer.Exit(1)

    current = read_active_backend_sync(db_path)
    if current == canonical:
        console.print(f"[dim]Backend is already[/] [cyan]{_BACKEND_DISPLAY[canonical]}[/]. Nothing to do.")
        return

    console.print(
        f"Switching backend: [cyan]{_BACKEND_DISPLAY[current]}[/] → [cyan]{_BACKEND_DISPLAY[canonical]}[/]"
    )

    # Two-stage transitions are only needed for `pass → keystore`,
    # since `upgrade` already handles `keystore → pass` directly via
    # `read_secrets`. Everything else is a single-step migrate.
    if current == BACKEND_PASSPHRASE and canonical == BACKEND_KEYCHAIN:
        # passphrase → db → keychain. `downgrade` writes plaintext
        # back to the DB, then `migrate --to keychain` moves it.
        cmd_downgrade()
        cmd_migrate(BACKEND_KEYCHAIN)
        return
    if canonical == BACKEND_DB and current == BACKEND_KEYCHAIN:
        cmd_migrate(BACKEND_DB)
        return
    if canonical == BACKEND_DB and current == BACKEND_PASSPHRASE:
        cmd_downgrade()
        return
    if canonical == BACKEND_KEYCHAIN:
        # current == BACKEND_DB
        cmd_migrate(BACKEND_KEYCHAIN)
        return
    if canonical == BACKEND_PASSPHRASE:
        # current is db or keychain — `upgrade` reads via the active
        # backend and re-encrypts everything.
        cmd_upgrade()
        return
    # Unreachable: every (current, canonical) pair is covered above.
    raise RuntimeError(f"unhandled transition {current} → {canonical}")


# --- recovery -------------------------------------------------------------


def cmd_recover() -> None:
    """Decrypt-in-place recovery for installs whose secrets got mismigrated.

    Two scenarios this fixes, both leaving you with ``$u1$``-prefixed
    ciphertext in a backend (DB or keychain) that doesn't know how to
    decrypt it:

    * A pre-fix wizard run migrated ciphertext from the passphrase
      backend straight into the keychain.
    * A manual ``security migrate`` was issued while the active
      backend was still passphrase.

    Walk every slot in both stores, find the ones that look encrypted,
    prompt for the passphrase, decrypt with the install salt (or
    per-record salt as fallback), and write the plaintext back to the
    same store. After this, ``security set {plain|keystore|pass}``
    works normally again. The Telegram session string is decrypted
    too but a re-auth via ``unread login --force`` is still needed
    because the on-disk SQLiteSession was deleted at upgrade time.
    """
    from unread.db._keys import SECRET_KEYS as _ALL_SLOTS
    from unread.db.repo import read_data_db_secrets_sync
    from unread.security.crypto import (
        PassphraseError,
        decrypt_with_key,
        derive_key,
        is_encrypted,
        parse_envelope,
    )
    from unread.security.passphrase import read_install_salt

    settings = get_settings()
    db_path = settings.storage.data_path

    # Build a map of {slot: (where, ciphertext)} for every encrypted
    # row we can see. Either store may have ciphertext after a
    # botched migration.
    found: dict[str, tuple[str, str]] = {}
    db_rows = read_data_db_secrets_sync(db_path)
    for slot, value in db_rows.items():
        if value and is_encrypted(value):
            found[slot] = ("db", value)
    if keychain_available():
        for slot in sorted(_ALL_SLOTS):
            value = keychain_read(slot)
            if value and is_encrypted(value):
                # DB takes precedence if both stores have it (shouldn't
                # happen, but pick a deterministic winner).
                found.setdefault(slot, ("keychain", value))

    if not found:
        console.print(
            "[green]No encrypted-but-misplaced rows found.[/] "
            "Nothing to recover — your backend is already in a coherent state."
        )
        return

    console.print(f"[yellow]Found {len(found)} ciphertext row(s) sitting in the wrong backend:[/]")
    for slot, (where, _) in sorted(found.items()):
        console.print(f"  • {slot:<24} in {where}")
    console.print("")

    salt = read_install_salt(db_path)
    # Honour the same passphrase source order as `read_secrets`:
    # in-process cache → UNREAD_PASSPHRASE → getpass prompt. Keeps
    # cron / scripted recovery viable.
    from unread.secrets import _ensure_passphrase

    try:
        pw = _ensure_passphrase()
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    install_key: bytes | None = derive_key(pw, salt) if salt is not None else None

    # Pre-prod review: previous loop did one Scrypt per non-matching
    # salt - N salts means N x Scrypt for a wrong passphrase. Cache
    # derived keys keyed by salt so each distinct salt costs at most
    # one derivation regardless of how many slots share it.
    salt_keys: dict[bytes, bytes] = {}
    if install_key is not None and salt is not None:
        salt_keys[salt] = install_key

    # Validate the passphrase against the FIRST encrypted row before
    # surfacing any plaintext. Without this, a wrong passphrase that
    # happens to have a salt-shared install key for some slots would
    # leak partial decrypts before failing on a later slot — both a
    # confusing UX and a small information disclosure ("which slots
    # were salt-shared with the install").
    decrypted: dict[str, tuple[str, str]] = {}
    items = list(found.items())
    for slot, (where, ct) in items:
        try:
            env = parse_envelope(ct)
            row_key = salt_keys.get(env.salt)
            if row_key is None:
                row_key = derive_key(pw, env.salt)
                salt_keys[env.salt] = row_key
            plaintext = decrypt_with_key(ct, row_key, slot_name=slot)
        except PassphraseError:
            console.print(f"[red]Wrong passphrase[/] (failed on slot {slot!r}). Aborting before any writes.")
            raise typer.Exit(1) from None
        decrypted[slot] = (where, plaintext)

    # Write plaintext back to the same store. We deliberately keep the
    # current backend flag — recovery restores readability without
    # changing the user's chosen storage policy. They can run
    # `unread security set plain` afterwards if they want plaintext
    # at rest, or re-run `set pass` to encrypt cleanly.
    db_writes: dict[str, str] = {}
    for slot, (where, plaintext) in decrypted.items():
        if where == "keychain":
            keychain_write(slot, plaintext)
        else:
            db_writes[slot] = plaintext
    if db_writes:
        _run_async(_put_db_secrets(db_path, db_writes))

    # If the install was on `passphrase` before the bad migration,
    # demote to `db` since the install salt + ciphertext invariants
    # are no longer in sync. The user can re-encrypt with
    # `unread security set pass`.
    current_backend = read_active_backend_sync(db_path)
    if current_backend == BACKEND_PASSPHRASE:
        _set_active_backend_sync(db_path, BACKEND_DB)
        console.print("Demoted backend: [cyan]passphrase[/] → [cyan]db[/].")

    console.print(f"\n[green]✓ Recovered {len(decrypted)} slot(s).[/]")
    console.print(
        "[yellow]Telegram session must be re-authenticated:[/] "
        "the on-disk SQLiteSession was removed during the original encrypt step. "
        "Run [cyan]unread login --force[/] to log in again."
    )


# --- session revoke -------------------------------------------------------


def cmd_revoke_session() -> None:
    """Delete the local Telethon session file and remind the user to revoke remotely."""
    settings = get_settings()
    session_path = settings.telegram.session_path
    candidates = [session_path, session_path.with_name(session_path.name + ".session")]
    removed: list[Path] = []
    for c in candidates:
        try:
            if c.exists():
                c.unlink()
                removed.append(c)
        except OSError as e:
            console.print(f"[red]Could not remove {c}:[/] {e}")

    if not removed:
        console.print("[dim]No local session file found — nothing to remove.[/]")
    else:
        for p in removed:
            console.print(f"[green]Removed[/] {p}")

    console.print("")
    console.print(
        "[bold]Important:[/] removing the local file does NOT log you out on the "
        "Telegram side. Open Telegram → Settings → Devices → Active Sessions and "
        "terminate the entry that matches this device to fully revoke."
    )


def register(app: typer.Typer, panel: str) -> typer.Typer:
    """Build and register the `security` typer subapp on the root ``app``.

    Returned for tests that want to invoke it directly without going
    through the root.
    """
    security_app = typer.Typer(
        help="Inspect and harden on-disk credential storage.",
        no_args_is_help=True,
    )

    @security_app.command("status")
    def _status() -> None:
        """Print the active backend, slot inventory, FDE state."""
        cmd_status()

    @security_app.command("set")
    def _set(
        backend: str = typer.Argument(
            ...,
            metavar="{plain|keystore|pass}",
            help=(
                "plain    — data.sqlite::secrets, plaintext (default)\n"
                "keystore — OS keychain (encrypted at rest, no passphrase needed)\n"
                "pass     — passphrase-encrypted, includes the Telegram session"
            ),
        ),
    ) -> None:
        """Switch the credential-storage backend (recommended one-shot UX).

        Aliases for ``migrate`` / ``upgrade`` / ``downgrade`` — picks
        the right combination for the current → target transition.
        Examples: ``unread security set keystore``, ``unread security
        set pass``, ``unread security set plain``.
        """
        cmd_set(backend)

    @security_app.command("migrate", hidden=True)
    def _migrate(
        to: str = typer.Option(
            ...,
            "--to",
            help="Target backend: db | keychain.",
        ),
    ) -> None:
        """Legacy migrate command. Prefer `unread security set {plain|keystore}`."""
        cmd_migrate(to)

    @security_app.command("revoke-session")
    def _revoke() -> None:
        """Delete the local Telegram session file (revoke remotely from the Telegram app)."""
        cmd_revoke_session()

    @security_app.command("recover")
    def _recover() -> None:
        """Decrypt-in-place fix for slots that got migrated as ciphertext."""
        cmd_recover()

    @security_app.command("upgrade", hidden=True)
    def _upgrade() -> None:
        """Legacy upgrade command. Prefer `unread security set pass`."""
        cmd_upgrade()

    @security_app.command("rotate-passphrase")
    def _rotate() -> None:
        """Re-encrypt every slot under a new passphrase."""
        cmd_rotate_passphrase()

    @security_app.command("downgrade", hidden=True)
    def _downgrade() -> None:
        """Legacy downgrade command. Prefer `unread security set plain`."""
        cmd_downgrade()

    @security_app.command("unlock")
    def _unlock(
        keep: str | None = typer.Option(
            None,
            "--keep",
            help="Cache TTL (e.g. 30m, 2h, 1d). Default: until `lock`.",
        ),
    ) -> None:
        """Cache the derived key so subsequent commands don't prompt."""
        cmd_unlock(keep)

    @security_app.command("lock")
    def _lock() -> None:
        """Wipe the on-disk key cache."""
        cmd_lock()

    app.add_typer(security_app, name="security", rich_help_panel=panel)
    return security_app


__all__ = [
    "KEYCHAIN_SERVICE",
    "cmd_downgrade",
    "cmd_lock",
    "cmd_migrate",
    "cmd_recover",
    "cmd_revoke_session",
    "cmd_rotate_passphrase",
    "cmd_set",
    "cmd_status",
    "cmd_unlock",
    "cmd_upgrade",
    "register",
]
