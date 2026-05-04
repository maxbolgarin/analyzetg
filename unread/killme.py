"""`unread killme` — irreversible self-uninstall.

Wipes every artifact this CLI ever wrote: the install dir tree (DB,
session, media cache, reports, config), the install pointer at
``~/.unread/install.toml``, every secret in the OS keychain, the
runtime key cache, and (best-effort) the `uv tool`-managed binary.

The command is gated behind an explicit "killme" type-in confirmation
because the action is irrecoverable — none of the deletes go to a
trash. `--yes` skips the type-in for scripted teardowns.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()


@dataclass
class _Plan:
    """What `killme` is about to remove. Built before any prompt so the
    user sees exact paths / slot names / sizes."""

    install_home: Path
    install_pointer: Path | None  # `~/.unread/install.toml`, or None when absent
    install_home_size: int  # bytes
    home_entries: list[tuple[Path, int]]  # (path, size) for top-level subtree of install_home
    keychain_slots: list[str]  # slot names with non-empty values
    runtime_key_path: Path | None  # cached encryption key, if present
    binary_uninstall: tuple[str, list[str]] | None  # (label, argv) or None
    # Aux: DB-side info captured before deletion (since the DB itself is
    # going away). Used in the plan summary so the user sees what they'll
    # lose.
    active_backend: str = ""
    extra_notes: list[str] = field(default_factory=list)


def cmd_killme(yes: bool) -> int:
    """Show a deletion plan, confirm, then wipe everything. Returns the exit code."""
    # Refuse to run when the resolved install home looks dangerous (e.g.
    # `UNREAD_HOME=/` or `UNREAD_HOME=$HOME`) BEFORE building the plan.
    # `_build_plan` walks the install dir to compute sizes; rooted at `/`
    # that's a multi-minute (or OOM) filesystem walk on the way to a
    # rejection that we already know is coming. Users who set the env
    # var by mistake would otherwise lose their entire home directory
    # or root filesystem to `shutil.rmtree` later in this function.
    from unread.core.paths import unread_home

    home_for_safety = unread_home().resolve()
    rejection = _reject_unsafe_home(home_for_safety)
    if rejection is not None:
        console.print(f"[bold red]Refusing to run killme:[/] {rejection}")
        console.print(
            "[yellow]If this is really the install you want to wipe, set UNREAD_HOME "
            "to a deeper-nested path or remove it manually.[/]"
        )
        return 1

    plan = _build_plan()

    _print_plan(plan)

    if not _confirm_killme(yes):
        console.print("[yellow]Aborted. Nothing was deleted.[/]")
        return 1

    failures: list[str] = []

    # 1) Wipe keychain entries first while the data DB still exists —
    # the active-backend lookup at startup needs the DB present.
    for slot in plan.keychain_slots:
        try:
            from unread.secrets_backend import keychain_delete

            keychain_delete(slot)
            console.print(f"  [green]✓[/] keychain  {slot}")
        except Exception as e:
            failures.append(f"keychain:{slot}: {e}")
            console.print(f"  [red]×[/] keychain  {slot}: {e}")

    # 2) Drop the cached encryption key (passphrase backend artifact).
    if plan.runtime_key_path is not None:
        try:
            from unread.security.crypto import forget_cached_key

            forget_cached_key()
            console.print(f"  [green]✓[/] removed   {plan.runtime_key_path}")
        except Exception as e:
            failures.append(f"runtime-key: {e}")
            console.print(f"  [red]×[/] runtime key: {e}")

    # 3) Wipe the install dir tree.
    if plan.install_home.exists():
        try:
            shutil.rmtree(plan.install_home)
            console.print(f"  [green]✓[/] removed   {plan.install_home}")
        except Exception as e:
            failures.append(f"install-home: {e}")
            console.print(f"  [red]×[/] {plan.install_home}: {e}")

    # 4) Drop the pointer file at `~/.unread/install.toml` if it lives
    # outside the install dir we just wiped (custom-path installs).
    if plan.install_pointer is not None and plan.install_pointer.exists():
        try:
            plan.install_pointer.unlink()
            console.print(f"  [green]✓[/] removed   {plan.install_pointer}")
            # Try to drop the now-empty `~/.unread/` shell as well.
            with contextlib.suppress(OSError):
                plan.install_pointer.parent.rmdir()
        except Exception as e:
            failures.append(f"install-pointer: {e}")
            console.print(f"  [red]×[/] {plan.install_pointer}: {e}")

    # 5) Best-effort binary uninstall. We run this LAST because once the
    # `unread` script is gone the user can't easily re-run anything.
    if plan.binary_uninstall is not None:
        label, argv = plan.binary_uninstall
        console.print(f"\n[bold]Uninstalling binary via {label}…[/]")
        try:
            # cwd=Path.home() because the install dir we just deleted may
            # have been the user's CWD; subprocess inherits the parent's
            # CWD and would fail with ENOENT before even running.
            # env=clean_subprocess_env() so we don't hand the user's
            # API keys to `uv tool uninstall` along the way out.
            from unread.util.subprocess_env import clean_subprocess_env

            res = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                cwd=str(Path.home()),
                env=clean_subprocess_env(),
            )
            stdout = (res.stdout or "").strip()
            stderr = (res.stderr or "").strip()
            if res.returncode == 0:
                if stdout:
                    console.print(f"  [grey70]{stdout}[/]")
                console.print("  [green]✓[/] binary uninstalled.")
            else:
                failures.append(f"binary: rc={res.returncode}")
                if stderr:
                    console.print(f"  [red]×[/] {stderr}")
                console.print(
                    f"  [yellow]Binary still present.[/] Remove it manually: [cyan]{' '.join(argv)}[/]"
                )
        except FileNotFoundError:
            failures.append(f"binary: {argv[0]} not found")
            console.print(
                f"  [yellow]{argv[0]} not on PATH — uninstall the binary manually "
                f"with your package manager.[/]"
            )
        except Exception as e:
            failures.append(f"binary: {e}")
            console.print(f"  [red]×[/] {e}")
    else:
        console.print(
            "\n[grey70]Skipping binary uninstall — couldn't detect a `uv tool` install. "
            "If you used pip / pipx / brew, uninstall manually.[/]"
        )

    if failures:
        console.print(
            f"\n[yellow]Done with {len(failures)} issue(s):[/] "
            "review the lines marked × above. Re-run after fixing or clean up by hand."
        )
        return 2

    console.print(
        "\n[green]✓ unread fully removed.[/] "
        "Thanks for trying it. (You can re-install any time with "
        "[cyan]uv tool install unread[/].)"
    )
    return 0


def _reject_unsafe_home(home: Path) -> str | None:
    """Return a reason string if `home` is too dangerous to rmtree, else None.

    A misconfigured `UNREAD_HOME` (e.g. `/`, the user's `$HOME`, `/usr`)
    would, without this guard, take the user's entire home directory or
    root filesystem with it on `killme`. We refuse outright when the
    target:

    * resolves to the filesystem root,
    * is the user's home directory itself,
    * is one of a handful of common system roots,
    * has fewer than two path components after the root (so we never
      operate on first-level dirs like `/usr`, `/etc`, `/var`).

    The conservative bar — at least two components AND not the home dir —
    matches the realistic install layouts (`~/.unread`, `~/projects/foo/.unread`,
    `/opt/unread/install`) without false positives in tests where the
    sandbox is several levels deep under `/tmp/`.
    """
    try:
        resolved = home.resolve()
    except OSError as e:
        return f"could not resolve {home} ({e})"
    parts = resolved.parts
    if len(parts) <= 1:
        return f"{resolved} resolves to the filesystem root"
    try:
        user_home = Path.home().resolve()
    except OSError:
        user_home = None
    if user_home is not None and resolved == user_home:
        return f"{resolved} is the user's home directory"
    # Reject first-level system dirs on POSIX. On Windows `parts` looks
    # like ('C:\\', 'Users', ...) so length≥3 implies safe.
    dangerous = {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
        "/Users",
        "/Applications",
        "/System",
        "/Library",
    }
    # Check both the literal `home` and the symlink-resolved variant.
    # On macOS, `Path("/etc").resolve()` returns `/private/etc` (3 parts),
    # which slips past both the dangerous-set membership and the
    # len<3 fallback below — but the user clearly typed a system dir.
    # Catch both spellings explicitly.
    if str(home) in dangerous or str(resolved) in dangerous:
        return f"{resolved} is a system directory"
    if len(parts) < 3:
        return f"{resolved} is a top-level directory; refusing to wipe"
    return None


def _build_plan() -> _Plan:
    """Inspect the live install and produce a deletion plan."""
    from unread.config import get_settings
    from unread.core.paths import install_pointer_path, unread_home
    from unread.secrets_backend import (
        BACKEND_DB,
        keychain_available,
        keychain_read,
        keychain_service,
        read_active_backend_sync,
    )

    settings = get_settings()
    home = unread_home().resolve()

    # Pointer file: only list it separately when it lives OUTSIDE the
    # install home (custom-path / current-folder install). When the
    # install is at the default `~/.unread/`, removing the home directory
    # already takes the pointer with it.
    pointer = install_pointer_path()
    pointer_resolved: Path | None = None
    if pointer.exists():
        try:
            if home != pointer.parent.resolve():
                pointer_resolved = pointer
        except OSError:
            pointer_resolved = pointer

    # Walk the install home for sizes (best-effort; symlink loops, perm
    # errors all degrade to size=0 for that subtree rather than crashing
    # plan construction).
    home_size = _dir_size(home) if home.exists() else 0

    home_entries: list[tuple[Path, int]] = []
    if home.exists():
        # Top-level entries only — keeps the listing readable. Sub-tree
        # detail goes into the per-row total.
        try:
            for child in sorted(home.iterdir()):
                home_entries.append((child, _path_size(child)))
        except OSError:
            pass

    # Active backend + populated slots. We capture every populated slot
    # we can see in the keychain (regardless of which backend is active)
    # because a previous install may have left orphan rows.
    active_backend = BACKEND_DB
    db_path = settings.storage.data_path
    try:
        if db_path.is_file():
            active_backend = read_active_backend_sync(db_path)
    except Exception:
        pass

    keychain_slots: list[str] = []
    if keychain_available():
        from unread.db._keys import SECRET_KEYS

        for slot in sorted(SECRET_KEYS):
            try:
                if keychain_read(slot):
                    keychain_slots.append(slot)
            except Exception:
                pass

    # Runtime key cache. Uses the public `runtime_key_cache_path()`
    # helper so a future rename of the underlying private function
    # doesn't silently leave the cached key on disk after `killme`.
    runtime_key: Path | None = None
    try:
        from unread.security.crypto import runtime_key_cache_path

        cand = runtime_key_cache_path()
        if cand.is_file():
            runtime_key = cand
    except Exception:
        pass

    # Binary uninstall: prefer `uv tool` since that's the documented
    # install path. Detect by looking at `uv tool list`.
    binary = _detect_binary_uninstall()

    notes: list[str] = []
    if active_backend == "passphrase":
        notes.append(
            "Backend is `passphrase` — once data.sqlite is gone, the encrypted "
            "Telegram session can't be recovered. Make sure you've revoked the "
            "session in Telegram (Settings → Devices) if you care about that."
        )
    notes.append(
        f"Keychain service name [cyan]{keychain_service()}[/] will be cleared of "
        "every unread credential listed above."
    )

    return _Plan(
        install_home=home,
        install_pointer=pointer_resolved,
        install_home_size=home_size,
        home_entries=home_entries,
        keychain_slots=keychain_slots,
        runtime_key_path=runtime_key,
        binary_uninstall=binary,
        active_backend=active_backend,
        extra_notes=notes,
    )


def _print_plan(plan: _Plan) -> None:
    """Render the deletion plan to the user."""
    console.print("[bold red]unread killme[/] — full, irreversible uninstall\n")
    console.print("[bold]The following will be PERMANENTLY deleted:[/]\n")

    # Install dir.
    if plan.install_home.exists():
        console.print(f"  [bold]Install directory[/] ([grey70]{_fmt_bytes(plan.install_home_size)}[/])")
        console.print(f"    {plan.install_home}")
        for child, size in plan.home_entries:
            label = child.name + ("/" if child.is_dir() else "")
            console.print(f"      [grey70]·[/] {label:<24} [grey70]({_fmt_bytes(size)})[/]")
        console.print("")
    else:
        console.print(f"  [bold]Install directory[/] [grey70](not present at {plan.install_home})[/]\n")

    # Pointer file (only listed when separate from the install dir).
    if plan.install_pointer is not None:
        console.print("  [bold]Install pointer file[/]")
        console.print(f"    {plan.install_pointer}\n")

    # Keychain.
    if plan.keychain_slots:
        console.print(f"  [bold]OS keychain entries[/] [grey70](active backend: {plan.active_backend})[/]")
        for slot in plan.keychain_slots:
            console.print(f"    [grey70]·[/] {slot}")
        console.print("")
    else:
        console.print("  [bold]OS keychain entries[/] [grey70](none found)[/]\n")

    # Runtime key cache.
    if plan.runtime_key_path is not None:
        console.print("  [bold]Cached encryption key[/]")
        console.print(f"    {plan.runtime_key_path}\n")

    # Binary uninstall.
    if plan.binary_uninstall is not None:
        label, argv = plan.binary_uninstall
        console.print(f"  [bold]Binary uninstall[/] [grey70](via {label})[/]")
        console.print(f"    [cyan]{' '.join(argv)}[/]\n")
    else:
        console.print("  [bold]Binary uninstall[/] [grey70](skipped — no `uv tool` install detected)[/]\n")

    if plan.extra_notes:
        console.print("[bold yellow]Notes:[/]")
        for note in plan.extra_notes:
            console.print(f"  [yellow]![/] {note}")
        console.print("")

    console.print(
        "[bold red]This cannot be undone. Reports, analysis cache, message history, "
        "credentials — everything goes.[/]\n"
    )


def _confirm_killme(yes: bool) -> bool:
    """Require the user to type ``killme`` (or pass `--yes`).

    A simple Y/N prompt isn't enough here — a fat-fingered y on the
    wrong terminal would wipe months of cached analyses. We require
    typing the literal string `killme`, which matches the command name
    so the muscle-memory cost is small but the accidental-keypress cost
    is high.
    """
    if yes:
        console.print("[yellow]--yes given; skipping the type-in confirmation.[/]")
        return True
    if not sys.stdin.isatty():
        console.print(
            "[red]Refusing to run non-interactively without --yes.[/] "
            "Either run from a TTY or pass `--yes` to skip the type-in."
        )
        return False
    try:
        typed = input("Type 'killme' to confirm (anything else cancels): ").strip()
    except (KeyboardInterrupt, EOFError):
        console.print("")
        return False
    return typed == "killme"


def _detect_binary_uninstall() -> tuple[str, list[str]] | None:
    """Return (label, argv) for the right uninstall command, or None.

    Today we recognise `uv tool install`. pipx / pip / brew installs
    fall through to None — the plan just prints a "uninstall manually"
    note instead of guessing wrong.
    """
    uv_path = shutil.which("uv")
    if uv_path is None:
        return None
    # `uv tool list` lists installed tools in a stable form. Parsing
    # output isn't strictly needed; we just want to confirm the user
    # has `uv tool install`-ed `unread` so the uninstall actually does
    # something (uv exits 0 with a "not installed" message either way,
    # but skipping the call is tidier).
    try:
        res = subprocess.run(
            [uv_path, "tool", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    listed = (res.stdout or "") + "\n" + (res.stderr or "")
    if "unread" not in listed:
        return None
    return ("uv tool", [uv_path, "tool", "uninstall", "unread"])


def _path_size(path: Path) -> int:
    """File size, or recursive directory size, in bytes. 0 on error."""
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return _dir_size(path)
    except OSError:
        pass
    return 0


def _dir_size(root: Path) -> int:
    """Recursive byte count of a directory subtree."""
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(root):
            base = Path(dirpath)
            for name in files:
                with contextlib.suppress(OSError):
                    total += (base / name).stat().st_size
    except OSError:
        return total
    return total


def _fmt_bytes(n: int) -> str:
    """Compact human-readable byte string."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    val = float(n) / 1024
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{n} B"
