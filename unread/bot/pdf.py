"""Markdown → PDF conversion for bot report delivery.

The actual rendering happens in a `unread.bot._pdf_worker` subprocess.
WeasyPrint's ctypes binding to Pango / Cairo can segfault when the
shared libraries aren't quite right (a known hazard on macOS Apple
Silicon when Homebrew's `lib/` isn't on the dynamic loader path) —
and a segfault inside the bot's main process would take down the
whole bot mid-request, AFTER the user already got "Sending report…".
Subprocess isolation means we catch a non-zero exit / killed worker
and fall back to a `.md` upload instead.

`is_available()` does a real test render in the subprocess too, so a
broken Pango install reports unavailable instead of falsely claiming
PDF works and then exploding on the first real request.
"""

from __future__ import annotations

import json
import subprocess
import sys

import structlog

log = structlog.get_logger(__name__)


# Minimal print-friendly stylesheet. Kept here (not in the worker) so
# we can tune the look without rebuilding the worker entry point.
# System fonts only — no TTF to ship in the container.
_PDF_CSS = """
@page {
    size: A4;
    margin: 1.6cm 1.8cm;
}
body {
    font-family: -apple-system, "Helvetica Neue", "Segoe UI", "Liberation Sans", sans-serif;
    font-size: 11pt;
    line-height: 1.45;
    color: #222;
}
h1, h2, h3 { color: #111; }
h1 { font-size: 1.5em; margin-top: 0.2em; }
h2 { font-size: 1.2em; margin-top: 1.2em; border-bottom: 1px solid #ddd; padding-bottom: 0.2em; }
h3 { font-size: 1.05em; margin-top: 1em; }
a { color: #0a64c2; text-decoration: none; }
a:hover { text-decoration: underline; }
ul, ol { margin: 0.4em 0 0.8em 1.4em; padding: 0; }
li { margin: 0.2em 0; }
code {
    font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.92em;
    background: #f4f4f5;
    padding: 0 0.25em;
    border-radius: 3px;
}
pre {
    background: #f7f7f8;
    border: 1px solid #e2e2e4;
    border-radius: 4px;
    padding: 0.6em 0.8em;
    overflow-x: auto;
    font-size: 0.88em;
}
pre code { background: transparent; padding: 0; }
hr { border: 0; border-top: 1px solid #ddd; margin: 1em 0; }
table { border-collapse: collapse; width: 100%; margin: 0.6em 0; }
th, td { border: 1px solid #ddd; padding: 0.35em 0.55em; text-align: left; }
th { background: #f5f5f7; }
blockquote {
    border-left: 3px solid #ccc;
    margin: 0.5em 0;
    padding: 0.1em 0.9em;
    color: #444;
}
"""


# Probe result memoized on first call so we don't pay subprocess
# overhead per request.
_AVAILABLE_CACHED: bool | None = None


def is_available() -> bool:
    """True iff a real test-render in the worker subprocess succeeds.

    Importing weasyprint isn't enough — its ctypes bindings load
    Pango / Cairo lazily, and a broken loader path (common on macOS
    when Homebrew's lib isn't visible to the dynamic linker)
    segfaults at render time, not import time. We do a tiny test
    render here so a misconfigured host correctly reports `False`
    instead of falsely claiming PDF works and then crashing the bot
    on the first real request.

    Memoized — the answer doesn't change within a single process
    lifetime, and we don't want to fork a subprocess per request.
    """
    global _AVAILABLE_CACHED
    if _AVAILABLE_CACHED is not None:
        return _AVAILABLE_CACHED
    try:
        _render_via_worker("# probe\n", title="probe", timeout=10)
        _AVAILABLE_CACHED = True
    except Exception as e:
        log.warning(
            "bot.pdf_probe_failed",
            error=str(e),
            hint=(
                "weasyprint can't render PDFs on this host. Linux: "
                "`apt-get install libpango-1.0-0 libpangoft2-1.0-0`. "
                "macOS Apple Silicon: `brew install pango` AND export "
                "`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_FALLBACK_LIBRARY_PATH` "
                "before running `unread bot run`."
            ),
        )
        _AVAILABLE_CACHED = False
    return _AVAILABLE_CACHED


def markdown_to_pdf_bytes(md_text: str, *, title: str = "Report") -> bytes:
    """Render `md_text` to PDF bytes via the worker subprocess.

    Strips the leading `---` frontmatter so it flows as readable
    text instead of being rendered as two empty horizontal rules
    around raw key/value lines.

    Raises `RuntimeError` on subprocess failure (segfault, missing
    libs, timeout) — caller is expected to catch and fall back.
    """
    body = _strip_frontmatter_as_pdf_preamble(md_text)
    return _render_via_worker(body, title=title, timeout=60)


def _render_via_worker(md_text: str, *, title: str, timeout: int) -> bytes:
    """Spawn the worker, capture PDF bytes off stdout."""
    payload = json.dumps({"md": md_text, "title": title, "css": _PDF_CSS})
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unread.bot._pdf_worker"],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"PDF worker timed out after {timeout}s") from e
    except OSError as e:
        raise RuntimeError(f"PDF worker spawn failed: {e}") from e

    if proc.returncode != 0:
        # rc -11 / -SIGSEGV is the typical macOS Pango misconfig.
        # Bubble up the stderr text so logs show the real cause.
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PDF worker rc={proc.returncode}: {stderr_tail or '(no stderr)'}")
    if not proc.stdout:
        raise RuntimeError("PDF worker returned empty output")
    return proc.stdout


def _strip_frontmatter_as_pdf_preamble(md_text: str) -> str:
    """Convert a leading `---\\n...\\n---` block into plain prose lines."""
    if not md_text.startswith("---"):
        return md_text
    lines = md_text.splitlines()
    if len(lines) < 2:
        return md_text
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return md_text
    preamble = "\n".join(lines[1:end_idx])
    rest = "\n".join(lines[end_idx + 1 :])
    return f"{preamble}\n\n---\n{rest}"
