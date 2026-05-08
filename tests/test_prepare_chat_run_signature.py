"""`prepare_chat_run` / `prepare_chat_runs_per_topic` kwarg drift guard.

Both functions live in `unread/core/pipeline.py` and are called from
several command modules (`analyzer/commands.py`, `export/commands.py`,
`runner.py`, `media/commands.py`, plus self-recursive calls inside the
pipeline itself).

Failure mode this test catches: a caller passes a kwarg the prep
function doesn't accept (e.g. `source_language` was added to the
analyzer's signature, the call sites were updated, but the prep
function never grew the parameter — so each `analyze` run failed at
the call site with `TypeError: got an unexpected keyword argument
'source_language'`).

We can't easily exercise the real call paths in unit tests (lots of
client/repo setup), so we statically scan each command module for
`prepare_chat_run(...)` / `prepare_chat_runs_per_topic(...)` calls,
parse their kwargs via the AST, and verify every kwarg name is in the
target function's signature. Catches drift the moment the source is
imported.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from unread.core.pipeline import prepare_chat_run, prepare_chat_runs_per_topic

# Modules that contain calls to the prep functions. We keep this list
# explicit (rather than walking the whole package) so a new caller
# added without thought triggers a code-review nudge.
_CALLER_MODULES = [
    "unread/analyzer/commands.py",
    "unread/export/commands.py",
    "unread/runner.py",
    "unread/media/commands.py",
    # `core/pipeline.py` itself calls `prepare_chat_run` recursively
    # from `prepare_chat_runs_per_topic` — include it so the recursion
    # is also kwarg-checked.
    "unread/core/pipeline.py",
]

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _collect_call_kwargs(source: str, func_name: str) -> list[tuple[int, set[str]]]:
    """Return [(lineno, kwarg_names), …] for every call to `func_name`
    in `source`. AST-based, so comments and string literals don't
    confuse it the way grep would."""
    tree = ast.parse(source)
    out: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match plain `func_name(...)` calls (not `obj.func_name(...)`).
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == func_name:
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            out.append((node.lineno, kw_names))
    return out


@pytest.mark.parametrize(
    ("func", "func_name"),
    [
        (prepare_chat_run, "prepare_chat_run"),
        (prepare_chat_runs_per_topic, "prepare_chat_runs_per_topic"),
    ],
)
def test_call_sites_only_pass_supported_kwargs(func, func_name):
    """Every kwarg passed by every caller must exist in the prep
    function's signature. Otherwise the call raises TypeError at
    runtime — exactly the regression that broke `analyze` after
    `source_language` was added to callers but not to `prepare_chat_run`."""
    sig = inspect.signature(func)
    accepted = {
        p.name
        for p in sig.parameters.values()
        # Reject if the function accepts **kwargs — we want exact
        # name checking here; **kwargs would let everything through.
        if p.kind != inspect.Parameter.VAR_KEYWORD
    }
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if has_var_kw:
        pytest.skip(f"{func_name} accepts **kwargs; static check is moot")

    failures: list[str] = []
    for rel in _CALLER_MODULES:
        path = _REPO_ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text()
        for lineno, kw_names in _collect_call_kwargs(source, func_name):
            extra = kw_names - accepted
            if extra:
                failures.append(f"{rel}:{lineno} passes unsupported {sorted(extra)}")
    assert not failures, "kwarg drift:\n  " + "\n  ".join(failures)
