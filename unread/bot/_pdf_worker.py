"""Subprocess worker for PDF rendering.

Run as ``python -m unread.bot._pdf_worker``. Reads a JSON payload
{"md": ..., "title": ..., "css": ...} from stdin, writes PDF bytes
to stdout, status to stderr.

Isolating the WeasyPrint + Cairo/Pango ctypes call into a subprocess
means a renderer segfault (a known hazard on Apple Silicon when the
WeasyPrint loader can't find the right Pango build) kills only this
worker — the parent bot catches the non-zero exit code and falls
back to a `.md` upload instead of crashing the whole process.
"""

from __future__ import annotations

import json
import os
import sys


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        sys.stderr.write(f"pdf_worker: invalid stdin payload: {e}\n")
        return 2

    md_text = payload.get("md", "")
    title = payload.get("title", "Report")
    css = payload.get("css", "")

    try:
        from markdown_it import MarkdownIt
        from weasyprint import CSS, HTML
    except Exception as e:
        sys.stderr.write(f"pdf_worker: dependency import failed: {e}\n")
        return 3

    try:
        md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(["table", "strikethrough"])
        html_body = md.render(md_text)
        safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
        html_doc = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<title>{safe_title}</title>"
            "</head><body>"
            f"{html_body}"
            "</body></html>"
        )
        stylesheets = [CSS(string=css)] if css else None
        pdf_bytes = HTML(string=html_doc).write_pdf(stylesheets=stylesheets)
    except Exception as e:
        sys.stderr.write(f"pdf_worker: render failed: {type(e).__name__}: {e}\n")
        return 4

    # Write PDF bytes to stdout (binary). On Windows the buffer would
    # need explicit binary handling, but the bot runs Linux/macOS.
    try:
        sys.stdout.buffer.write(pdf_bytes)
        sys.stdout.buffer.flush()
    except Exception as e:
        sys.stderr.write(f"pdf_worker: stdout write failed: {e}\n")
        return 5
    return 0


if __name__ == "__main__":
    # Force unbuffered stdout for the binary PDF payload.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(_main())
