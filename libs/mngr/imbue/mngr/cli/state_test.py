"""Integration tests for the `mngr state` CLI command.

These drive the real ``state`` click command end to end against a discoverable
local host: the cheap ``--quick`` poll, the host detail/agent-ref listing, the
``--quick``/``--fields`` conflict, and (under tmux) the rich agent detail and
template paths.
"""

import json
from collections.abc import Callable
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.testing import create_agent_data_json
from imbue.mngr.cli.state import state
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.providers.local.instance import LocalProviderInstance


def _last_json_line(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def test_state_quick_host_reports_running_state(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # The local host is always RUNNING; --quick should report it without a full fetch.
    create_agent_data_json(local_provider.host_dir, "agent-" + uuid4().hex)
    host_id = local_provider.host_id

    result = cli_runner.invoke(state, [str(host_id), "--quick", "--format=json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    payload = _last_json_line(result.output)
    assert payload["host_state"] == "RUNNING"
    # Host target -> no agent_state key.
    assert "agent_state" not in payload


def test_state_host_json_includes_agent_refs(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    create_agent_data_json(local_provider.host_dir, "stateful-host-agent")
    host_id = local_provider.host_id

    result = cli_runner.invoke(state, [str(host_id), "--format=json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    payload = _last_json_line(result.output)
    assert payload["host"]["state"] == "RUNNING"
    agent_names = {agent["name"] for agent in payload["agents"]}
    assert "stateful-host-agent" in agent_names


def test_state_quick_rejects_fields(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    # The conflict is rejected before any resolution, so a bogus target is fine.
    result = cli_runner.invoke(state, ["some-target", "--quick", "--fields", "state"], obj=plugin_manager)

    assert result.exit_code != 0
    assert "--fields" in result.output


@pytest.mark.tmux
def test_state_agent_json_returns_full_details(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    create_test_agent: Callable[[str, str], str],
) -> None:
    agent_name = "state-detail-" + uuid4().hex
    create_test_agent(agent_name, "sleep 847500")

    result = cli_runner.invoke(state, [agent_name, "--format=json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    payload = _last_json_line(result.output)
    assert payload["resource_type"] == "agent"
    assert payload["name"] == agent_name
    # The embedded host details mirror what `mngr list` produces.
    assert payload["host"]["name"] is not None


@pytest.mark.tmux
def test_state_agent_template_renders_fields(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    create_test_agent: Callable[[str, str], str],
) -> None:
    agent_name = "state-tmpl-" + uuid4().hex
    create_test_agent(agent_name, "sleep 847501")

    result = cli_runner.invoke(state, [agent_name, "--format", "{name}::{state}"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1].startswith(f"{agent_name}::")
