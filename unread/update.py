"""`unread update` — check PyPI for a newer release and install it.

Mirrors `killme.py`'s shape (single-file feature with detect → plan → run):

  * `fetch_latest_version()` — async PyPI JSON fetch.
  * `is_newer(latest, current)` — PEP 440 comparison.
  * `detect_install_method()` — returns (label, argv) for the upgrade
    command, or `None` when unknown.
  * `run_upgrade(argv)` — `subprocess.run` with the user's terminal
    streaming through.
  * `cmd_update(check, yes)` — sync Typer command body.

`fetch_latest_version` and `is_newer` are also reused by `cmd_doctor`
for a passive "newer version available" hint at the end of the doctor
report — keeping the network access best-effort and gated by a tight
timeout so doctor stays useful offline.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys

import httpx
import typer
from packaging.version import InvalidVersion, Version
from rich.console import Console

from unread import __version__
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.subprocess_env import clean_subprocess_env

console = Console()

PYPI_URL = "https://pypi.org/pypi/unread/json"


class UpdateCheckError(RuntimeError):
    """Raised when the PyPI version probe fails — network, HTTP, or
    parse error. Callers (cmd_update, cmd_doctor) decide how loud to be
    about it: cmd_update prints the message; doctor swallows it."""


async def fetch_latest_version(timeout: float = 5.0) -> str:
    """Fetch the latest release of `unread` from PyPI's JSON API.

    Returns the `info.version` field as a string. Raises
    `UpdateCheckError` on any network / HTTP / parse failure so callers
    can render a single friendly message instead of an opaque traceback.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(PYPI_URL)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise UpdateCheckError(str(e)) from e
    try:
        return str(data["info"]["version"])
    except (KeyError, TypeError) as e:
        raise UpdateCheckError(f"unexpected PyPI response shape: {e}") from e


def is_newer(latest: str, current: str) -> bool:
    """`True` iff `latest` is a strictly newer PEP 440 version than `current`.

    Falls back to a string comparison when either side fails to parse —
    that path is unreachable for our own published versions but keeps
    the helper robust against malformed inputs from a future PyPI
    response shape change.
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return latest != current and latest > current


def detect_install_method() -> tuple[str, list[str]] | None:
    """Return `(label, argv)` for the right upgrade command, or `None`
    when nothing matches.

    Order:
      1. `uv tool` — covers regular `uv tool install unread` AND
         `uv tool install --editable .` (both show in `uv tool list`).
      2. `pipx` — detected via `sys.executable` path heuristic.
      3. `pip` fallback — `sys.executable -m pip install --upgrade unread`.
      4. `None` — caller prints a manual instruction list.
    """
    uv = _detect_uv_tool()
    if uv is not None:
        return uv
    pipx = _detect_pipx()
    if pipx is not None:
        return pipx
    if sys.executable:
        return ("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "unread"])
    return None


def _detect_uv_tool() -> tuple[str, list[str]] | None:
    """Match `killme.py:_detect_binary_uninstall` so the install / upgrade
    detection stays in lockstep — anything reachable by `uv tool uninstall`
    is reachable by `uv tool upgrade`."""
    uv_path = shutil.which("uv")
    if uv_path is None:
        return None
    try:
        res = subprocess.run(
            [uv_path, "tool", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=clean_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    listed = (res.stdout or "") + "\n" + (res.stderr or "")
    if "unread" not in listed:
        return None
    return ("uv tool", [uv_path, "tool", "upgrade", "unread"])


def _detect_pipx() -> tuple[str, list[str]] | None:
    """Heuristic: pipx puts the install's interpreter under
    `<data-dir>/pipx/venvs/<pkg>/...`. The exact prefix differs by OS
    (`~/.local/share/pipx/venvs/` on Linux, `~/Library/Application Support/pipx/venvs/`
    on macOS, `%USERPROFILE%\\pipx\\venvs\\` on Windows), but every layout
    contains the literal substring `pipx/venvs` (or `pipx\\venvs`) — that's
    what we match.
    """
    exe = sys.executable or ""
    if "pipx/venvs" not in exe and "pipx\\venvs" not in exe:
        return None
    pipx_path = shutil.which("pipx")
    if pipx_path is None:
        return None
    return ("pipx", [pipx_path, "upgrade", "unread"])


def run_upgrade(argv: list[str]) -> int:
    """Run the upgrade subprocess inheriting the user's terminal so they
    see live progress (uv / pip print download bars). Returns the
    process exit code; `0` on success."""
    try:
        res = subprocess.run(
            argv,
            check=False,
            env=clean_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        console.print(f"[red]Upgrade subprocess failed:[/] {e}")
        return 1
    return res.returncode


def _confirm_install(yes: bool) -> bool:
    """y/N prompt. `--yes` skips. Non-TTY without --yes refuses cleanly."""
    if yes:
        return True
    if not sys.stdin.isatty():
        console.print(
            "[yellow]Refusing to install non-interactively without --yes.[/] Re-run with `--yes` to apply."
        )
        return False
    try:
        typed = input(_t("update_install_prompt")).strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("")
        return False
    return typed in ("y", "yes")


def cmd_update(*, check: bool, yes: bool) -> None:
    """Sync entry point used by the Typer command wrapper in `cli.py`.

    Flow:
      1. Fetch latest from PyPI (async, run via `asyncio.run`).
      2. Compare against `__version__`.
      3. If equal → print "up to date" and return.
      4. If newer → print the comparison line.
         - `--check` → return without installing.
         - Otherwise prompt (y/N or `--yes`) and run the detected upgrade
           subprocess. Unknown install method → print manual commands.
    """
    try:
        latest = asyncio.run(fetch_latest_version())
    except UpdateCheckError as e:
        console.print(f"[red]{_tf('update_check_failed', error=str(e))}[/]")
        raise typer.Exit(1) from None

    current = __version__
    if not is_newer(latest, current):
        console.print(_tf("update_up_to_date", version=current))
        return

    console.print(_tf("update_available", latest=latest, current=current))
    if check:
        return

    detected = detect_install_method()
    if detected is None:
        console.print(_t("update_install_unknown_method"))
        return

    label, argv = detected
    pretty = " ".join(argv)
    console.print(f"[grey70](via {label}) {_tf('update_install_running', cmd=pretty)}[/]")
    if not _confirm_install(yes):
        console.print(_t("update_install_skipped"))
        return

    rc = run_upgrade(argv)
    if rc != 0:
        raise typer.Exit(rc)
