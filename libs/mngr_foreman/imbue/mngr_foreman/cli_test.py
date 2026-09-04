"""Tests for ``mngr foreman`` CLI option wiring (backgrounding flags)."""

from __future__ import annotations

import click

from imbue.mngr_foreman.cli import _DEFAULT_LOG_FILE
from imbue.mngr_foreman.cli import _DEFAULT_PID_FILE
from imbue.mngr_foreman.cli import foreman


def _param(name: str) -> click.Parameter:
    for param in foreman.params:
        if param.name == name:
            return param
    raise AssertionError(f"no --{name} option on `mngr foreman`")


def test_background_is_a_short_d_flag_default_false() -> None:
    param = _param("background")
    assert isinstance(param, click.Option)
    assert param.is_flag
    assert "-d" in param.opts
    assert "--background" in param.opts
    assert param.default is False


def test_log_and_pid_file_defaults_under_dot_mngr() -> None:
    assert _param("foreman_log_file").default == _DEFAULT_LOG_FILE
    assert _param("pid_file").default == _DEFAULT_PID_FILE
    assert _DEFAULT_LOG_FILE.name == "foreman.log"
    assert _DEFAULT_PID_FILE.name == "foreman.pid"
    assert _DEFAULT_LOG_FILE.parent.name == ".mngr"


def test_foreman_is_a_group_with_install_and_uninstall() -> None:
    # Bare `mngr foreman` still runs the server (invoke_without_command); the group
    # adds the systemd subcommands.
    assert isinstance(foreman, click.Group)
    assert foreman.invoke_without_command is True
    assert set(foreman.commands) >= {"install", "uninstall"}
