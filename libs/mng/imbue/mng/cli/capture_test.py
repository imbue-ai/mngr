import pluggy
from click.testing import CliRunner

from imbue.mng.cli.capture import CaptureCliOptions
from imbue.mng.cli.capture import capture


def test_capture_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(capture, ["--help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "Capture" in result.output or "capture" in result.output


def test_capture_nonexistent_agent_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(capture, ["nonexistent-agent-55123"], obj=plugin_manager, catch_exceptions=True)
    assert result.exit_code != 0


def test_capture_cli_options_accepts_full_flag() -> None:
    opts = CaptureCliOptions(
        agent="test-agent",
        start=True,
        full=False,
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
    assert opts.full is False

    opts_full = CaptureCliOptions(
        agent="test-agent",
        start=True,
        full=True,
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
    assert opts_full.full is True
