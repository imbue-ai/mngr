import json
from pathlib import Path
from uuid import uuid4

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.rename import _output
from imbue.mngr.cli.rename import _output_result
from imbue.mngr.cli.rename import rename
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_rename_dry_run_flag_parses_and_short_circuits(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The --dry-run flag must parse through Click and gate the rename to preview-only.

    This drives the real CLI parser (rather than constructing RenameCliOptions
    by hand): if Click failed to wire --dry-run to opts.dry_run, the command
    would perform the rename instead of previewing it, and the agent would no
    longer be found under its original name.
    """
    agent_name = f"test-rename-dryparse-{uuid4().hex}"
    new_name = f"test-renamed-dryparse-{uuid4().hex}"

    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName(agent_name),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 938271"),
        ),
    )

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name, "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert f"Would rename agent: {agent_name} -> {new_name}" in result.output
    # The dry-run must not have mutated the agent: it is still under its old name.
    agent_names = [str(a.name) for a in host.get_agents()]
    assert agent_name in agent_names
    assert new_name not in agent_names


def _make_output_opts(fmt: OutputFormat = OutputFormat.HUMAN) -> OutputOptions:
    return OutputOptions(output_format=fmt, format_template=None)


def test_rename_output_writes_to_stdout_in_human_format(capsys) -> None:
    """_output should write the message to stdout in HUMAN format."""
    _output("Agent already named: my-agent", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "Agent already named: my-agent" in captured.out


def test_rename_output_is_silent_in_json_format(capsys) -> None:
    """_output should produce no stdout in JSON format (JSON uses _output_result)."""
    _output("some message", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_rename_output_result_human(capsys) -> None:
    """_output_result with HUMAN should show rename message."""
    _output_result("old", "new", "agent-id", _make_output_opts(OutputFormat.HUMAN))
    captured = capsys.readouterr()
    assert "old" in captured.out
    assert "new" in captured.out


def test_rename_output_result_json(capsys) -> None:
    """_output_result with JSON should emit JSON."""
    _output_result("old", "new", "agent-id", _make_output_opts(OutputFormat.JSON))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["old_name"] == "old"
    assert output["new_name"] == "new"


def test_rename_output_result_jsonl(capsys) -> None:
    """_output_result with JSONL should emit an event containing the rename fields."""
    _output_result("alpha", "beta", "agent-xyz", _make_output_opts(OutputFormat.JSONL))
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "rename_result"
    assert output["old_name"] == "alpha"
    assert output["new_name"] == "beta"
    assert output["agent_id"] == "agent-xyz"


def test_rename_requires_two_arguments(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that rename requires both current and new name arguments."""
    result = cli_runner.invoke(
        rename,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    # A missing required positional is a Click usage error (exit code 2). With
    # no args, the first missing argument reported is CURRENT.
    assert result.exit_code == 2
    assert "Missing argument 'CURRENT'" in result.output
