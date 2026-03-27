from pathlib import Path

import pytest

from imbue.mngr.api.provision import _read_existing_env_content
from imbue.mngr.api.provision import provision_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentProvisioningOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.env_utils import parse_env_file

# =============================================================================
# _read_existing_env_content Tests
# =============================================================================


@pytest.mark.tmux
def test_read_existing_env_content_returns_none_when_no_env_file(
    temp_work_dir: Path,
    local_host: Host,
) -> None:
    """_read_existing_env_content should return None when no env file exists."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-no-env"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847320"),
        ),
    )

    result = _read_existing_env_content(local_host, agent)

    local_host.destroy_agent(agent)

    assert result is None


@pytest.mark.tmux
def test_read_existing_env_content_reads_existing_env_file(
    temp_work_dir: Path,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """_read_existing_env_content should return the env file content when it exists."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-with-env"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847321"),
        ),
    )

    # Write an env file manually
    env_path = local_host.get_agent_env_path(agent)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("FOO=bar\nBAZ=qux\n")

    result = _read_existing_env_content(local_host, agent)

    local_host.destroy_agent(agent)

    assert result == "FOO=bar\nBAZ=qux\n"


# =============================================================================
# provision_agent Tests
# =============================================================================


@pytest.mark.tmux
def test_provision_agent_with_no_restart_on_stopped_agent(
    temp_work_dir: Path,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision_agent should provision a stopped agent without attempting restart."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-stopped"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847322"),
        ),
    )

    # Agent is stopped by default (not started)
    provisioning = AgentProvisioningOptions()
    environment = AgentEnvironmentOptions()

    # This should not raise and should not try to stop/start the agent
    provision_agent(
        agent=agent,
        host=local_host,
        provisioning=provisioning,
        environment=environment,
        mngr_ctx=temp_mngr_ctx,
        is_restart=False,
    )

    local_host.destroy_agent(agent)


@pytest.mark.tmux
def test_provision_agent_with_env_vars(
    temp_work_dir: Path,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """provision_agent should apply environment variables from env files."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-env"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847323"),
        ),
    )

    # Create an env file
    env_file = tmp_path / "provision.env"
    env_file.write_text("PROV_VAR=provision_value\n")

    provisioning = AgentProvisioningOptions()
    environment = AgentEnvironmentOptions(
        env_files=(env_file,),
    )

    provision_agent(
        agent=agent,
        host=local_host,
        provisioning=provisioning,
        environment=environment,
        mngr_ctx=temp_mngr_ctx,
        is_restart=False,
    )

    # Verify the env vars were written to the agent's env file
    env_path = local_host.get_agent_env_path(agent)
    assert env_path.exists(), "Env file should exist after provisioning with env_files"
    env_vars = parse_env_file(env_path.read_text())
    assert env_vars["PROV_VAR"] == "provision_value"

    local_host.destroy_agent(agent)


@pytest.mark.tmux
def test_provision_agent_merges_existing_env_content(
    temp_work_dir: Path,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision_agent should merge existing env content with new env options."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-merge"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847324"),
        ),
    )

    # Write existing env content
    env_path = local_host.get_agent_env_path(agent)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("EXISTING_VAR=existing_value\n")

    provisioning = AgentProvisioningOptions()
    environment = AgentEnvironmentOptions()

    provision_agent(
        agent=agent,
        host=local_host,
        provisioning=provisioning,
        environment=environment,
        mngr_ctx=temp_mngr_ctx,
        is_restart=False,
    )

    # Verify the existing env content is preserved after provisioning
    env_content = env_path.read_text()
    env_vars = parse_env_file(env_content)
    assert env_vars["EXISTING_VAR"] == "existing_value"

    local_host.destroy_agent(agent)


@pytest.mark.tmux
def test_provision_agent_restarts_running_agent(
    temp_work_dir: Path,
    local_host: Host,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision_agent should stop and restart a running agent when is_restart=True."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("provision-restart"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847325"),
        ),
    )

    # Start the agent
    local_host.start_agents([agent.id])

    provisioning = AgentProvisioningOptions()
    environment = AgentEnvironmentOptions()

    provision_agent(
        agent=agent,
        host=local_host,
        provisioning=provisioning,
        environment=environment,
        mngr_ctx=temp_mngr_ctx,
        is_restart=True,
    )

    # Agent should be running again after provision
    assert agent.is_running()

    local_host.destroy_agent(agent)
