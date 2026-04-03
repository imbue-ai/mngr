"""Tests for the mngr extras command."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.mngr.cli import extras as extras_mod
from imbue.mngr.cli.extras import _completion_status
from imbue.mngr.cli.extras import _detect_shell
from imbue.mngr.cli.extras import _generate_completion_script
from imbue.mngr.cli.extras import _get_shell_rc
from imbue.mngr.cli.extras import _install_claude_plugin
from imbue.mngr.cli.extras import _is_completion_configured
from imbue.mngr.cli.extras import _plugins_status
from imbue.mngr.cli.extras import extras


def test_detect_shell_returns_zsh_or_bash() -> None:
    """_detect_shell returns a valid shell type."""
    shell = _detect_shell()
    assert shell in ("zsh", "bash")


def test_detect_shell_returns_zsh_for_zsh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_shell returns 'zsh' when SHELL env is set to zsh."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _detect_shell() == "zsh"


def test_detect_shell_returns_bash_for_bash_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_shell returns 'bash' when SHELL env is set to bash."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _detect_shell() == "bash"


def test_get_shell_rc_zsh() -> None:
    """_get_shell_rc returns .zshrc for zsh."""
    rc_path = _get_shell_rc("zsh")
    assert rc_path.name == ".zshrc"


def test_get_shell_rc_bash() -> None:
    """_get_shell_rc returns .bashrc for bash."""
    rc_path = _get_shell_rc("bash")
    assert rc_path.name == ".bashrc"


def test_is_completion_configured_false_for_nonexistent_file(tmp_path: Path) -> None:
    """_is_completion_configured returns False for a file that doesn't exist."""
    assert _is_completion_configured(tmp_path / "nonexistent") is False


def test_is_completion_configured_false_for_empty_file(tmp_path: Path) -> None:
    """_is_completion_configured returns False when the RC file has no mngr completion."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# empty rc file\n")
    assert _is_completion_configured(rc) is False


def test_is_completion_configured_true_when_present(tmp_path: Path) -> None:
    """_is_completion_configured returns True when _mngr_complete is in the file."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# some config\n_mngr_complete() { ... }\n")
    assert _is_completion_configured(rc) is True


def test_generate_completion_script_zsh() -> None:
    """_generate_completion_script returns a non-empty string for zsh."""
    script = _generate_completion_script("zsh")
    assert isinstance(script, str)
    assert "_mngr_complete" in script


def test_generate_completion_script_bash() -> None:
    """_generate_completion_script returns a non-empty string for bash."""
    script = _generate_completion_script("bash")
    assert isinstance(script, str)
    assert "_mngr_complete" in script


def test_completion_status_returns_tuple() -> None:
    """_completion_status returns a 3-tuple."""
    result = _completion_status()
    assert len(result) == 3
    configured, shell_type, rc_path = result
    assert isinstance(configured, bool)
    assert shell_type in ("zsh", "bash")
    assert isinstance(rc_path, Path)


def test_install_claude_plugin_no_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """_install_claude_plugin returns False when claude is not available."""
    monkeypatch.setattr(extras_mod, "_claude_plugin_status", lambda: (False, False))
    assert _install_claude_plugin(auto=True) is False


def test_plugins_status_returns_string() -> None:
    """_plugins_status returns a string describing plugin status."""
    status = _plugins_status()
    assert isinstance(status, str)
    assert len(status) > 0


def test_extras_no_args_shows_status(cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running 'mngr extras' with no flags shows status."""
    monkeypatch.setattr(extras_mod, "_claude_plugin_status", lambda: (False, False))
    result = cli_runner.invoke(extras, [])
    assert result.exit_code == 0
    assert "Extras" in result.output


def test_extras_help(cli_runner: CliRunner) -> None:
    """The --help flag should work for the extras command."""
    result = cli_runner.invoke(extras, ["--help"])
    assert result.exit_code == 0


def test_extras_completion_subcommand(cli_runner: CliRunner) -> None:
    """The 'extras completion' subcommand should work."""
    result = cli_runner.invoke(extras, ["completion"])
    assert result.exit_code == 0


def test_extras_plugins_subcommand(cli_runner: CliRunner) -> None:
    """The 'extras plugins' subcommand should work."""
    result = cli_runner.invoke(extras, ["plugins"])
    assert result.exit_code in (0, 1)
