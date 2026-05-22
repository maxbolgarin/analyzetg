"""Logging: structlog with a Rich console handler."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

console = Console()


# Allowed values for `[logging] mode` / `UNREAD_LOG_MODE`. Order matters
# for noise level (silent < normal < verbose < debug).
LOG_MODES: tuple[str, ...] = ("silent", "normal", "verbose", "debug")

_MODE_TO_LEVEL: dict[str, int] = {
    "silent": logging.ERROR,
    "normal": logging.WARNING,
    "verbose": logging.INFO,
    "debug": logging.DEBUG,
}

# Module-level mode that `status_print` / `is_silent` consult. Mutated by
# `setup_logging` and `set_log_mode` (the latter is mainly for tests).
_active_mode: str = "normal"


def is_silent() -> bool:
    """True when the current mode suppresses status-arrow output.

    The arrow-line `console.print(...)` call sites in pipelines route
    through `status_print` and check this. The Rich `Progress` instances
    pass `disable=is_silent()` at construction so they're hidden too.
    """
    return _active_mode == "silent"


def get_log_mode() -> str:
    """Return the current mode — useful for tests and for code that wants
    to branch on mode without going through `is_silent()`."""
    return _active_mode


def set_log_mode(mode: str) -> None:
    """Set the active mode without re-running `setup_logging` (which
    would also rebuild handlers). Used by tests; production code should
    call `setup_logging(mode=...)` instead so the structlog level updates
    in lockstep."""
    global _active_mode
    if mode not in LOG_MODES:
        raise ValueError(f"invalid log mode {mode!r}; expected one of {LOG_MODES}")
    _active_mode = mode


def status_print(*args: Any, **kwargs: Any) -> None:
    """Print a high-level status arrow / progress line, unless the
    current mode is `silent`. Delegates to the shared Rich `console`
    so markup, color, and Rich object rendering all work as before."""
    if is_silent():
        return
    console.print(*args, **kwargs)


def resolve_cli_log_mode(*, quiet: bool, verbose: bool, debug: bool) -> str | None:
    """Map the three CLI booleans onto a mode name, or `None` when no
    flag was passed (so the caller falls through to env / config).

    Conflict policy:
      * `-q` with `-v` or `--debug` → reject (ambiguous).
      * `-v` with `--debug` → take `debug` (both mean 'more', debug is
        the superset, so this is a forgiving merge rather than a bug).
    """
    if quiet and (verbose or debug):
        raise ValueError("cannot combine --quiet with --verbose / --debug")
    if debug:
        return "debug"
    if verbose:
        return "verbose"
    if quiet:
        return "silent"
    return None


def resolve_log_mode(*, cli_flag: str | None, settings_mode: str) -> str:
    """Resolve the effective log mode using the precedence chain:
    CLI flag > `UNREAD_LOG_MODE` env > config setting > `"normal"`.

    Invalid CLI flag → raises (caller error).
    Invalid env value → silently ignored (env vars come from arbitrary
    shells; raising would brick every command).
    """
    if cli_flag is not None:
        if cli_flag not in LOG_MODES:
            raise ValueError(f"invalid CLI log mode {cli_flag!r}; expected one of {LOG_MODES}")
        return cli_flag
    env_value = os.environ.get("UNREAD_LOG_MODE", "").strip().lower()
    if env_value in LOG_MODES:
        return env_value
    if settings_mode in LOG_MODES:
        return settings_mode
    return "normal"


# Regex shapes for common secret-bearing strings. Any value in a
# log event matching one of these gets masked before rendering.
# Tuned for false-negatives on harmless strings, false-positives are
# fine since redaction is one-way and visible in the output.
_SECRET_VALUE_RE = re.compile(
    r"""
    (?:
        sk-(?:ant-|or-|proj-)?[A-Za-z0-9_\-]{16,}  # OpenAI / Anthropic / OpenRouter
        | AIza[A-Za-z0-9_\-]{30,}                  # Google API
        | gsk_[A-Za-z0-9]{20,}                     # Groq
        | sk_(?:live|test)_[A-Za-z0-9]{20,}        # generic Stripe-style
    )
    """,
    re.VERBOSE,
)

# Event-dict keys that ALWAYS get masked, regardless of value shape.
# Catches Telethon session strings, api_hash values, etc. that don't
# match a regex but live behind an obvious key name.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_hash",
        "apikey",
        "secret",
        "token",
        "password",
        "passphrase",
        "session_string",
        "auth_key",
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "openrouter_api_key",
    }
)

_REDACTED = "***REDACTED***"


def _redact_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """structlog processor: mask secret-shaped values and known-secret keys.

    Last line of defense — modules should still avoid logging raw
    credentials. Walks nested dicts/lists/tuples up to depth 6 so
    common `extra={"payload": {...}}` patterns, Telethon's nested
    error structures, and deeply-nested provider response payloads
    don't bypass the filter. Depth cap keeps the cost bounded on
    every log call (a malicious caller can't pin the logger by
    handing in a depth-1000 nested dict).
    """

    def _scrub(key: str | None, value: Any, depth: int) -> Any:
        # Match by key first — covers Telethon session strings, api_hash,
        # etc. that don't match the regex but live behind an obvious key.
        if isinstance(key, str) and key.lower() in _SECRET_KEYS:
            return _REDACTED if value else value
        if isinstance(value, str) and value:
            masked = _SECRET_VALUE_RE.sub(_REDACTED, value)
            return masked
        if depth <= 0:
            return value
        if isinstance(value, dict):
            return {k: _scrub(k if isinstance(k, str) else None, v, depth - 1) for k, v in value.items()}
        if isinstance(value, list | tuple):
            cls = type(value)
            return cls(_scrub(None, item, depth - 1) for item in value)
        return value

    for key, value in list(event_dict.items()):
        event_dict[key] = _scrub(key, value, depth=6)
    return event_dict


# Module-level handle so repeat `setup_logging` calls swap the file
# instead of stacking handlers. Each call closes the prior handler
# (if any) before attaching a fresh one — keeps tests clean and avoids
# leaking file descriptors when the wizard re-runs `setup_logging`.
_FILE_HANDLER: logging.handlers.RotatingFileHandler | None = None
# Plain-text renderer mirrors the console one without ANSI colors. Built
# once and reused by the file-emitting structlog processor.
_PLAIN_RENDERER = structlog.dev.ConsoleRenderer(colors=False)


def _close_file_handler() -> None:
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        with contextlib.suppress(Exception):
            _FILE_HANDLER.close()
        _FILE_HANDLER = None


def _file_emit_processor_factory(handler: logging.Handler):
    """Build a structlog processor that copies each event to ``handler``.

    Sits BEFORE the colored ConsoleRenderer in the structlog pipeline,
    so it sees the post-redaction event dict. Renders a plain-text
    (no-color) copy of the same event and pushes it through the stdlib
    handler — which is what gives us the rotation policy that
    `RotatingFileHandler` enforces. The processor itself does not
    modify the event dict — the colored renderer downstream still gets
    exactly what it would have without file logging enabled.
    """

    def _processor(logger: Any, method_name: str, event_dict: dict) -> dict:
        # ConsoleRenderer mutates the dict it receives. Pass a shallow
        # copy so the colored renderer downstream still sees every key.
        rendered = _PLAIN_RENDERER(logger, method_name, dict(event_dict))
        # Map structlog's level name → stdlib level so handler-level
        # filters (when the user lowers them later) still work.
        level_name = (event_dict.get("level") or method_name or "info").upper()
        level_no = logging.getLevelName(level_name)
        if not isinstance(level_no, int):
            level_no = logging.INFO
        record = logging.LogRecord(
            name="unread",
            level=level_no,
            pathname="",
            lineno=0,
            msg=rendered,
            args=None,
            exc_info=None,
        )
        # File-system pressure / rotation race must not poison the
        # console output — swallow and keep going.
        with contextlib.suppress(Exception):
            handler.handle(record)
        return event_dict

    return _processor


def _resolve_file_handler(level: int) -> logging.handlers.RotatingFileHandler | None:
    """Build (or reuse) a `RotatingFileHandler` from settings, or return None.

    Returns None when:
    - `[logging] file_path` is not set,
    - the path can't be opened (parent missing + unwritable, perms,
      read-only mount): the function logs a warning to stderr and
      degrades to terminal-only rather than refusing to start.

    Closes any prior file handler so repeat `setup_logging` calls
    (tests, wizard re-init) don't leak file descriptors.
    """
    _close_file_handler()
    try:
        from unread.config import get_settings

        settings = get_settings()
        cfg = getattr(settings, "logging", None)
        path_raw: Path | str | None = getattr(cfg, "file_path", None) if cfg is not None else None
        if not path_raw:
            return None
        path = Path(path_raw).expanduser()
        max_bytes = int(getattr(cfg, "file_max_bytes", 10_000_000))
        backup_count = int(getattr(cfg, "file_backup_count", 3))
    except Exception:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_handler = logging.handlers.RotatingFileHandler(
            filename=str(path),
            maxBytes=max(0, max_bytes),
            backupCount=max(0, backup_count),
            encoding="utf-8",
            delay=True,  # only open the file on first write
        )
        new_handler.setLevel(level)
        # Plain renderer already builds the full line; the formatter
        # just emits it verbatim so we don't double-stamp the level
        # / time prefix.
        new_handler.setFormatter(logging.Formatter("%(message)s"))
    except OSError as e:
        import sys

        print(
            f"warning: could not open log file {path} ({e}); continuing terminal-only.",
            file=sys.stderr,
        )
        return None
    global _FILE_HANDLER
    _FILE_HANDLER = new_handler
    return new_handler


def setup_logging(mode: str = "normal") -> None:
    """Configure structlog + stdlib logging. Idempotent.

    ``mode`` is one of ``silent`` / ``normal`` / ``verbose`` / ``debug``;
    see :class:`unread.config.LoggingCfg` for the matrix. Also exports
    ``UNREAD_DEBUG=1`` into the environment when ``mode == "debug"`` so
    other modules (notably ``cli._run``'s top-level error handler) can
    decide whether to render a Rich traceback or a friendly one-liner
    without re-plumbing the flag through every command body.

    When ``settings.logging.file_path`` is set, attaches a
    ``RotatingFileHandler`` so structlog events ALSO land in that file
    (plain text, same redactor pipeline). Rotation is governed by
    ``settings.logging.file_max_bytes`` and ``file_backup_count``.
    """
    if mode not in LOG_MODES:
        raise ValueError(f"invalid log mode {mode!r}; expected one of {LOG_MODES}")
    global _active_mode
    _active_mode = mode

    level = _MODE_TO_LEVEL[mode]
    # `verbose` and below intentionally do NOT set UNREAD_DEBUG — Rich
    # tracebacks expose local-variable values (including API keys) and
    # must be opt-in via `--debug` so a user reaching for `verbose`
    # doesn't pay that security cost unexpectedly.
    if mode == "debug":
        os.environ["UNREAD_DEBUG"] = "1"
    rich_tracebacks_enabled = mode == "debug"
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=rich_tracebacks_enabled,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Mute chatty libraries. markdown_it floods DEBUG with per-token
    # `entering <rule>:` lines whenever Rich renders a report; httpcore /
    # urllib3 / asyncio / PIL are the usual suspects behind the rest of
    # the DEBUG noise from third-party packages.
    for noisy in (
        "telethon",
        "httpx",
        "httpcore",
        "urllib3",
        "openai",
        "aiosqlite",
        "asyncio",
        "markdown_it",
        "PIL",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    file_handler = _resolve_file_handler(level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
    ]
    if file_handler is not None:
        # File processor must sit AFTER the redactor (so secrets are
        # already scrubbed) and BEFORE the colored ConsoleRenderer
        # (which mutates the event dict).
        processors.append(_file_emit_processor_factory(file_handler))
    processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name) if name else structlog.get_logger()
