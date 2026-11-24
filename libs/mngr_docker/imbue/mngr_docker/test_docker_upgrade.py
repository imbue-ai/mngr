from pathlib import Path

import pytest

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.api.testing import UPGRADABLE_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.testing import register_upgradable_command_agent_types
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import upgrade_agents
from imbue.mngr.cli.upgrade import format_upgrade_outcome
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_docker.instance import DockerProviderInstance

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]


@pytest.mark.docker
@pytest.mark.docker_sdk
@pytest.mark.rsync
def test_upgrade_rewrites_and_restarts_an_agent_on_a_docker_host_over_ssh_and_skips_it_once_the_host_stops(
    docker_provider: DockerProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    register_upgradable_command_agent_types()
    # Found by name the way a configured provider is, without loading the docker backend the tests leave out.
    _instance_cache[(docker_provider.name, id(temp_mngr_ctx))] = docker_provider
    host = docker_provider.create_host(HostName("upgrade-remote"))
    assert isinstance(host, OnlineHostInterface)
    work_dir = Path("/root/upgrade-work-59124")
    host.execute_idempotent_command(f"mkdir -p {work_dir}")
    agent = host.create_agent_state(
        work_dir,
        CreateAgentOptions(
            name=AgentName("upgrade-remote-59124"),
            agent_type=AgentTypeName(UPGRADABLE_COMMAND_AGENT_TYPE),
            command=CommandString("sleep 959124"),
        ),
    )
    host.start_agents([agent.id])
    wait_for(
        lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
        timeout=60.0,
        error_message="the agent on the docker host did not come up",
    )
    match = AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=host.id,
        host_name=host.get_name(),
        provider_name=docker_provider.name,
    )
    agent_label = f"{agent.name}@{host.get_name()}.{docker_provider.name}"

    def upgrade_once() -> AgentUpgradeOutcome:
        (outcome,) = upgrade_agents(
            [match],
            is_forced=False,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
            caller=None,
            mngr_ctx=temp_mngr_ctx,
        )
        return outcome

    outcome = upgrade_once()
    second_outcome = upgrade_once()

    assert isinstance(outcome, AgentUpgraded)
    assert (outcome.plan, outcome.prior_state) == (UpgradePlan.RESTART_IDLE, AgentLifecycleState.WAITING)
    assert format_upgrade_outcome(outcome) == f"upgraded {agent_label} (restarted)"
    assert isinstance(second_outcome, AgentCurrent)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    assert host.path_exists(state_dir / "commands" / UPGRADED_SCRIPT_NAME)
    assert all(host.path_exists(host.host_dir / "commands" / name) for name in SHARED_SHELL_LIB_NAMES)
    assert not host.path_exists(state_dir / UPGRADE_STAGING_DIR_NAME)
    assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING

    docker_provider.stop_host(host, create_snapshot=False)
    offline_outcome = upgrade_once()

    assert format_upgrade_outcome(offline_outcome) == (
        f"skipped {agent_label}: its host is stopped (`mngr start {agent_label}` starts it)"
    )
