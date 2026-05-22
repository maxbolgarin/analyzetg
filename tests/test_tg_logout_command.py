"""Confirmation + credential-purge prompts for `unread tg logout`.

Old behavior (single command, single side-effect): `unread tg logout`
unconditionally wiped the session file and printed a one-liner. Power
users could lose a working session by typoing `tg logout` instead of
`tg login`, and there was no in-tree way to also remove the persisted
api_id / api_hash — `unread security clear` was the only path, and
discoverability was poor.

New behavior pinned by these tests:

  - **Session-clear confirm** (TTY only, skipped with `--yes`): user
    must say yes before the session file goes away.
  - **Credentials purge** (TTY only, skipped with `--yes`, forced with
    `--purge-credentials`): asked AFTER the session is cleared, and
    only when at least one of `telegram.api_id` / `telegram.api_hash`
    is still persisted (no point asking when there's nothing to delete).
  - Non-TTY (CI / pipes): historical contract preserved — session
    cleared unconditionally, credentials kept unless `--purge-credentials`
    is passed (so scripted logout doesn't accidentally lock out
    re-login).
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from unread.cli import app

runner = CliRunner()


def _patches(
    *,
    had_session: bool,
    interactive: bool,
    persisted_creds: dict[str, str],
    confirm_results: list[bool],
):
    """Bundle the five module-level mocks every logout test needs.

    `confirm_results` is consumed in order — first call returns the
    first element, etc. A test that expects N prompts should provide
    exactly N return values (an extra makes the test brittle, a
    missing one raises StopIteration so the failure is loud)."""
    confirm_iter = iter(confirm_results)

    def _confirm_side_effect(*_a, **_kw):
        return next(confirm_iter)

    # Most of `logout_cmd`'s dependencies are lazy-imported inside the
    # function body, so we have to patch the SOURCE module (where the
    # symbol lives), not `unread.cli` (where the alias would only exist
    # after the import statement runs). `_session_exists` and
    # `_delete_telegram_credentials` are exceptions — both live at
    # module level in `unread.cli` and are patched directly there.
    return [
        patch("unread.tg.session_state.is_session_authorized_sync", return_value=had_session),
        patch("unread.cli._session_exists", return_value=had_session),
        patch("unread.tg.client._wipe_local_session"),
        patch("unread.util.prompt._can_interact", return_value=interactive),
        patch("unread.util.prompt.confirm", side_effect=_confirm_side_effect),
        patch("unread.secrets.read_secrets", return_value=persisted_creds),
        patch("unread.cli._delete_telegram_credentials"),
    ]


def _enter_all(patches):
    """Enter every context manager and return the list of mocks
    in the same order so the caller can assert on them."""
    return [p.__enter__() for p in patches]


def _exit_all(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


def test_logout_yes_skips_both_prompts_session_cleared_creds_kept():
    """`--yes`: session goes, credentials stay (since `--purge-credentials`
    is not also passed). Neither confirm should fire."""
    patches = _patches(
        had_session=True,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[],  # neither prompt should be asked
    )
    mocks = _enter_all(patches)
    try:
        # Index matches _patches() ordering.
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout", "--yes"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        confirm_mock.assert_not_called()
        delete_creds_mock.assert_not_called()
        assert "Local Telegram session cleared." in result.output
        # Credentials were kept → user is reminded they can re-link.
        assert "kept" in result.output.lower() or "tg login" in result.output
    finally:
        _exit_all(patches)


def test_logout_yes_with_purge_credentials_deletes_both():
    """`--yes --purge-credentials`: session cleared, credentials deleted,
    no prompts asked."""
    patches = _patches(
        had_session=True,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout", "--yes", "--purge-credentials"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        confirm_mock.assert_not_called()
        delete_creds_mock.assert_called_once()
        assert "Telegram api_id / api_hash deleted." in result.output
    finally:
        _exit_all(patches)


def test_logout_session_confirm_no_aborts_early():
    """User declines the session-clear prompt → exit 0, nothing wiped."""
    patches = _patches(
        had_session=True,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[False],  # decline the session-clear prompt
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_not_called()
        delete_creds_mock.assert_not_called()
        assert "cancelled" in result.output.lower()
    finally:
        _exit_all(patches)


def test_logout_session_confirm_yes_credentials_confirm_no_keeps_creds():
    """Yes-then-no: session cleared, credentials kept."""
    patches = _patches(
        had_session=True,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[True, False],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        assert confirm_mock.call_count == 2
        delete_creds_mock.assert_not_called()
        assert "Local Telegram session cleared." in result.output
        assert "kept" in result.output.lower()
    finally:
        _exit_all(patches)


def test_logout_session_confirm_yes_credentials_confirm_yes_deletes_both():
    """Yes-then-yes: session cleared, credentials deleted."""
    patches = _patches(
        had_session=True,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[True, True],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        assert confirm_mock.call_count == 2
        delete_creds_mock.assert_called_once()
        assert "Telegram api_id / api_hash deleted." in result.output
    finally:
        _exit_all(patches)


def test_logout_no_session_no_credentials_skips_all_prompts():
    """Nothing to clear and nothing to purge → no prompts, friendly
    "no active session" message, exit 0."""
    patches = _patches(
        had_session=False,
        interactive=True,
        persisted_creds={},  # no creds persisted either
        confirm_results=[],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()  # safe to call even when nothing to wipe
        confirm_mock.assert_not_called()
        delete_creds_mock.assert_not_called()
        assert "No active session to clear." in result.output
    finally:
        _exit_all(patches)


def test_logout_no_session_but_credentials_persisted_only_asks_for_creds():
    """No session to clear → skip the session-clear prompt; still ask
    about persisting credentials when they exist (some users want to
    revoke creds even after the session is already gone)."""
    patches = _patches(
        had_session=False,
        interactive=True,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[True],  # yes to credentials purge
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        # Only one confirm — for credentials. The session-clear confirm
        # is skipped because there was nothing to clear.
        assert confirm_mock.call_count == 1
        delete_creds_mock.assert_called_once()
    finally:
        _exit_all(patches)


def test_logout_non_tty_skips_session_confirm_and_keeps_creds():
    """Non-TTY (CI / piped runs): preserve historical contract — session
    cleared without prompting, credentials kept (no `--purge-credentials`)."""
    patches = _patches(
        had_session=True,
        interactive=False,  # the key difference
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        confirm_mock.assert_not_called()
        delete_creds_mock.assert_not_called()
    finally:
        _exit_all(patches)


def test_logout_non_tty_with_purge_credentials_deletes_both():
    """Non-TTY + `--purge-credentials`: scripted full wipe path."""
    patches = _patches(
        had_session=True,
        interactive=False,
        persisted_creds={"telegram.api_id": "12345", "telegram.api_hash": "abc"},
        confirm_results=[],
    )
    mocks = _enter_all(patches)
    try:
        wipe_mock = mocks[2]
        confirm_mock = mocks[4]
        delete_creds_mock = mocks[6]

        result = runner.invoke(app, ["tg", "logout", "--purge-credentials"])
        assert result.exit_code == 0, result.output
        wipe_mock.assert_called_once()
        confirm_mock.assert_not_called()
        delete_creds_mock.assert_called_once()
        assert "Telegram api_id / api_hash deleted." in result.output
    finally:
        _exit_all(patches)
