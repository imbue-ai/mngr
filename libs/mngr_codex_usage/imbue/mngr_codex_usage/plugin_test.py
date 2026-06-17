"""Unit tests for mngr_codex_usage.plugin (writer install + hookimpl filter)."""

from __future__ import annotations

import os
from pathlib import Path

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.primitives import AgentId
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig
from imbue.mngr_codex_usage.plugin import _USAGE_EMIT_SCRIPT
from imbue.mngr_codex_usage.plugin import _USAGE_WRITER_SCRIPT
from imbue.mngr_codex_usage.plugin import on_after_provisioning


def _commands_dir(host_dir: Path, agent_id: AgentId) -> Path:
    return get_agent_state_dir_path(host_dir, agent_id) / "commands"


def _writer_path(host_dir: Path, agent_id: AgentId) -> Path:
    return _commands_dir(host_dir, agent_id) / _USAGE_WRITER_SCRIPT


def test_installs_executable_writer_for_codex_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = create_test_agent(
        local_provider,
        work_dir,
        agent_config=CodexAgentConfig(),
        agent_type=None,
        extra_data=None,
        agent_class=CodexAgent,
    )
    on_after_provisioning(agent=agent, host=agent.host, mngr_ctx=temp_mngr_ctx)
    writer = _writer_path(agent.host.host_dir, agent.id)
    assert writer.is_file()
    assert os.access(writer, os.X_OK), "writer script must be executable for the supervisor's -x check"
    assert "codex/usage" in writer.read_text()
    # The writer invokes the emitter via python3 <dir>/codex_usage_emit.py, so it
    # must be installed alongside the writer in the same commands/ dir.
    emitter = _commands_dir(agent.host.host_dir, agent.id) / _USAGE_EMIT_SCRIPT
    assert emitter.is_file()
    assert _USAGE_EMIT_SCRIPT in writer.read_text()


def test_skips_non_codex_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
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
    assert not _writer_path(agent.host.host_dir, agent.id).exists()
