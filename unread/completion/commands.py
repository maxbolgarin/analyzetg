"""Implementation of the `unread completion` subcommand group.

Wraps Typer's built-in completion machinery so users get tab-completion
without the `--install-completion` / `--show-completion` flags polluting
the root callback's flag list (which `unread help flags` documents).

Typer 0.24's generated zsh/fish completion scripts are broken with
Click 8.3 — they set `_TYPER_COMPLETE_ARGS` but Click's `ZshComplete`
/ `FishComplete` read `COMP_WORDS` / `COMP_CWORD`. Pressing Tab raises
a `KeyError` deep inside `click.shell_completion.get_completion_args`.
We patch the scripts here (`_PATCHED_SCRIPTS`) so the env vars Click
expects are set, while keeping Typer's `_UNREAD_COMPLETE=complete_<shell>`
instruction so Typer's dispatcher still recognizes the request. Bash
isn't affected — Typer's bash script already sets the right env vars.
"""

from __future__ import annotations

import typer
from rich.console import Console

from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import get_logger

console = Console()
log = get_logger(__name__)

# Pinned so the install-time and runtime paths agree. Typer derives a
# default from `prog_name` (uppercase + dashes→underscores) but pinning
# avoids surprises if someone renames the entry point.
_PROG_NAME = "unread"
_COMPLETE_VAR = "_UNREAD_COMPLETE"

# bash + zsh + fish: the three Typer ships with on Unix. Powershell /
# pwsh are dropped because Typer's pwsh script has the same Click-8
# env-var mismatch and we don't have a user to validate the patch.
_VALID_SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")


# Click-compatible zsh completion. Adapted from Click 8.3's stock
# `ZshComplete.source_template`, with the instruction value swapped
# back to `complete_zsh` (Typer's `shell_complete` partitions the
# value as `instruction_shell` so the wrapper still recognizes it).
_ZSH_SCRIPT = """\
#compdef unread

_unread_completion() {
  local -a completions
  local -a completions_with_descriptions
  local -a response
  (( ! $+commands[unread] )) && return 1

  response=("${(@f)$(env COMP_WORDS=\"${words[*]}\" COMP_CWORD=$((CURRENT-1)) _UNREAD_COMPLETE=complete_zsh unread)}")

  for type key descr in ${response}; do
    if [[ "$type" == "plain" ]]; then
      if [[ "$descr" == "_" ]]; then
        completions+=("$key")
      else
        completions_with_descriptions+=("$key":"$descr")
      fi
    elif [[ "$type" == "dir" ]]; then
      _path_files -/
    elif [[ "$type" == "file" ]]; then
      _path_files -f
    fi
  done

  if [ -n "$completions_with_descriptions" ]; then
    _describe -V unsorted completions_with_descriptions -U
  fi

  if [ -n "$completions" ]; then
    compadd -U -V unsorted -a completions
  fi
}

compdef _unread_completion unread
"""

# Click-compatible fish completion. Same idea: keep the
# `complete_fish` instruction value (Typer recognizes it) but set
# `COMP_WORDS` / `COMP_CWORD` so `FishComplete.get_completion_args`
# reads the cursor state correctly. Output format is Click's
# `type,value` per line.
_FISH_SCRIPT = """\
function _unread_completion;
    set -l response (env _UNREAD_COMPLETE=complete_fish COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) unread);

    for completion in $response;
        set -l metadata (string split "," $completion);

        if test $metadata[1] = "dir";
            __fish_complete_directories $metadata[2];
        else if test $metadata[1] = "file";
            __fish_complete_path $metadata[2];
        else if test $metadata[1] = "plain";
            echo $metadata[2];
        end;
    end;
end;

complete --no-files --command unread --arguments "(_unread_completion)";
"""

# Shells where we substitute our own template instead of Typer's.
# Bash falls through to `typer.completion.get_completion_script` —
# Typer's bash script already sets `COMP_WORDS` / `COMP_CWORD`.
_PATCHED_SCRIPTS: dict[str, str] = {
    "zsh": _ZSH_SCRIPT,
    "fish": _FISH_SCRIPT,
}


def _completion_script(shell: str) -> str:
    """Return the completion script for ``shell`` — patched for zsh/fish."""
    if shell in _PATCHED_SCRIPTS:
        return _PATCHED_SCRIPTS[shell]
    from typer import completion as tc

    return tc.get_completion_script(
        prog_name=_PROG_NAME,
        complete_var=_COMPLETE_VAR,
        shell=shell,
    )


def _resolve_shell(shell: str | None) -> str:
    """Validate a user-given shell name, or auto-detect via shellingham + $SHELL."""
    if shell:
        s = shell.strip().lower()
        if s not in _VALID_SHELLS:
            console.print(
                f"[red]{_t('cli_error_prefix')}[/] "
                f"{_tf('err_completion_unknown_shell', shell=repr(shell), shells=', '.join(_VALID_SHELLS))}"
            )
            raise typer.Exit(1)
        return s

    # Primary: parent-process inspection. Most accurate when it works
    # (handles users who run zsh inside a bash login shell etc.).
    detected: str | None = None
    try:
        import shellingham

        detected, _ = shellingham.detect_shell()
    except shellingham.ShellDetectionFailure:
        detected = None
    except ImportError:  # pragma: no cover — shellingham is a hard dep via Typer
        detected = None

    # Fallback: `$SHELL` env var. Loses information when the user's
    # login shell differs from the one they're typing in, but covers
    # the long tail of containers / CI / detached terminals where
    # shellingham can't walk the parent chain.
    if not detected:
        import os
        from pathlib import Path

        shell_env = os.environ.get("SHELL", "")
        if shell_env:
            detected = Path(shell_env).name

    if not detected:
        console.print(
            f"[red]Could not auto-detect your shell.[/]  Pass it explicitly: {' | '.join(_VALID_SHELLS)}."
        )
        raise typer.Exit(1)

    if detected not in _VALID_SHELLS:
        console.print(
            f"[red]Detected shell {detected!r} is not supported by Typer.[/]  "
            f"Pass one explicitly: {' | '.join(_VALID_SHELLS)}."
        )
        raise typer.Exit(1)
    return detected


def cmd_install(shell: str | None = None) -> None:
    """Install the completion script and source it from the user's rc file.

    For bash we delegate to ``typer.completion.install`` (its bash script
    is correct on Click 8). For zsh and fish we write our patched
    script ourselves and append a `source` line to the user's rc file —
    Typer's `install()` would write its own broken template otherwise.
    """
    resolved = _resolve_shell(shell)

    if resolved == "bash":
        # Typer's bash path works as-is.
        from typer import completion as tc

        try:
            shell_used, path = tc.install(
                shell=resolved,
                prog_name=_PROG_NAME,
                complete_var=_COMPLETE_VAR,
            )
        except Exception as e:
            console.print(f"[red]Install failed:[/] {e}")
            raise typer.Exit(1) from e
        console.print(f"[green]✓[/] {shell_used} completion installed at [cyan]{path}[/]")
        console.print(
            "[grey70]Restart your shell (or `exec $SHELL`) to pick it up. Type `unread <Tab>` to test.[/]"
        )
        return

    # zsh / fish: write our patched script + ensure it's sourced.
    try:
        script_path = _install_patched_script(resolved)
    except Exception as e:
        console.print(f"[red]Install failed:[/] {e}")
        raise typer.Exit(1) from e

    console.print(f"[green]✓[/] {resolved} completion installed at [cyan]{script_path}[/]")
    console.print(
        "[grey70]Restart your shell (or `exec $SHELL`) to pick it up. Type `unread <Tab>` to test.[/]"
    )


def cmd_show(shell: str | None = None) -> None:
    """Print the completion script to stdout for manual sourcing."""
    resolved = _resolve_shell(shell)
    try:
        script = _completion_script(resolved)
    except Exception as e:
        console.print(f"[red]Could not generate completion script:[/] {e}")
        raise typer.Exit(1) from e
    # Bypass rich so the shell snippet stays byte-exact when piped.
    print(script)


def _install_patched_script(shell: str) -> Path:  # noqa: F821 — Path imported lazily
    """Write the patched script + add a source line to the shell's rc file.

    Mirrors `typer.completion._install_zsh` / `_install_fish` so the
    install layout is familiar:

      • zsh  → ``~/.zfunc/_unread`` (and a ``fpath += ~/.zfunc; autoload -Uz
                compinit; compinit`` block in ``~/.zshrc``).
      • fish → ``~/.config/fish/completions/unread.fish`` (no rc edit
                needed; fish auto-loads anything in this directory).

    Idempotent: re-running overwrites the script and skips the rc edit
    when the source line is already present.
    """
    from pathlib import Path

    home = Path.home()
    if shell == "zsh":
        script_dir = home / ".zfunc"
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / "_unread"
        script_path.write_text(_ZSH_SCRIPT)
        rc_path = home / ".zshrc"
        rc_block = "\n".join(
            [
                "fpath+=~/.zfunc",
                "autoload -Uz compinit",
                "compinit",
            ]
        )
        _append_to_rc(rc_path, "fpath+=~/.zfunc", rc_block)
        return script_path

    if shell == "fish":
        script_dir = home / ".config" / "fish" / "completions"
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / "unread.fish"
        script_path.write_text(_FISH_SCRIPT)
        return script_path

    raise RuntimeError(f"unsupported patched shell: {shell!r}")


def _append_to_rc(rc_path: Path, marker: str, block: str) -> None:  # noqa: F821
    """Append ``block`` to ``rc_path`` if ``marker`` isn't already there."""
    existing = rc_path.read_text() if rc_path.is_file() else ""
    if marker in existing:
        return
    suffix = "\n" if existing and not existing.endswith("\n") else ""
    rc_path.write_text(existing + suffix + block + "\n")


def register(app: typer.Typer, panel: str) -> typer.Typer:
    """Build and register the `completion` typer subapp on the root ``app``."""
    completion_app = typer.Typer(
        help="Install shell tab-completion (bash / zsh / fish).",
        no_args_is_help=True,
    )

    @completion_app.command("install")
    def _install(
        shell: str | None = typer.Argument(
            None,
            metavar="[SHELL]",
            help="bash | zsh | fish — auto-detected when omitted.",
        ),
    ) -> None:
        """Install completion: writes the script + sources it from your rc file."""
        cmd_install(shell)

    @completion_app.command("show")
    def _show(
        shell: str | None = typer.Argument(
            None,
            metavar="[SHELL]",
            help="bash | zsh | fish — auto-detected when omitted.",
        ),
    ) -> None:
        """Print the completion script (for manual sourcing or piping)."""
        cmd_show(shell)

    app.add_typer(completion_app, name="completion", rich_help_panel=panel)
    return completion_app


__all__ = ["cmd_install", "cmd_show", "register"]
