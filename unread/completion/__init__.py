"""User-facing commands for installing shell tab-completion.

The `completion` subcommand group wraps Typer's completion machinery
(`typer.completion.install` / `get_completion_script`) so users can
turn on tab-completion without the `--install-completion` /
`--show-completion` flags appearing on the root callback's flag list.
"""
