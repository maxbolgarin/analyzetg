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


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent.

    Also exports ``UNREAD_VERBOSE=1`` into the environment when called
    with ``verbose=True`` so other modules (notably ``cli._run``'s
    top-level error handler) can decide whether to render a Rich
    traceback or a friendly one-liner without re-plumbing the flag
    through every command body.

    When ``settings.logging.file_path`` is set, attaches a
    ``RotatingFileHandler`` so structlog events ALSO land in that file
    (plain text, same redactor pipeline). Rotation is governed by
    ``settings.logging.file_max_bytes`` and ``file_backup_count``.
    """
    if verbose:
        os.environ["UNREAD_VERBOSE"] = "1"
    level = logging.DEBUG if verbose or os.environ.get("UNREAD_DEBUG") else logging.INFO

    # Rich tracebacks render local variables — including any passphrase
    # / API key still on the stack — to the terminal on any unhandled
    # exception. The structlog redactor only walks top-level event-dict
    # keys, so a Rich traceback bypasses it. Gate the feature behind
    # `verbose=True` (or `UNREAD_VERBOSE=1`) so production runs default
    # to the safe boring traceback that doesn't print locals.
    rich_tracebacks_enabled = bool(verbose or os.environ.get("UNREAD_VERBOSE"))
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

    # Mute chatty libraries
    for noisy in ("telethon", "httpx", "openai", "aiosqlite"):
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
