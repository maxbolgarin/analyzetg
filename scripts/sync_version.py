#!/usr/bin/env python3
"""Write a release version into pyproject.toml and unread/__init__.py.

Invoked from semantic-release's `@semantic-release/exec` plugin during the
`prepare` step. The `@semantic-release/git` plugin then commits the
modified files alongside CHANGELOG.md so the published wheel and the
git tag agree on the version string.

Usage:
    python scripts/sync_version.py 1.2.3
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml", r'^version\s*=\s*"[^"]+"', 'version = "{v}"'),
    ("unread/__init__.py", r'^__version__\s*=\s*"[^"]+"', '__version__ = "{v}"'),
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sync_version.py <version>", file=sys.stderr)
        return 2
    version = argv[1].strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", version):
        print(f"refusing to write non-semver version: {version!r}", file=sys.stderr)
        return 2

    failures: list[str] = []
    for relpath, pattern, replacement in TARGETS:
        path = ROOT / relpath
        original = path.read_text(encoding="utf-8")
        new_text, count = re.subn(
            pattern,
            replacement.format(v=version),
            original,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            failures.append(f"{relpath}: pattern {pattern!r} matched {count} times (expected 1)")
            continue
        path.write_text(new_text, encoding="utf-8")
        print(f"updated {relpath} -> {version}")

    if failures:
        for line in failures:
            print(f"sync_version: {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
