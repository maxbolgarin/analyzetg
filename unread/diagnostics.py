"""Diagnostic-bundle helper for `unread bug-report`.

Generates a single block of text the user can paste into a GitHub issue.
The bundle includes version, Python/platform, doctor output, cache
sizes, and the contents of `~/.unread/.env` and `~/.unread/config.toml`
with every secret value masked.

The redaction is intentionally aggressive: anything that contains a
SECRET_KEYS-derived token in its key name OR matches a known
secret-shaped pattern (e.g. `sk-…`, `r-…` for OpenRouter, long hex
runs) is replaced with `***redacted***` before printing.
"""

from __future__ import annotations

import contextlib
import io
import platform
import re
from pathlib import Path

from unread import __version__
from unread.db._keys import SECRET_KEYS

# Match any leaf segment of the secret keys (e.g. "api_key", "api_id",
# "api_hash"). Anything whose key contains one of these is redacted.
_SECRET_SEGMENTS: frozenset[str] = frozenset({key.split(".")[-1] for key in SECRET_KEYS})

_REDACTED = "***redacted***"

# Heuristic patterns for value-shaped secrets that may appear in logs
# even when the key name doesn't include `api_*`. Order matters — more
# specific patterns first so they win against the looser ones.
_VALUE_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),  # Anthropic
    re.compile(r"\bsk-or-v\d+-[A-Za-z0-9_\-]{20,}\b"),  # OpenRouter (v1+)
    re.compile(r"\bsk-or-[A-Za-z0-9_\-]{20,}\b"),  # OpenRouter (legacy)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),  # OpenAI / generic
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"),  # Google
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),  # Hugging Face
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{16,}", re.IGNORECASE),  # generic Bearer
    # Telegram bot tokens: <numeric_id>:<35-46 base64-ish>. Anchored on the
    # `:` separator so we don't eat ordinary numbers.
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,46}\b"),
)

# Key-name segments that should redact the *value* on any TOML / .env
# line whose key contains them, even when the value isn't shaped like a
# known token. Caught here in addition to `_SECRET_SEGMENTS` (declared
# above from `SECRET_KEYS`) so generic fields like `password` and
# `secret` are covered without depending on the typed-secret allowlist.
_GENERIC_SECRET_NAME_TOKENS: frozenset[str] = frozenset({"password", "secret", "token", "credential", "auth"})


def redact_text(raw: str) -> str:
    """Redact secret-shaped substrings from arbitrary text."""

    out = raw
    for pat in _VALUE_SHAPE_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def redact_config_file(path: Path) -> str:
    """Read a config / env file and return its content with secrets masked.

    Handles both `key = "value"` (TOML) and `KEY=value` (.env) shapes.
    Lines whose key contains a secret-segment token (`api_key`,
    `api_id`, `api_hash`, …) get their value replaced with the
    redaction sentinel; other lines pass through (still subject to
    value-shape redaction).
    """
    if not path.exists():
        return f"(file not present: {path})\n"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(could not read {path}: {e})\n"

    out_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        # `key = ...` (TOML) or `KEY=value` (.env). The leading
        # whitespace of TOML preserves indentation in nested tables.
        m = re.match(r"^(\s*)([A-Za-z0-9_.\-]+)(\s*[:=]\s*)(.*)$", line)
        if not m:
            out_lines.append(redact_text(line))
            continue
        indent, key, sep, value = m.groups()
        leaf = key.split(".")[-1].lower()
        # Match the leaf against the typed-secret allowlist OR the
        # generic-name tokens. Substring containment so .env-style
        # flat keys like `TELEGRAM_API_HASH=…` (whose leaf is
        # `telegram_api_hash`, not `api_hash`) are still caught — same
        # for custom `[smtp] password = …` lines in config.toml.
        is_secret_key = (
            leaf in _SECRET_SEGMENTS
            or any(t in leaf for t in _SECRET_SEGMENTS)
            or any(t in leaf for t in _GENERIC_SECRET_NAME_TOKENS)
        )
        if is_secret_key:
            # Preserve the original quoting style so the file still
            # parses if the user edits it after pasting.
            if value.startswith('"') and value.endswith('"'):
                masked = f'"{_REDACTED}"'
            elif value.startswith("'") and value.endswith("'"):
                masked = f"'{_REDACTED}'"
            else:
                masked = _REDACTED
            out_lines.append(f"{indent}{key}{sep}{masked}")
        else:
            out_lines.append(redact_text(line))
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


def collect_log_tail(log_path: Path | None, max_lines: int = 100) -> str:
    """Read the last `max_lines` lines of a log file, redacting secrets."""
    if log_path is None or not log_path.exists():
        return (
            "(no file logging configured — set `[logging] file_path` in config.toml to capture future runs)\n"
        )
    try:
        # Cheap tail: read the whole file if small, otherwise seek.
        size = log_path.stat().st_size
        if size <= 256 * 1024:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        else:
            with log_path.open("rb") as fh:
                fh.seek(-256 * 1024, io.SEEK_END)
                text = fh.read().decode("utf-8", errors="replace")
                # Drop the (likely-partial) first line.
                text = text.split("\n", 1)[1] if "\n" in text else text
    except OSError as e:
        return f"(could not read {log_path}: {e})\n"
    lines = text.splitlines()
    tail = "\n".join(lines[-max_lines:])
    return redact_text(tail) + "\n"


async def build_bug_report() -> str:
    """Compose the full bug-report bundle as a single string.

    Calls `cmd_doctor` with stdout captured. The doctor function already
    redacts api_id, doesn't print api_key values, and surfaces every
    health signal a maintainer would ask about, so reusing it avoids
    drift between the two commands.
    """
    from unread.config import get_settings
    from unread.core.paths import default_config_path, default_env_path
    from unread.tg.commands import cmd_doctor

    settings = get_settings()

    # Capture doctor output. cmd_doctor prints via the module-level
    # `console` (rich.Console) — wrap a fresh Console writing to a
    # StringIO and patch the binding for the duration of the call.
    from rich.console import Console as _Console

    import unread.tg.commands as _tg_cmds

    buf = io.StringIO()
    captured = _Console(file=buf, force_terminal=False, width=100)
    saved = _tg_cmds.console
    _tg_cmds.console = captured  # type: ignore[assignment]
    try:
        # doctor raises `typer.Exit(1)` on FAIL — that's a
        # `click.exceptions.Exit` which subclasses RuntimeError, not
        # SystemExit. We want the bundle regardless (a failing doctor
        # is the most useful bug report).
        with contextlib.suppress(SystemExit, RuntimeError):
            await cmd_doctor()
    finally:
        _tg_cmds.console = saved  # type: ignore[assignment]
    doctor_text = redact_text(buf.getvalue())

    parts: list[str] = []
    parts.append("# unread bug report")
    parts.append("")
    parts.append(f"unread version: {__version__}")
    parts.append(f"python: {platform.python_version()}")
    parts.append(f"platform: {platform.platform()}")
    parts.append("")
    parts.append("## doctor")
    parts.append("")
    parts.append(doctor_text)
    parts.append("")
    parts.append("## config.toml (redacted)")
    parts.append("")
    parts.append("```toml")
    parts.append(redact_config_file(default_config_path()))
    parts.append("```")
    parts.append("")
    parts.append("## .env (redacted)")
    parts.append("")
    parts.append("```")
    parts.append(redact_config_file(default_env_path()))
    parts.append("```")
    parts.append("")
    parts.append("## recent logs (redacted)")
    parts.append("")
    parts.append("```")
    log_path = getattr(settings, "logging_file_path", None)
    if isinstance(log_path, str) and log_path:
        log_path = Path(log_path)
    elif not isinstance(log_path, Path):
        log_path = None
    parts.append(collect_log_tail(log_path))
    parts.append("```")
    parts.append("")
    parts.append("---")
    parts.append("Paste this bundle into a new issue at:")
    parts.append("https://github.com/maxbolgarin/unread/issues/new")
    parts.append("")
    return "\n".join(parts)
