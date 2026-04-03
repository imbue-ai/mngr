"""Tests for the mngr extras command."""

from pathlib import Path

from click.testing import CliRunner

from imbue.mngr.cli.extras import _completion_status
from imbue.mngr.cli.extras import _detect_shell
from imbue.mngr.cli.extras import _generate_completion_script
from imbue.mngr.cli.extras import _get_shell_rc
from imbue.mngr.cli.extras import _is_completion_configured
from imbue.mngr.cli.extras import extras


def test_detect_shell_returns_zsh_or_bash() -> None:
    """_detect_shell returns a valid shell type."""
    shell = _detect_shell()
    assert shell in ("zsh", "bash")


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


def test_extras_no_args_shows_status(cli_runner: CliRunner) -> None:
    """Running 'mngr extras' with no flags shows status."""
    result = cli_runner.invoke(extras, [])
    assert result.exit_code == 0
    assert "Extras" in result.output
