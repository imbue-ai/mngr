"""Unit tests for the provision CLI command."""

import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.provision import ProvisionCliOptions
from imbue.mng.cli.provision import _output_result
from imbue.mng.cli.provision import provision
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat

# =============================================================================
# Tests for ProvisionCliOptions
# =============================================================================


def test_provision_cli_options_can_be_instantiated() -> None:
    """Test that ProvisionCliOptions can be instantiated with all required fields."""
    opts = ProvisionCliOptions(
        agent="my-agent",
        agent_option=None,
        host=None,
        bootstrap=None,
        destroy_on_fail=False,
        restart=True,
        extra_provision_command=(),
        upload_file=(),
        append_to_file=(),
        prepend_to_file=(),
        create_directory=(),
        env=(),
        env_file=(),
        pass_env=(),
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
    assert opts.agent == "my-agent"
    assert opts.restart is True
    assert opts.extra_provision_command == ()


# =============================================================================
# Tests for _output_result
# =============================================================================


def test_output_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should output JSON data for JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result("my-agent", output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["agent"] == "my-agent"
    assert data["provisioned"] is True


def test_output_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should output JSONL event for JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result("my-agent", output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "provision_result"
    assert data["agent"] == "my-agent"
    assert data["provisioned"] is True


def test_output_result_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should produce no output for HUMAN format (logs go to stderr)."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result("my-agent", output_opts)
    captured = capsys.readouterr()
    # HUMAN format does not write anything to stdout for this function
    assert captured.out == ""


# =============================================================================
# Tests for provision CLI command
# =============================================================================


def test_provision_rejects_both_positional_and_option_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that specifying both positional and --agent option fails."""
    result = cli_runner.invoke(
        provision,
        ["my-agent", "--agent", "other-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Cannot specify both" in result.output
