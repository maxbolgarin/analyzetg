"""Subprocess-safe environment construction.

The CLI invokes a handful of unrelated binaries (`ffmpeg`, `fdesetup`,
`uv tool uninstall`, etc.) via `subprocess.run` / `create_subprocess_exec`.
Without an explicit `env=`, those children inherit the parent process's
entire environment — including every API key the user has exported in
their shell or that we loaded from `~/.unread/.env`. On a multi-user
host these env vars can then leak via `ps -e auxe`, `/proc/<pid>/environ`,
or just by being attached to a child that crashes and core-dumps.

This module's `clean_subprocess_env()` returns a copy of `os.environ`
with the known-secret variable names stripped. Callers that exec our
own binary (e.g. `unread watch` re-execing `unread analyze`) keep the
full environment via the regular `subprocess.run(...)` call; only the
unrelated-tool sites use this helper.
"""

from __future__ import annotations

import os

# Env-var names that may carry user secrets. Anything matching these
# (case-sensitive) is dropped before passing the env down to a child.
# Match by name rather than by value because the leak surface is the
# variable presence, not the value shape — `ps auxe` shows names too.
_SECRET_ENV_NAMES: frozenset[str] = frozenset(
    {
        # Provider keys
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        # Telegram credentials
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_SESSION",
        "TELEGRAM_SESSION_STRING",
        # Passphrase backend
        "UNREAD_PASSPHRASE",
        # Generic (catch-all for user-supplied custom keys)
        "AZURE_OPENAI_KEY",
        "AZURE_OPENAI_API_KEY",
    }
)


def clean_subprocess_env(extra_drop: frozenset[str] | None = None) -> dict[str, str]:
    """Return a dict suitable for passing as `env=` to a child process.

    Drops every variable whose name appears in `_SECRET_ENV_NAMES` so
    a subprocess for an unrelated tool (ffmpeg, fdesetup, package
    manager) doesn't carry the user's API keys in its environment.
    `extra_drop` lets a caller suppress additional names without
    needing to edit this module.
    """
    drop = _SECRET_ENV_NAMES if not extra_drop else (_SECRET_ENV_NAMES | extra_drop)
    return {k: v for k, v in os.environ.items() if k not in drop}


__all__ = ["clean_subprocess_env"]
