# Contributing to `unread`

Thanks for considering a contribution. This file is the short version
for first-time contributors. The architecture-deep `CLAUDE.md` at the
repo root is the source of truth for invariants and module layout.

## Development setup

`unread` targets Python 3.11+ and is managed with [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/maxbolgarin/unread.git
cd unread
uv sync --extra dev
```

That gives you `pytest`, `pytest-asyncio`, and `ruff` alongside runtime
deps. Use `uv run <cmd>` to invoke any tool inside the project's
virtualenv.

To install your working tree as the global `unread` CLI for end-to-end
testing:

```bash
uv tool install --editable . --reinstall
unread doctor   # preflight check
```

## Running tests and linters

```bash
uv run pytest -q                          # full suite (pytest-asyncio is in auto mode)
uv run pytest -q tests/test_chunker.py    # one file
uv run pytest -q -k "ask and not wizard"  # filter by name expression
uv run ruff check .                       # lint (CI gate)
uv run ruff format --check .              # format check (CI gate)
uv run ruff format .                      # apply formatting
```

Tests must run offline: never introduce a real network call from inside
a test.

## Commit messages

The release pipeline uses [Conventional Commits](https://www.conventionalcommits.org)
to compute the next version and generate the changelog. Use one of:

| Prefix      | When to use                                          | Release bump |
| ----------- | ---------------------------------------------------- | ------------ |
| `feat:`     | New user-visible capability                          | minor        |
| `fix:`      | Bug fix                                              | patch        |
| `perf:`     | Performance improvement                              | patch        |
| `refactor:` | Internal change with no user-visible behavior change | patch        |
| `docs:`     | Documentation only                                   | patch        |
| `build:`    | Packaging / build config                             | patch        |
| `test:`     | Test-only changes                                    | none         |
| `ci:`       | CI workflow changes                                  | none         |
| `chore:`    | Maintenance, deps, formatting                        | none         |

A footer line `BREAKING CHANGE: <description>` (or `!` after the type:
`feat!: …`) triggers a major bump. Use sparingly — there are no real
users to migrate yet, but be deliberate when 1.0 is out.

## Pull requests

- Branch off `main`. Keep PRs focused — split unrelated changes.
- Run `ruff check`, `ruff format --check`, and `pytest -q` locally
  before opening the PR.
- If you change architecture (new module, moved responsibility,
  changed invariant) update `CLAUDE.md` in the same PR.
- The PR description should explain *why* — bugs and refactors decay,
  but the rationale stays useful.

## Adding a new preset

Presets live at `presets/<lang>/<name>.md`. Each file has YAML-ish
frontmatter:

```markdown
---
name: my_preset
prompt_version: v1
description: One-line summary of what this preset does
---

System prompt body here…
```

If you only edit a single preset's body, bump that preset's
`prompt_version` so the analysis cache invalidates. If you change
`_base.md`, `_reduce.md`, or anything in `compose_system_prompt`,
bump `BASE_VERSION` in `unread/analyzer/prompts.py` instead — it
invalidates every preset's cache.

Preset language directories are autonomous: a preset added to
`presets/en/` is **not** automatically available under `presets/ru/`
(or vice versa). Add it under each language you want it to ship in.

## Adding a new AI provider

1. Add a wrapper in `unread/ai/` implementing the `ChatProvider`
   protocol (see `openai_provider.py` for the canonical shape).
2. Wire it into `make_chat_provider` in `unread/ai/providers.py`.
3. Add a test in `tests/test_ai_providers.py` covering provider
   selection, model resolution, and the missing-credential path.

If your provider doesn't supply Whisper / vision / embeddings, the
existing OpenAI special-cases in `enrich/audio.py`, `enrich/image.py`,
and `ask/embeddings.py` will continue to gate on `settings.openai.api_key`
— that's intentional, see `CLAUDE.md` "AI provider routing".

## Reporting bugs and requesting features

Use the GitHub issue templates — they prompt for the version
(`unread --version`), `unread doctor` output, OS, and reproduction
steps that make triage tractable.
