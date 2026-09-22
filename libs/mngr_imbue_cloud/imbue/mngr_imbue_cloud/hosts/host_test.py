import json
from pathlib import Path

import pytest

from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_imbue_cloud.errors import FixedAgentIdError
from imbue.mngr_imbue_cloud.hosts.host import ImbueCloudHost


def _create_options(agent_id: AgentId | None) -> CreateAgentOptions:
    return CreateAgentOptions(
        agent_id=agent_id,
        name=AgentName("system-services"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )


# this tests: IF a leased host carries the lease's agent id but no baked agent state is on disk
# (the slow path's rebuilt container) THEN: the agent is created at the lease's id, not a fresh one
def test_create_agent_state_without_baked_state_creates_the_agent_at_the_lease_id(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = make_local_host_of_class(local_provider, ImbueCloudHost, pre_baked_agent_id=lease_agent_id)

    agent = host.create_agent_state(temp_work_dir, _create_options(agent_id=None))

    assert agent.id == lease_agent_id
    assert (local_provider.host_dir / "agents" / str(lease_agent_id) / "data.json").is_file()


def test_create_agent_state_refuses_an_agent_id_that_differs_from_the_lease(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    requested_agent_id = AgentId.generate()
    host = make_local_host_of_class(local_provider, ImbueCloudHost, pre_baked_agent_id=lease_agent_id)

    with pytest.raises(FixedAgentIdError, match=str(lease_agent_id)):
        host.create_agent_state(temp_work_dir, _create_options(agent_id=requested_agent_id))

    for agent_id in (lease_agent_id, requested_agent_id):
        assert not (local_provider.host_dir / "agents" / str(agent_id)).exists()


def _agent_shared_lib_path(local_provider: LocalProviderInstance, agent_id: AgentId) -> Path:
    """The agent-level ``mngr_log.sh``: written by mngr's full provisioning, never by the adopt path."""
    return local_provider.host_dir / "agents" / str(agent_id) / "commands" / "mngr_log.sh"


# this tests: IF the agent was created afresh at the lease's id (no baked state was on disk)
# THEN: provision_agent runs mngr's full provisioning, even though the fresh data.json now sits at the pre-baked id
def test_provision_agent_runs_the_full_provisioning_after_a_create_without_baked_state(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = make_local_host_of_class(local_provider, ImbueCloudHost, pre_baked_agent_id=lease_agent_id)
    options = _create_options(agent_id=None)
    agent = host.create_agent_state(temp_work_dir, options)

    host.provision_agent(agent, options, host.mngr_ctx)

    assert _agent_shared_lib_path(local_provider, lease_agent_id).is_file()


# this tests: IF the bake's agent state is on disk (the fast path adopted it)
# THEN: provision_agent takes the minimal path and skips mngr's full provisioning
def test_provision_agent_skips_the_full_provisioning_when_the_baked_state_was_adopted(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = make_local_host_of_class(local_provider, ImbueCloudHost, pre_baked_agent_id=lease_agent_id)
    baked_data_path = local_provider.host_dir / "agents" / str(lease_agent_id) / "data.json"
    baked_data_path.parent.mkdir(parents=True)
    baked_data_path.write_text(
        json.dumps(
            {
                "id": str(lease_agent_id),
                "name": "system-services",
                "type": "generic",
                "command": "sleep 1",
                "work_dir": str(temp_work_dir),
                "create_time": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    options = _create_options(agent_id=None)
    agent = host.create_agent_state(temp_work_dir, options)

    host.provision_agent(agent, options, host.mngr_ctx)

    assert agent.id == lease_agent_id
    assert not _agent_shared_lib_path(local_provider, lease_agent_id).exists()
