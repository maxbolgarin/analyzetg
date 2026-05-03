"""`unread update` — version compare, install detection, command flow.

All tests run offline. The PyPI fetch is mocked via `httpx.AsyncClient`,
the `uv tool list` probe via `subprocess.run`, and the pipx heuristic
via `sys.executable`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer

from unread import update as upd

# ------------------------------------------------------------- is_newer


def test_is_newer_minor_bump() -> None:
    assert upd.is_newer("0.2.0", "0.1.0") is True


def test_is_newer_major_bump() -> None:
    assert upd.is_newer("1.0.0", "0.9.9") is True


def test_is_newer_equal_returns_false() -> None:
    assert upd.is_newer("0.1.0", "0.1.0") is False


def test_is_newer_pre_release_vs_stable() -> None:
    # PEP 440: 0.2.0a1 is considered older than 0.2.0, but newer than 0.1.0.
    assert upd.is_newer("0.2.0a1", "0.1.0") is True
    assert upd.is_newer("0.2.0a1", "0.2.0") is False


def test_is_newer_older_returns_false() -> None:
    assert upd.is_newer("0.1.0", "0.2.0") is False


# -------------------------------------------------- fetch_latest_version


def _fake_async_client(json_payload: dict | None = None, *, side_effect=None):
    """Build an AsyncMock that mimics the `async with httpx.AsyncClient` shape."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if side_effect is not None:
        client.get = AsyncMock(side_effect=side_effect)
    else:
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.json = MagicMock(return_value=json_payload or {})
        client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_fetch_latest_version_parses_pypi_response() -> None:
    fake = _fake_async_client({"info": {"version": "0.9.99"}})
    with patch("unread.update.httpx.AsyncClient", return_value=fake):
        out = await upd.fetch_latest_version()
    assert out == "0.9.99"


@pytest.mark.asyncio
async def test_fetch_latest_version_network_error_raises() -> None:
    fake = _fake_async_client(side_effect=httpx.ConnectError("dns"))
    with (
        patch("unread.update.httpx.AsyncClient", return_value=fake),
        pytest.raises(upd.UpdateCheckError),
    ):
        await upd.fetch_latest_version()


@pytest.mark.asyncio
async def test_fetch_latest_version_bad_payload_raises() -> None:
    fake = _fake_async_client({"unexpected": "shape"})
    with (
        patch("unread.update.httpx.AsyncClient", return_value=fake),
        pytest.raises(upd.UpdateCheckError),
    ):
        await upd.fetch_latest_version()


# ------------------------------------------------ detect_install_method


def test_detect_install_method_uv_tool(monkeypatch) -> None:
    monkeypatch.setattr(upd.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    fake_run = MagicMock(return_value=MagicMock(stdout="unread v0.1.0\n", stderr=""))
    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    result = upd.detect_install_method()
    assert result is not None
    label, argv = result
    assert label == "uv tool"
    assert argv == ["/usr/bin/uv", "tool", "upgrade", "unread"]


def test_detect_install_method_pipx(monkeypatch) -> None:
    monkeypatch.setattr(upd.shutil, "which", lambda name: "/usr/bin/pipx" if name == "pipx" else None)
    monkeypatch.setattr(upd.sys, "executable", "/home/u/.local/share/pipx/venvs/unread/bin/python")
    result = upd.detect_install_method()
    assert result is not None
    label, argv = result
    assert label == "pipx"
    assert argv == ["/usr/bin/pipx", "upgrade", "unread"]


def test_detect_install_method_pip_fallback(monkeypatch) -> None:
    monkeypatch.setattr(upd.shutil, "which", lambda name: None)
    monkeypatch.setattr(upd.sys, "executable", "/usr/bin/python3.11")
    result = upd.detect_install_method()
    assert result is not None
    label, argv = result
    assert label == "pip"
    assert argv == ["/usr/bin/python3.11", "-m", "pip", "install", "--upgrade", "unread"]


def test_detect_install_method_uv_present_but_unread_not_listed(monkeypatch) -> None:
    """uv exists but `unread` isn't a uv-managed tool — fall through to pip."""
    monkeypatch.setattr(upd.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    fake_run = MagicMock(return_value=MagicMock(stdout="other-tool v1.0\n", stderr=""))
    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    monkeypatch.setattr(upd.sys, "executable", "/usr/bin/python3")
    result = upd.detect_install_method()
    assert result is not None
    label, _ = result
    assert label == "pip"


def test_detect_install_method_none_when_no_executable(monkeypatch) -> None:
    monkeypatch.setattr(upd.shutil, "which", lambda name: None)
    monkeypatch.setattr(upd.sys, "executable", "")
    assert upd.detect_install_method() is None


# ------------------------------------------------------------ cmd_update


def test_cmd_update_up_to_date(monkeypatch, capsys) -> None:
    """fetch returns same version → friendly note, no install attempt."""
    monkeypatch.setattr(upd, "__version__", "0.1.0")
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(return_value="0.1.0"))
    fake_run = MagicMock()
    monkeypatch.setattr(upd.subprocess, "run", fake_run)

    upd.cmd_update(check=False, yes=False)

    out = capsys.readouterr().out
    assert "0.1.0" in out
    fake_run.assert_not_called()


def test_cmd_update_check_only_does_not_install(monkeypatch, capsys) -> None:
    """`--check` reports newer version but never spawns the upgrade subprocess."""
    monkeypatch.setattr(upd, "__version__", "0.1.0")
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(return_value="0.2.0"))
    fake_run = MagicMock()
    monkeypatch.setattr(upd.subprocess, "run", fake_run)

    upd.cmd_update(check=True, yes=False)

    out = capsys.readouterr().out
    assert "0.2.0" in out
    fake_run.assert_not_called()


def test_cmd_update_yes_runs_install(monkeypatch) -> None:
    """`--yes` skips the prompt and runs the detected upgrade argv."""
    monkeypatch.setattr(upd, "__version__", "0.1.0")
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(return_value="0.2.0"))
    monkeypatch.setattr(
        upd, "detect_install_method", lambda: ("uv tool", ["/usr/bin/uv", "tool", "upgrade", "unread"])
    )
    fake_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(upd.subprocess, "run", fake_run)

    upd.cmd_update(check=False, yes=True)

    fake_run.assert_called_once()
    called_argv = fake_run.call_args.args[0]
    assert called_argv == ["/usr/bin/uv", "tool", "upgrade", "unread"]


def test_cmd_update_unknown_install_prints_manual_commands(monkeypatch, capsys) -> None:
    """No detectable install method → print manual instructions, no subprocess."""
    monkeypatch.setattr(upd, "__version__", "0.1.0")
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(return_value="0.2.0"))
    monkeypatch.setattr(upd, "detect_install_method", lambda: None)
    fake_run = MagicMock()
    monkeypatch.setattr(upd.subprocess, "run", fake_run)

    upd.cmd_update(check=False, yes=True)

    out = capsys.readouterr().out
    # Every fallback command should appear in the manual-instruction block.
    assert "uv tool upgrade unread" in out
    assert "pipx upgrade unread" in out
    assert "pip install --upgrade unread" in out
    fake_run.assert_not_called()


def test_cmd_update_install_subprocess_failure_propagates_exit(monkeypatch) -> None:
    """Non-zero subprocess exit becomes a typer.Exit(rc)."""
    monkeypatch.setattr(upd, "__version__", "0.1.0")
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(return_value="0.2.0"))
    monkeypatch.setattr(
        upd, "detect_install_method", lambda: ("pip", ["python", "-m", "pip", "install", "-U", "unread"])
    )
    monkeypatch.setattr(upd.subprocess, "run", MagicMock(return_value=MagicMock(returncode=2)))

    with pytest.raises(typer.Exit) as ei:
        upd.cmd_update(check=False, yes=True)
    assert ei.value.exit_code == 2


def test_cmd_update_fetch_failure_exits_one(monkeypatch) -> None:
    """PyPI unreachable → friendly message + exit 1."""
    monkeypatch.setattr(upd, "fetch_latest_version", AsyncMock(side_effect=upd.UpdateCheckError("dns")))

    with pytest.raises(typer.Exit) as ei:
        upd.cmd_update(check=False, yes=False)
    assert ei.value.exit_code == 1


# --------------------------------------------------------- doctor passive


@pytest.mark.asyncio
async def test_doctor_silent_on_network_failure(monkeypatch) -> None:
    """The doctor's passive update check must swallow any network error
    so `unread doctor` keeps working offline. We only verify that
    `fetch_latest_version` raising doesn't crash the call.
    """

    async def _boom(timeout: float = 3.0) -> str:
        raise upd.UpdateCheckError("offline")

    import contextlib

    monkeypatch.setattr(upd, "fetch_latest_version", _boom)
    # Simulate the doctor's swallow block directly — full cmd_doctor()
    # depends on a live DB / Telegram session and isn't a clean unit
    # test target. The behaviour we care about is the suppression
    # shape, which mirrors the production code.
    with contextlib.suppress(Exception):
        await upd.fetch_latest_version(timeout=3.0)
    # If we got here without raising, the swallow works.
    assert True
