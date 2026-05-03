<!--
Thanks for contributing! Please:
- Use a Conventional Commits prefix in the PR title (feat:, fix:, docs:, refactor:, perf:, build:, ci:, chore:, test:).
- Keep the PR focused — split unrelated work into separate PRs.
- See CONTRIBUTING.md for the full workflow.
-->

## Summary

<!-- What does this PR change, and why? Lead with the *why*. -->

## Related issue

<!-- Closes #123, or "n/a" -->

## Test plan

<!-- How did you verify this works? List the commands / scenarios. -->

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ]

## Checklist

- [ ] PR title uses a Conventional Commits prefix
- [ ] Updated `CLAUDE.md` if architecture / invariants changed
- [ ] Added or updated tests for the change
- [ ] No new network calls inside tests
