"""Integration tests for the wait CLI command.

These drive the real ``wait`` click command end to end against a discoverable
local host, exercising target resolution, the positional/``--state`` argument
combining and default-state fallback, the already-matched fast path, the
timeout path, and the exit-code contract documented in README.md.
"""

import json
from uuid import uuid4

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.exit_codes import EXIT_CODE_TIMEOUT
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_wait.cli import wait
from imbue.mngr_wait.plugin import register_cli_commands
from imbue.mngr_wait.testing import create_agent_data_json


def test_wait_command_already_matched_local_host_running_exits_success(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # The local host is always RUNNING, so waiting for RUNNING matches immediately.
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(
        wait,
        [str(host_id), "RUNNING", "--format=json", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code == EXIT_CODE_SUCCESS, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["is_matched"] is True
    assert payload["matched_state"] == "RUNNING"
    assert payload["target_type"] == "HOST"
    assert payload["final_host_state"] == "RUNNING"


def test_wait_command_times_out_when_state_never_reached_exits_with_timeout_code(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # The local host never stops, so waiting for STOPPED must time out (exit 2),
    # which verifies the EXIT_CODE_TIMEOUT mapping (distinct from the error code).
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(
        wait,
        [str(host_id), "STOPPED", "--timeout", "1s", "--interval", "1s", "--format=json", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code == EXIT_CODE_TIMEOUT, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["is_matched"] is False
    assert payload["is_timed_out"] is True


def test_wait_command_unparseable_target_is_a_bad_parameter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    # A target that cannot be parsed as an agent or host address is surfaced as
    # a click BadParameter (non-zero exit), not an internal error.
    result = cli_runner.invoke(
        wait,
        ["bad/name", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code != EXIT_CODE_SUCCESS
    assert "Invalid value" in result.output or "bad/name" in result.output


def test_wait_command_nonexistent_agent_target_exits_nonzero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    # A well-formed but nonexistent agent name resolves to a "Could not find
    # agent" error and exits non-zero.
    result = cli_runner.invoke(
        wait,
        ["nonexistent-agent-" + uuid4().hex, "DONE", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code != EXIT_CODE_SUCCESS
    assert "Could not find agent" in result.output


def test_wait_command_combines_positional_and_state_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # Positional states and repeatable --state options are unioned; RUNNING is
    # supplied via --state and matches the always-running local host.
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(
        wait,
        [str(host_id), "STOPPED", "--state", "RUNNING", "--format=json", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code == EXIT_CODE_SUCCESS, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["matched_state"] == "RUNNING"


def test_wait_command_rejects_invalid_state_string(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # An unknown state string is rejected with a clear error before polling.
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(
        wait,
        [str(host_id), "NOPESTATE", "--daemonize"],
        obj=plugin_manager,
    )

    assert result.exit_code != EXIT_CODE_SUCCESS
    assert "Invalid state: 'NOPESTATE'" in result.output


def test_wait_command_reads_target_from_stdin_when_omitted(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # With no positional TARGET, the identifier is read from stdin. States are
    # supplied via --state so the lone positional slot stays empty.
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(
        wait,
        ["--state", "RUNNING", "--format=json", "--daemonize"],
        obj=plugin_manager,
        input=f"{host_id}\n",
    )

    assert result.exit_code == EXIT_CODE_SUCCESS, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["matched_state"] == "RUNNING"
    assert payload["target"] == str(host_id)


def test_wait_command_is_registered_through_the_plugin_manager(
    plugin_manager: pluggy.PluginManager,
) -> None:
    # The autouse plugin_manager loads setuptools entry points, so this verifies
    # the wait command is actually discovered and wired into mngr's CLI via the
    # register_cli_commands hook (not just returned by the hookimpl directly).
    command_lists = plugin_manager.hook.register_cli_commands()
    discovered_names = {
        command.name for command_list in command_lists if command_list is not None for command in command_list
    }
    assert "wait" in discovered_names


def test_register_cli_commands_hookimpl_returns_only_wait_command() -> None:
    # Smoke test of the hookimpl in isolation (does not exercise plugin-manager
    # discovery -- that is covered by the test above).
    commands = register_cli_commands()
    assert commands is not None
    assert [command.name for command in commands] == ["wait"]
