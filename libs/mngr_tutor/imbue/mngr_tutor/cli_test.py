"""Unit tests for the tutor CLI command."""

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_tutor.cli import TutorCliOptions
from imbue.mngr_tutor.cli import tutor


def test_tutor_cli_options_can_be_instantiated() -> None:
    opts = TutorCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.output_format == "human"


def test_tutor_command_calls_lesson_selector(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tutor command should call run_lesson_selector."""
    monkeypatch.setattr(
        "imbue.mngr_tutor.cli.run_lesson_selector",
        lambda lessons: None,  # Return None to exit the selector loop
    )
    result = cli_runner.invoke(tutor, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
