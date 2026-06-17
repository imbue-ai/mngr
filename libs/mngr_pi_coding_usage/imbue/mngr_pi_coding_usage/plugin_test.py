"""Unit tests for mngr_pi_coding_usage.plugin (gate provisioning + hookimpl filter)."""

from __future__ import annotations

from pathlib import Path

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.primitives import AgentId
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_pi_coding_usage.plugin import _USAGE_GATE_FILENAME
from imbue.mngr_pi_coding_usage.plugin import on_after_provisioning


def _gate_path(host_dir: Path, agent_id: AgentId) -> Path:
    return get_agent_state_dir_path(host_dir, agent_id) / _USAGE_GATE_FILENAME


def test_provisions_usage_gate_for_pi_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = create_test_agent(
        local_provider,
        work_dir,
        agent_config=PiCodingAgentConfig(),
        agent_type=None,
        extra_data=None,
        agent_class=PiCodingAgent,
    )
    on_after_provisioning(agent=agent, host=agent.host, mngr_ctx=temp_mngr_ctx)
    gate = _gate_path(agent.host.host_dir, agent.id)
    assert gate.read_text() == "1"


def test_skips_non_pi_agent(local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
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
    assert not _gate_path(agent.host.host_dir, agent.id).exists()
