import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import PrivateAttr

from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr_imbue_cloud.errors import FixedAgentIdError
from imbue.mngr_imbue_cloud.hosts.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.hosts.mock_published_endpoints_test import MockLeasePublishedEndpoints


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
    host = make_local_host_of_class(
        local_provider,
        ImbueCloudHost,
        pre_baked_agent_id=lease_agent_id,
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )

    agent = host.create_agent_state(temp_work_dir, _create_options(agent_id=None))

    assert agent.id == lease_agent_id
    assert (local_provider.host_dir / "agents" / str(lease_agent_id) / "data.json").is_file()


def test_create_agent_state_refuses_an_agent_id_that_differs_from_the_lease(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    requested_agent_id = AgentId.generate()
    host = make_local_host_of_class(
        local_provider,
        ImbueCloudHost,
        pre_baked_agent_id=lease_agent_id,
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )

    with pytest.raises(FixedAgentIdError, match=str(lease_agent_id)):
        host.create_agent_state(temp_work_dir, _create_options(agent_id=requested_agent_id))

    for agent_id in (lease_agent_id, requested_agent_id):
        assert not (local_provider.host_dir / "agents" / str(agent_id)).exists()


def _agent_shared_lib_path(local_provider: LocalProviderInstance, agent_id: AgentId) -> Path:
    """The agent-level ``mngr_log.sh``: written by mngr's full provisioning, never by the adopt path."""
    return local_provider.host_dir / "agents" / str(agent_id) / "commands" / "mngr_log.sh"


def _baked_agent_data_path(local_provider: LocalProviderInstance, lease_agent_id: AgentId) -> Path:
    return local_provider.host_dir / "agents" / str(lease_agent_id) / "data.json"


def _write_baked_agent_data(
    local_provider: LocalProviderInstance, lease_agent_id: AgentId, data: Mapping[str, Any]
) -> Path:
    baked_data_path = _baked_agent_data_path(local_provider, lease_agent_id)
    baked_data_path.parent.mkdir(parents=True, exist_ok=True)
    baked_data_path.write_text(json.dumps(data))
    return baked_data_path


def _baked_agent_data(lease_agent_id: AgentId, work_dir: Path) -> dict[str, Any]:
    return {
        "id": str(lease_agent_id),
        "name": "system-services",
        "type": "generic",
        "command": "sleep 1",
        "work_dir": str(work_dir),
        "create_time": "2026-01-01T00:00:00+00:00",
    }


# this tests: IF the agent was created afresh at the lease's id (no baked state was on disk)
# THEN: provision_agent runs mngr's full provisioning, even though the fresh data.json now sits at the pre-baked id
def test_provision_agent_runs_the_full_provisioning_after_a_create_without_baked_state(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = make_local_host_of_class(
        local_provider,
        ImbueCloudHost,
        pre_baked_agent_id=lease_agent_id,
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )
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
    host = make_local_host_of_class(
        local_provider,
        ImbueCloudHost,
        pre_baked_agent_id=lease_agent_id,
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )
    _write_baked_agent_data(local_provider, lease_agent_id, _baked_agent_data(lease_agent_id, temp_work_dir))
    options = _create_options(agent_id=None)
    agent = host.create_agent_state(temp_work_dir, options)

    host.provision_agent(agent, options, host.mngr_ctx)

    assert agent.id == lease_agent_id
    assert not _agent_shared_lib_path(local_provider, lease_agent_id).exists()


class _StartRecordingImbueCloudHost(ImbueCloudHost):
    """Answers the start path's shell commands without running them (the session probe reports no session)."""

    _recorded_commands: list[str] = PrivateAttr(default_factory=list)

    @property
    def recorded_commands(self) -> tuple[str, ...]:
        return tuple(self._recorded_commands)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        self._recorded_commands.append(command)
        if command.startswith("tmux has-session") and "tmux new-session" not in command:
            return CommandResult(stdout="", stderr="can't find session: system-services\n", success=False)
        return CommandResult(stdout="", stderr="", success=True)


def _make_freshly_leased_host(
    local_provider: LocalProviderInstance,
    lease_agent_id: AgentId,
    host_class: type[ImbueCloudHost],
) -> ImbueCloudHost:
    return make_local_host_of_class(
        local_provider,
        host_class,
        pre_baked_agent_id=lease_agent_id,
        is_freshly_leased_by_this_process=True,
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )


def _write_host_env(host: ImbueCloudHost, content: str) -> None:
    env_path = host.get_host_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(content)


# this tests: IF the fast path's create holds the lock on a host it just leased
# THEN: the host env file is read once, writes keep the in-memory copy current, and the copy lapses with the lock
def test_freshly_leased_host_keeps_the_host_env_in_memory_while_it_holds_the_lock(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_freshly_leased_host(local_provider, AgentId.generate(), host_class=ImbueCloudHost)
    _write_host_env(host, "MNGR_PREFIX=baked-48213-\n")

    with host.lock_cooperatively(timeout_seconds=None):
        host.set_env_vars({"LATCHKEY_GATEWAY": "http://gateway-48213"})
        _write_host_env(host, "MNGR_PREFIX=rewritten-elsewhere-48213-\n")

        assert host.get_env_vars() == {"MNGR_PREFIX": "baked-48213-", "LATCHKEY_GATEWAY": "http://gateway-48213"}

    assert host.get_env_vars() == {"MNGR_PREFIX": "rewritten-elsewhere-48213-"}


# this tests: IF something rewrites the host env between the fast path's env write and its lock
# (a post-host-create command runs there) THEN: the create reads the rewritten file once it holds the lock
def test_freshly_leased_host_rereads_a_host_env_changed_before_it_took_the_lock(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_freshly_leased_host(local_provider, AgentId.generate(), host_class=ImbueCloudHost)
    _write_host_env(host, "MNGR_PREFIX=baked-30577-\n")
    host.set_env_vars({"LATCHKEY_GATEWAY": "http://gateway-30577"})
    assert host.get_env_vars() == {"MNGR_PREFIX": "baked-30577-", "LATCHKEY_GATEWAY": "http://gateway-30577"}

    _write_host_env(host, "MNGR_PREFIX=baked-30577-\nANTHROPIC_API_KEY=sk-30577\n")

    with host.lock_cooperatively(timeout_seconds=None):
        assert host.get_env_vars() == {"MNGR_PREFIX": "baked-30577-", "ANTHROPIC_API_KEY": "sk-30577"}


# this tests: IF the fast path finds no baked agent state, so mngr's full create and provisioning run
# THEN: the host stops keeping files in memory, since provisioning commands may rewrite them
def test_freshly_leased_host_without_baked_state_rereads_the_host_env(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    host = _make_freshly_leased_host(local_provider, AgentId.generate(), host_class=ImbueCloudHost)
    _write_host_env(host, "MNGR_PREFIX=baked-81120-\n")

    with host.lock_cooperatively(timeout_seconds=None):
        assert host.get_env_vars() == {"MNGR_PREFIX": "baked-81120-"}
        host.create_agent_state(temp_work_dir, _create_options(agent_id=None))
        _write_host_env(host, "MNGR_PREFIX=provisioned-81120-\n")

        assert host.get_env_vars() == {"MNGR_PREFIX": "provisioned-81120-"}


# this tests: IF a leased host was not built by this process's fast path
# THEN: it reads the host env from disk on every read, even while it holds the lock
def test_host_not_freshly_leased_rereads_the_host_env_every_time(local_provider: LocalProviderInstance) -> None:
    host = make_local_host_of_class(
        local_provider,
        ImbueCloudHost,
        pre_baked_agent_id=AgentId.generate(),
        published_endpoint_source=MockLeasePublishedEndpoints(),
    )
    _write_host_env(host, "MNGR_PREFIX=first-71542-\n")

    with host.lock_cooperatively(timeout_seconds=None):
        assert host.get_env_vars() == {"MNGR_PREFIX": "first-71542-"}

        _write_host_env(host, "MNGR_PREFIX=second-71542-\n")

        assert host.get_env_vars() == {"MNGR_PREFIX": "second-71542-"}


# this tests: IF the fast path adopts the pre-baked agent on a host it just leased
# THEN: the agent's data.json is read from the container once; later steps of the create reuse that read
def test_freshly_leased_host_reads_the_baked_agent_data_once(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = _make_freshly_leased_host(local_provider, lease_agent_id, host_class=ImbueCloudHost)
    baked_data = _baked_agent_data(lease_agent_id, temp_work_dir)
    baked_data_path = _write_baked_agent_data(local_provider, lease_agent_id, baked_data)
    options = _create_options(agent_id=None)

    with host.lock_cooperatively(timeout_seconds=None):
        work_dir_result = host.create_agent_work_dir(host, temp_work_dir, options)
        _write_baked_agent_data(local_provider, lease_agent_id, {**baked_data, "work_dir": "/elsewhere-60318"})

        agent = host.create_agent_state(work_dir_result.path, options)

    assert agent.work_dir == temp_work_dir
    assert json.loads(baked_data_path.read_text())["work_dir"] == str(temp_work_dir)


def _make_freshly_leased_start_recording_host(
    local_provider: LocalProviderInstance, lease_agent_id: AgentId, temp_work_dir: Path
) -> _StartRecordingImbueCloudHost:
    """A freshly leased host whose baked agent records a 123x45 tmux size."""
    host = _make_freshly_leased_host(local_provider, lease_agent_id, host_class=_StartRecordingImbueCloudHost)
    assert isinstance(host, _StartRecordingImbueCloudHost)
    baked_data = {**_baked_agent_data(lease_agent_id, temp_work_dir), "tmux": {"width": 123, "height": 45}}
    _write_baked_agent_data(local_provider, lease_agent_id, baked_data)
    return host


def _resize_baked_agent_on_disk_behind_the_hosts_back(
    local_provider: LocalProviderInstance, lease_agent_id: AgentId
) -> None:
    patched_data = json.loads(_baked_agent_data_path(local_provider, lease_agent_id).read_text())
    _write_baked_agent_data(local_provider, lease_agent_id, {**patched_data, "tmux": {"width": 321, "height": 54}})


def _tmux_launch_commands(host: _StartRecordingImbueCloudHost) -> list[str]:
    return [command for command in host.recorded_commands if "tmux new-session" in command]


# this tests: IF the fast path starts the pre-baked agent on a host it just leased
# THEN: the start reads the agent's data.json from memory, and the host reads it from disk once the agent started
def test_freshly_leased_host_starts_its_agent_from_memory_and_rereads_afterwards(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = _make_freshly_leased_start_recording_host(local_provider, lease_agent_id, temp_work_dir)

    with host.lock_cooperatively(timeout_seconds=None):
        agent = host.create_agent_state(temp_work_dir, _create_options(agent_id=None))
        _resize_baked_agent_on_disk_behind_the_hosts_back(local_provider, lease_agent_id)

        host.start_agents([lease_agent_id])

        launch_commands = _tmux_launch_commands(host)
        assert len(launch_commands) == 1
        assert " -x 123 -y 45 " in launch_commands[0]
        assert host.get_agent_tmux_options(agent).width == 321


# this tests: IF a start on a host it just leased launches more than the pre-baked agent
# THEN: the start reads the agent's data.json from disk, since an agent launched earlier in it may write the file
def test_freshly_leased_host_starting_more_than_its_pre_baked_agent_reads_from_disk(
    local_provider: LocalProviderInstance, temp_work_dir: Path
) -> None:
    lease_agent_id = AgentId.generate()
    host = _make_freshly_leased_start_recording_host(local_provider, lease_agent_id, temp_work_dir)

    with host.lock_cooperatively(timeout_seconds=None):
        host.create_agent_state(temp_work_dir, _create_options(agent_id=None))
        _resize_baked_agent_on_disk_behind_the_hosts_back(local_provider, lease_agent_id)

        with pytest.raises(AgentNotFoundOnHostError):
            host.start_agents([lease_agent_id, AgentId.generate()])

        launch_commands = _tmux_launch_commands(host)
        assert len(launch_commands) == 1
        assert " -x 321 -y 54 " in launch_commands[0]


# this tests: IF a leased host is asked for its published endpoints THEN: it asks its provider's
# lease record every time, so a restore that re-picked the box ports is reported without a rebuild
def test_get_published_endpoints_reads_the_lease_record_on_every_call(local_provider: LocalProviderInstance) -> None:
    source = MockLeasePublishedEndpoints()
    host = make_local_host_of_class(
        local_provider, ImbueCloudHost, pre_baked_agent_id=AgentId.generate(), published_endpoint_source=source
    )
    assert host.get_published_endpoints() == []
    moved = PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="203.0.113.9", host_port=23012)
    source.endpoints = [moved]
    assert host.get_published_endpoints() == [moved]
    assert source.asked_host_ids == [host.id, host.id]
