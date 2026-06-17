"""Unit tests for mngr_opencode_usage.plugin (provisioning helper + hookimpl filter)."""

from __future__ import annotations

from pathlib import Path

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode_usage.plugin import _USAGE_PLUGIN_FILENAME
from imbue.mngr_opencode_usage.plugin import _provision_usage_writer_plugin
from imbue.mngr_opencode_usage.plugin import on_after_provisioning


def _expected_writer_path(agent_state_dir: Path) -> Path:
    return get_opencode_plugin_path(get_opencode_config_dir(agent_state_dir)).parent / _USAGE_PLUGIN_FILENAME


def test_provision_installs_writer_plugin_in_the_opencode_plugin_dir(local_host: Host) -> None:
    state_dir = local_host.host_dir / "agents" / "agent-opencode-usage"
    _provision_usage_writer_plugin(local_host, state_dir)
    written = _expected_writer_path(state_dir)
    assert written.is_file()
    assert "MngrUsagePlugin" in written.read_text()


def test_provision_is_idempotent(local_host: Host) -> None:
    state_dir = local_host.host_dir / "agents" / "agent-opencode-usage-2"
    _provision_usage_writer_plugin(local_host, state_dir)
    _provision_usage_writer_plugin(local_host, state_dir)
    assert _expected_writer_path(state_dir).is_file()


def test_hookimpl_skips_non_opencode_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    # A real non-OpenCode agent (plain BaseAgent) must be a no-op: the
    # isinstance(OpenCodeAgent) guard short-circuits before any file is written.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = create_test_agent(
        local_provider,
        work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )
    on_after_provisioning(agent=agent, host=agent.host, mngr_ctx=temp_mngr_ctx)
    state_dir = get_agent_state_dir_path(agent.host.host_dir, agent.id)
    assert not _expected_writer_path(state_dir).exists()


def test_hookimpl_provisions_opencode_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = create_test_agent(
        local_provider,
        work_dir,
        agent_config=OpenCodeAgentConfig(),
        agent_type=None,
        extra_data=None,
        agent_class=OpenCodeAgent,
    )
    on_after_provisioning(agent=agent, host=agent.host, mngr_ctx=temp_mngr_ctx)
    state_dir = get_agent_state_dir_path(agent.host.host_dir, agent.id)
    assert _expected_writer_path(state_dir).is_file()
