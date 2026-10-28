"""Unit tests for the connect API module."""

import os
import shlex
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pluggy
import pytest
from pydantic import Field
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.suspension import ClockReading
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.connect import SIGNAL_EXIT_CODE_DESTROY
from imbue.mngr.api.connect import SIGNAL_EXIT_CODE_STOP
from imbue.mngr.api.connect import SSH_ERROR_EXIT_CODE
from imbue.mngr.api.connect import TERMINAL_MODE_RESET
from imbue.mngr.api.connect import _build_ssh_activity_wrapper_script
from imbue.mngr.api.connect import _build_ssh_args
from imbue.mngr.api.connect import _determine_post_disconnect_action
from imbue.mngr.api.connect import build_attach_argv
from imbue.mngr.api.connect import connect_to_agent
from imbue.mngr.api.connect import resolve_connect_command
from imbue.mngr.api.connect import restore_terminal_modes
from imbue.mngr.api.connect import run_connect_command
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostOfflineError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NestedTmuxError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import RestorableMockProviderInstance
from imbue.mngr.providers.mock_provider_test import make_offline_host

_TEST_AGENT_ID = AgentId("agent-6f1c3d9a2b8e4f7a9c0d1e2f3a4b5c6d")
_TEST_HOST_ID = HostId("host-0a1b2c3d4e5f60718293a4b5c6d7e8f9")


def test_build_ssh_activity_wrapper_script_creates_activity_directory() -> None:
    """Test that the wrapper script creates the activity directory."""
    script = _build_ssh_activity_wrapper_script("mngr-test-session", _TEST_AGENT_ID, Path("/home/user/.mngr"))

    assert "mkdir -p '/home/user/.mngr/activity'" in script


def test_build_ssh_activity_wrapper_script_writes_to_activity_file() -> None:
    """Test that the wrapper script writes to the activity/ssh file."""
    script = _build_ssh_activity_wrapper_script("mngr-test-session", _TEST_AGENT_ID, Path("/home/user/.mngr"))

    assert "'/home/user/.mngr/activity/ssh'" in script


def test_build_ssh_activity_wrapper_script_attaches_to_tmux_session() -> None:
    """Test that the wrapper script attaches to the correct tmux session."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", _TEST_AGENT_ID, Path("/home/user/.mngr"))

    # The attach command is built via build_attach_argv (raw `=name` from
    # TmuxSessionTarget.as_target_arg) and rendered with shlex.join, which quotes a
    # token only if it contains shell-special characters; '=mngr-my-agent' has none,
    # so it appears unquoted.
    assert "tmux attach -t =mngr-my-agent" in script


def test_build_ssh_activity_wrapper_script_without_attach_args_is_plain_attach() -> None:
    """With no attach_args the attach command is the plain `tmux attach` (no client flags)."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", _TEST_AGENT_ID, Path("/home/user/.mngr"))
    assert "tmux attach -t =mngr-my-agent" in script


def test_build_ssh_activity_wrapper_script_inserts_attach_args_before_subcommand() -> None:
    """attach_args are tmux client flags and must appear before the `attach` subcommand,
    e.g. `tmux -CC attach` for iTerm2 control mode."""
    script = _build_ssh_activity_wrapper_script(
        "mngr-my-agent", _TEST_AGENT_ID, Path("/home/user/.mngr"), attach_args=("-CC",)
    )
    assert "tmux -CC attach -t =mngr-my-agent" in script


def test_build_ssh_activity_wrapper_script_no_longer_sends_sigwinch() -> None:
    """The post-attach SIGWINCH nudge has moved to a per-session tmux client-attached
    hook (set at session creation), so the connect wrapper must no longer send it.
    This guards against reintroducing a redundant second nudge on `mngr connect`."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", _TEST_AGENT_ID, Path("/home/user/.mngr"))

    assert "kill -WINCH" not in script
    assert "window-size" not in script


def test_build_ssh_activity_wrapper_script_kills_activity_tracker_on_exit() -> None:
    """Test that the wrapper script kills the activity tracker when tmux exits."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/tmp/.mngr"))

    assert "kill $MNGR_ACTIVITY_PID" in script


def test_build_ssh_activity_wrapper_script_writes_json_with_time_and_pid() -> None:
    """Test that the activity file contains JSON with time and ssh_pid."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/tmp/.mngr"))

    # The script should write JSON with time and ssh_pid fields
    assert "time" in script
    assert "ssh_pid" in script
    assert "TIME_MS" in script


def test_build_ssh_activity_wrapper_script_handles_paths_with_spaces() -> None:
    """Test that the wrapper script handles paths with spaces correctly."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/home/user/my dir/.mngr"))

    # Paths should be quoted to handle spaces
    assert "'/home/user/my dir/.mngr/activity'" in script
    assert "'/home/user/my dir/.mngr/activity/ssh'" in script


def test_build_ssh_activity_wrapper_script_checks_for_signal_file() -> None:
    """Test that the wrapper script checks the agent's signal file, then the legacy session-named one."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", _TEST_AGENT_ID, Path("/home/user/.mngr"))

    assert (
        f"for SIGNAL_FILE in /home/user/.mngr/signals/{_TEST_AGENT_ID} /home/user/.mngr/signals/mngr-my-agent;"
        in script
    )


def test_build_ssh_activity_wrapper_script_exits_with_destroy_code_on_destroy_signal() -> None:
    """Test that the wrapper script exits with SIGNAL_EXIT_CODE_DESTROY when signal is 'destroy'."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/tmp/.mngr"))

    assert f"exit {SIGNAL_EXIT_CODE_DESTROY}" in script
    assert '"destroy"' in script


def test_build_ssh_activity_wrapper_script_exits_with_stop_code_on_stop_signal() -> None:
    """Test that the wrapper script exits with SIGNAL_EXIT_CODE_STOP when signal is 'stop'."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/tmp/.mngr"))

    assert f"exit {SIGNAL_EXIT_CODE_STOP}" in script
    assert '"stop"' in script


def test_build_ssh_activity_wrapper_script_removes_signal_file_after_reading() -> None:
    """Test that the wrapper script removes the signal file after reading it."""
    script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/tmp/.mngr"))

    assert 'rm -f "$SIGNAL_FILE"' in script


def test_build_ssh_activity_wrapper_script_clears_stale_signal_files_before_attaching() -> None:
    """A signal file left by an earlier connect must not turn this connect's plain detach into a stop."""
    script = _build_ssh_activity_wrapper_script("mngr-unique-session", _TEST_AGENT_ID, Path("/data/.mngr"))

    clear_command = f"rm -f /data/.mngr/signals/{_TEST_AGENT_ID} /data/.mngr/signals/mngr-unique-session;"
    assert script.index(clear_command) < script.index("tmux attach")


# Tests for _build_ssh_args


def _create_pyinfra_ssh_host(
    hostname: str,
    data: dict[str, Any],
) -> PyinfraHost:
    """Create a real pyinfra Host with the given SSH connection data."""
    names_data = ([(hostname, data)], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)
    pyinfra_host = inventory.get_host(hostname)
    pyinfra_host.init(state)
    return pyinfra_host


def _make_ssh_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    hostname: str = "example.com",
    ssh_user: str | None = "ubuntu",
    ssh_port: int | None = 22,
    ssh_key: str | None = "/home/user/.ssh/id_rsa",
    ssh_known_hosts_file: str | None = None,
) -> Host:
    """Create a real Host with an SSH pyinfra connector for testing."""
    host_data: dict[str, Any] = {}
    if ssh_user is not None:
        host_data["ssh_user"] = ssh_user
    if ssh_port is not None:
        host_data["ssh_port"] = ssh_port
    if ssh_key is not None:
        host_data["ssh_key"] = ssh_key
    if ssh_known_hosts_file is not None:
        host_data["ssh_known_hosts_file"] = ssh_known_hosts_file

    pyinfra_host = _create_pyinfra_ssh_host(hostname, host_data)
    connector = PyinfraConnector(pyinfra_host)

    return Host(
        id=HostId(f"host-{uuid4().hex}"),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
    )


class _TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name.

    BaseAgent.get_expected_process_name reads data.json via the host connector,
    which fails for SSH hosts in tests since no SSH server is running. This
    subclass returns a fixed process name to avoid that code path.
    """

    def get_expected_process_name(self) -> str:
        return "test-process"


def _make_remote_agent(
    host: Host,
    temp_mngr_ctx: MngrContext,
    agent_name: str = "test-agent",
) -> _TestAgent:
    """Create a test agent on a remote host for testing connect_to_agent."""
    return _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("generic"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=temp_mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


def test_build_ssh_args_with_known_hosts_file(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args uses StrictHostKeyChecking=yes with a known_hosts file."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-i" in args
    assert "/home/user/.ssh/id_rsa" in args
    assert "-p" in args
    assert "22" in args
    # Double-quoted for ssh: UserKnownHostsFile is a whitespace-separated list.
    assert 'UserKnownHostsFile="/tmp/known_hosts"' in args
    assert "StrictHostKeyChecking=yes" in " ".join(args)
    assert "ubuntu@example.com" in args


def test_build_ssh_args_quotes_a_known_hosts_path_with_spaces(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A spaced known_hosts path stays one file: these args are exec'd, so ssh is the only parser.

    ssh splits UserKnownHostsFile on whitespace itself, so an unquoted spaced path becomes
    several nonexistent files and StrictHostKeyChecking=yes then refuses the connection.
    """
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file="/path with spaces/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert 'UserKnownHostsFile="/path with spaces/known_hosts"' in args


def test_build_ssh_args_with_allow_unknown_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args disables host key checking when allowed."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file=None)
    opts = ConnectionOptions(is_unknown_host_allowed=True)

    args = _build_ssh_args(host, opts)

    assert "StrictHostKeyChecking=no" in " ".join(args)
    assert "UserKnownHostsFile=/dev/null" in " ".join(args)


def test_build_ssh_args_raises_without_known_hosts_or_allow_unknown(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args raises MngrError when no known_hosts and not allowing unknown."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file=None)
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    with pytest.raises(MngrError, match="known_hosts"):
        _build_ssh_args(host, opts)


def test_build_ssh_args_without_user(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits user@ when ssh_user is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_user=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    # Should have bare hostname, not user@hostname
    assert "example.com" in args
    assert not any("@" in arg for arg in args)


def test_build_ssh_args_without_port(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits -p when ssh_port is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_port=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-p" not in args


def test_build_ssh_args_without_key(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits -i when ssh_key is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_key=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-i" not in args


def test_build_ssh_args_known_hosts_dev_null_treated_as_missing(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that /dev/null known_hosts is treated as no known_hosts file."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file="/dev/null")
    opts = ConnectionOptions(is_unknown_host_allowed=True)

    args = _build_ssh_args(host, opts)

    # Should fall through to the allow_unknown_host branch
    assert "StrictHostKeyChecking=no" in " ".join(args)


# Tests for _determine_post_disconnect_action


def test_determine_post_disconnect_action_destroy_signal() -> None:
    """SIGNAL_EXIT_CODE_DESTROY maps to a mngr destroy of this one instance (a same-id agent on another host is spared)."""
    action = _determine_post_disconnect_action(SIGNAL_EXIT_CODE_DESTROY, _TEST_AGENT_ID, _TEST_HOST_ID)

    assert action is not None
    executable, argv = action
    assert executable == "mngr"
    assert argv == ["mngr", "destroy", f"{_TEST_AGENT_ID}@{_TEST_HOST_ID}", "-f"]
    assert parse_agent_address(argv[2]) == AgentAddress(agent=_TEST_AGENT_ID, host=HostAddress(host=_TEST_HOST_ID))


def test_determine_post_disconnect_action_stop_signal() -> None:
    """Test that SIGNAL_EXIT_CODE_STOP maps to a mngr stop action."""
    action = _determine_post_disconnect_action(SIGNAL_EXIT_CODE_STOP, _TEST_AGENT_ID, _TEST_HOST_ID)

    assert action is not None
    executable, argv = action
    assert executable == "mngr"
    assert argv == ["mngr", "stop", f"{_TEST_AGENT_ID}@{_TEST_HOST_ID}"]


def test_determine_post_disconnect_action_normal_exit_returns_none() -> None:
    """Test that a normal exit (code 0) returns no action."""
    action = _determine_post_disconnect_action(0, _TEST_AGENT_ID, _TEST_HOST_ID)

    assert action is None


def test_determine_post_disconnect_action_unknown_exit_code_returns_none() -> None:
    """Test that an unexpected exit code returns no action."""
    action = _determine_post_disconnect_action(255, _TEST_AGENT_ID, _TEST_HOST_ID)

    assert action is None


# Tests for connect_to_agent's remote session


class _ConnectRecorder:
    """Stands in for ssh, exec and the clocks during connect_to_agent, recording what each saw.

    Each ssh run returns the next scripted exit code (the last one repeats). The clocks are read
    once before and once after every ssh run; a run listed in ``suspended_ssh_runs`` ends with
    the wall clock a minute further ahead than the monotonic one.
    """

    def __init__(self, ssh_exit_codes: Sequence[int], suspended_ssh_runs: frozenset[int] = frozenset()) -> None:
        self.ssh_exit_codes = list(ssh_exit_codes)
        self.suspended_ssh_runs = suspended_ssh_runs
        self.ssh_calls: list[list[str]] = []
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.clock_reads = 0
        self.wall_offset_seconds = 0.0

    def run_ssh(self, ssh_args: Sequence[str], env: Mapping[str, str]) -> int:
        self.ssh_calls.append(list(ssh_args))
        return self.ssh_exit_codes[min(len(self.ssh_calls) - 1, len(self.ssh_exit_codes) - 1)]

    def exec_action(self, executable: str, argv: list[str]) -> None:
        self.exec_calls.append((executable, list(argv)))

    def read_clock(self) -> ClockReading:
        ssh_run, is_after_run = divmod(self.clock_reads, 2)
        if is_after_run and ssh_run in self.suspended_ssh_runs:
            self.wall_offset_seconds += 60.0
        self.clock_reads += 1
        return ClockReading(
            wall_seconds=self.clock_reads + self.wall_offset_seconds, monotonic_seconds=float(self.clock_reads)
        )


class _ReconnectAgent(_TestAgent):
    """Test agent with a fixed lifecycle state that records start requests instead of starting."""

    lifecycle_state: AgentLifecycleState = AgentLifecycleState.RUNNING
    start_requests: list[bool] = Field(default_factory=list)

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.lifecycle_state

    def get_ready_timeout_seconds(self) -> float:
        return 1.0

    def wait_for_ready_signal(
        self, is_readiness_awaited: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        self.start_requests.append(is_readiness_awaited)


class _HostWithAgents(Host):
    """SSH host whose agents are a fixed list rather than read over the connection."""

    agents_on_host: list[AgentInterface] = Field(default_factory=list)

    def get_agents(self) -> list[AgentInterface]:
        return list(self.agents_on_host)


def _make_remote_agent_and_provider(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    hostname: str = "example.com",
    agent_name: str = "test-agent",
) -> tuple[Host, _TestAgent, RestorableMockProviderInstance]:
    """A remote agent on an SSH host with a known_hosts file, and a provider holding no hosts yet."""
    host = _make_ssh_host(local_provider, mngr_ctx, hostname=hostname, ssh_known_hosts_file="/tmp/known_hosts")
    agent = _make_remote_agent(host, mngr_ctx, agent_name=agent_name)
    provider = RestorableMockProviderInstance(
        name=ProviderInstanceName(f"mock-{uuid4().hex}"), host_dir=Path("/mngr"), mngr_ctx=mngr_ctx
    )
    return host, agent, provider


def _make_offline_record(
    host_id: HostId,
    provider: RestorableMockProviderInstance,
    mngr_ctx: MngrContext,
    stop_reason: str | None,
    failure_reason: str | None = None,
) -> OfflineHost:
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test",
        created_at=now,
        updated_at=now,
        stop_reason=stop_reason,
        failure_reason=failure_reason,
    )
    return make_offline_host(certified_data, provider, mngr_ctx)


def _make_restored_host(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    host_id: HostId,
    agent_id: AgentId,
    lifecycle_state: AgentLifecycleState,
) -> tuple[_HostWithAgents, _ReconnectAgent]:
    """A host at a new endpoint, as a restore produces, holding an agent with the given id."""
    restored_host = _HostWithAgents(
        id=host_id,
        host_name=HostName("test"),
        connector=PyinfraConnector(
            _create_pyinfra_ssh_host(
                "new.example.com", {"ssh_user": "ubuntu", "ssh_port": 2222, "ssh_known_hosts_file": "/tmp/known_hosts"}
            )
        ),
        provider_instance=local_provider,
        mngr_ctx=mngr_ctx,
    )
    restored_agent = _ReconnectAgent(
        id=agent_id,
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("generic"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host_id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=restored_host,
        lifecycle_state=lifecycle_state,
    )
    restored_host.agents_on_host.append(restored_agent)
    return restored_host, restored_agent


@contextmanager
def _open_terminal() -> Iterator[tuple[int, int]]:
    """A pseudo-terminal as ``(master_fd, terminal_fd)``, both closed on exit."""
    master_fd, terminal_fd = os.openpty()
    try:
        yield master_fd, terminal_fd
    finally:
        # A slave closed with output still unread blocks on macOS until it drains.
        os.close(master_fd)
        os.close(terminal_fd)


def _read_terminal(master_fd: int) -> bytes:
    os.set_blocking(master_fd, False)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(master_fd, 4096):
            chunks.append(chunk)
    except BlockingIOError:
        pass
    return b"".join(chunks)


def _run_remote_connect(
    agent: AgentInterface,
    host: Host,
    mngr_ctx: MngrContext,
    recorder: _ConnectRecorder,
    provider: RestorableMockProviderInstance,
    connection_opts: ConnectionOptions,
) -> bytes:
    """Run connect_to_agent against a pseudo-terminal and return what it wrote to the terminal."""
    with _open_terminal() as (master_fd, terminal_fd):
        connect_to_agent(
            agent,
            host,
            mngr_ctx,
            connection_opts,
            provider=provider,
            run_ssh=recorder.run_ssh,
            exec_action=recorder.exec_action,
            read_clock=recorder.read_clock,
            terminal_fd=terminal_fd,
        )
        return _read_terminal(master_fd)


def _connect_remote_agent_staying_online(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    ssh_exit_codes: Sequence[int],
    retry_count: int,
    agent_name: str = "test-agent",
    suspended_ssh_runs: frozenset[int] = frozenset(),
) -> tuple[_ConnectRecorder, RestorableMockProviderInstance, str, bytes]:
    """Connect to an agent whose host stays online; returns the recorder, provider, agent address and terminal output."""
    host, agent, provider = _make_remote_agent_and_provider(local_provider, mngr_ctx, agent_name=agent_name)
    provider.mock_hosts = [host]
    recorder = _ConnectRecorder(ssh_exit_codes, suspended_ssh_runs=suspended_ssh_runs)
    opts = ConnectionOptions(retry_count=retry_count, retry_delay="1s")
    terminal_output = _run_remote_connect(agent, host, mngr_ctx, recorder, provider, opts)
    return recorder, provider, f"{agent.id}@{host.id}", terminal_output


@pytest.mark.parametrize(
    ("exit_code", "expected_exec"),
    [
        (SIGNAL_EXIT_CODE_DESTROY, ("mngr", ["mngr", "destroy", "{address}", "-f"])),
        (SIGNAL_EXIT_CODE_STOP, ("mngr", ["mngr", "stop", "{address}"])),
        (0, None),
    ],
)
def test_connect_to_agent_remote_clean_exit_runs_its_action_once_without_touching_the_terminal(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    exit_code: int,
    expected_exec: tuple[str, list[str]] | None,
) -> None:
    recorder, provider, address, terminal_output = _connect_remote_agent_staying_online(
        local_provider, temp_mngr_ctx, ssh_exit_codes=[exit_code], retry_count=2
    )

    expected_execs = (
        []
        if expected_exec is None
        else [(expected_exec[0], [arg.format(address=address) for arg in expected_exec[1]])]
    )
    assert recorder.exec_calls == expected_execs
    assert len(recorder.ssh_calls) == 1
    assert provider.connection_errors_cleared == []
    assert terminal_output == b""


@pytest.mark.parametrize("exit_code", [1, 127, -15])
def test_connect_to_agent_remote_raises_on_a_failure_that_is_not_a_lost_connection(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    exit_code: int,
) -> None:
    with pytest.raises(MngrError, match=f"ended with exit code {exit_code}"):
        _connect_remote_agent_staying_online(local_provider, temp_mngr_ctx, ssh_exit_codes=[exit_code], retry_count=2)


@pytest.mark.allow_warnings(match=r"^SSH connection failed")
def test_connect_to_agent_retries_a_lost_connection_to_an_online_host_then_raises(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx)
    provider.mock_hosts = [host]
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE])

    with pytest.raises(HostConnectionError, match="after 2 retries"):
        _run_remote_connect(
            agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=2, retry_delay="1s")
        )

    assert len(recorder.ssh_calls) == 3
    assert provider.connection_errors_cleared == [host.id, host.id, host.id]
    assert provider.started_hosts == []


def test_connect_to_agent_with_a_negative_retry_count_raises_after_one_lost_connection(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx)
    provider.mock_hosts = [host]
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE])

    with pytest.raises(HostConnectionError):
        _run_remote_connect(agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=-1))

    assert len(recorder.ssh_calls) == 1


def test_connect_to_agent_resets_the_terminal_after_each_lost_connection(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    recorder, _, _, terminal_output = _connect_remote_agent_staying_online(
        local_provider,
        temp_mngr_ctx,
        ssh_exit_codes=[SSH_ERROR_EXIT_CODE, SSH_ERROR_EXIT_CODE, 0],
        retry_count=0,
        suspended_ssh_runs=frozenset({0, 1}),
    )

    assert len(recorder.ssh_calls) == 3
    assert terminal_output == TERMINAL_MODE_RESET * 2


def test_connect_to_agent_reconnects_without_a_retry_after_a_drop_that_spanned_a_suspension(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    recorder, _, _, _ = _connect_remote_agent_staying_online(
        local_provider,
        temp_mngr_ctx,
        ssh_exit_codes=[SSH_ERROR_EXIT_CODE, 0],
        retry_count=0,
        suspended_ssh_runs=frozenset({0}),
    )

    assert len(recorder.ssh_calls) == 2


@pytest.mark.parametrize("lifecycle_state", [AgentLifecycleState.RUNNING, AgentLifecycleState.STOPPED])
def test_connect_to_agent_restores_a_host_that_paused_and_reconnects_to_its_new_address(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    lifecycle_state: AgentLifecycleState,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx, hostname="old.example.com")
    provider.mock_hosts = [_make_offline_record(host.id, provider, temp_mngr_ctx, stop_reason=HostState.PAUSED.value)]
    restored_host, restored_agent = _make_restored_host(
        local_provider, temp_mngr_ctx, host.id, agent.id, lifecycle_state
    )
    provider.mock_restored_host = restored_host
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE, 0])

    _run_remote_connect(agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=0))

    assert len(recorder.ssh_calls) == 2
    assert "ubuntu@old.example.com" in recorder.ssh_calls[0]
    assert "ubuntu@new.example.com" in recorder.ssh_calls[1]
    assert recorder.ssh_calls[1][recorder.ssh_calls[1].index("-p") + 1] == "2222"
    assert f"tmux attach -t ={agent.session_name}" in recorder.ssh_calls[1][-1]
    assert provider.started_hosts == [host.id]
    assert provider.connection_errors_cleared == [host.id]
    assert provider.cache_resets == 1
    assert restored_agent.start_requests == ([] if lifecycle_state == AgentLifecycleState.RUNNING else [False])


@pytest.mark.parametrize(
    ("stop_reason", "failure_reason", "expected_state"),
    [
        (HostState.STOPPED.value, None, HostState.STOPPED),
        (None, None, HostState.CRASHED),
        (None, "build failed", HostState.FAILED),
    ],
)
def test_connect_to_agent_does_not_restore_a_host_that_did_not_pause(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    stop_reason: str | None,
    failure_reason: str | None,
    expected_state: HostState,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx)
    provider.mock_hosts = [
        _make_offline_record(host.id, provider, temp_mngr_ctx, stop_reason=stop_reason, failure_reason=failure_reason)
    ]
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE])

    with pytest.raises(HostOfflineError, match=f"is {expected_state}"):
        _run_remote_connect(agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=2))

    assert len(recorder.ssh_calls) == 1
    assert provider.started_hosts == []


def test_connect_to_agent_does_not_restore_a_paused_host_when_auto_start_is_disabled(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx)
    provider.mock_hosts = [_make_offline_record(host.id, provider, temp_mngr_ctx, stop_reason=HostState.PAUSED.value)]
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE])

    with pytest.raises(UserInputError, match="automatic starting is disabled"):
        _run_remote_connect(
            agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(is_auto_start_allowed=False)
        )

    assert provider.started_hosts == []


def test_connect_to_agent_raises_when_the_restored_host_has_no_such_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx)
    provider.mock_hosts = [_make_offline_record(host.id, provider, temp_mngr_ctx, stop_reason=HostState.PAUSED.value)]
    restored_host, _ = _make_restored_host(
        local_provider, temp_mngr_ctx, host.id, AgentId(f"agent-{uuid4().hex}"), AgentLifecycleState.RUNNING
    )
    provider.mock_restored_host = restored_host
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE, 0])

    with pytest.raises(AgentNotFoundOnHostError):
        _run_remote_connect(agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=0))

    assert len(recorder.ssh_calls) == 1


@pytest.mark.allow_warnings(match=r"^SSH connection failed")
def test_connect_to_agent_reconnects_to_a_host_another_process_already_restored(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    host, agent, provider = _make_remote_agent_and_provider(local_provider, temp_mngr_ctx, hostname="old.example.com")
    restored_host, _ = _make_restored_host(
        local_provider, temp_mngr_ctx, host.id, agent.id, AgentLifecycleState.RUNNING
    )
    provider.mock_hosts = [restored_host]
    recorder = _ConnectRecorder([SSH_ERROR_EXIT_CODE, 0])

    _run_remote_connect(
        agent, host, temp_mngr_ctx, recorder, provider, ConnectionOptions(retry_count=1, retry_delay="1s")
    )

    assert "ubuntu@new.example.com" in recorder.ssh_calls[1]
    assert provider.started_hosts == []


def test_connect_to_agent_remote_uses_correct_session_name(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that connect_to_agent attaches to the session named from the context's prefix + agent name."""
    # Use a custom prefix (different from the default fixture prefix) to verify the code
    # reads the prefix from the context rather than using a hardcoded value
    custom_config = MngrConfig(default_host_dir=temp_host_dir, prefix="custom-")
    custom_ctx = MngrContext(config=custom_config, pm=plugin_manager, profile_dir=temp_profile_dir)

    recorder, _, _, _ = _connect_remote_agent_staying_online(
        local_provider, custom_ctx, ssh_exit_codes=[SIGNAL_EXIT_CODE_DESTROY], retry_count=0, agent_name="my-agent"
    )

    assert len(recorder.ssh_calls) == 1
    assert "tmux attach -t =custom-my-agent" in recorder.ssh_calls[0][-1]


def test_restore_terminal_modes_writes_the_reset_to_a_terminal() -> None:
    with _open_terminal() as (master_fd, terminal_fd):
        restore_terminal_modes(terminal_fd, ())
        assert _read_terminal(master_fd) == TERMINAL_MODE_RESET


def test_restore_terminal_modes_skips_tmux_control_mode() -> None:
    with _open_terminal() as (master_fd, terminal_fd):
        restore_terminal_modes(terminal_fd, ("-CC",))
        assert _read_terminal(master_fd) == b""


def test_restore_terminal_modes_skips_output_that_is_not_a_terminal() -> None:
    read_fd, write_fd = os.pipe()
    restore_terminal_modes(write_fd, ())
    os.close(write_fd)
    try:
        assert os.read(read_fd, 4096) == b""
    finally:
        os.close(read_fd)


def test_ssh_wrapper_script_is_correctly_quoted_for_bash_c() -> None:
    """Verify the wrapper script survives shell parsing as a single bash -c argument.

    SSH concatenates remote command arguments with spaces, so the wrapper must
    be shell-quoted into a single 'bash -c <quoted_script>' string. Otherwise
    bash -c only receives the first word (e.g. 'mkdir'), causing errors like
    'mkdir: missing operand'.
    """
    wrapper_script = _build_ssh_activity_wrapper_script("mngr-test", _TEST_AGENT_ID, Path("/mngr"))
    remote_command = "bash -c " + shlex.quote(wrapper_script)

    # When the remote shell parses this command, bash should receive
    # the full wrapper script as a single -c argument
    parsed = shlex.split(remote_command)
    assert parsed == ["bash", "-c", wrapper_script]


# Tests for nested tmux detection in connect_to_agent (local host)


def _make_local_host_and_agent(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    agent_name: str = "test-agent",
) -> tuple[Host, _TestAgent]:
    """Create a local host and agent for testing connect_to_agent."""
    host = Host(
        id=HostId(f"host-{uuid4().hex}"),
        host_name=HostName("test"),
        connector=PyinfraConnector(local_provider._create_local_pyinfra_host()),
        provider_instance=local_provider,
        mngr_ctx=mngr_ctx,
    )
    agent = _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("generic"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )
    return host, agent


def test_connect_to_agent_local_raises_nested_tmux_error_when_tmux_is_set(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent raises NestedTmuxError when $TMUX is set and is_nested_tmux_allowed is False."""
    host, agent = _make_local_host_and_agent(local_provider, temp_mngr_ctx)
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    # Simulate being inside a tmux session
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

    with pytest.raises(NestedTmuxError) as exc_info:
        connect_to_agent(agent, host, temp_mngr_ctx, opts)

    expected_session = f"{temp_mngr_ctx.config.prefix}test-agent"
    assert exc_info.value.session_name == expected_session
    assert expected_session in str(exc_info.value)
    assert exc_info.value.user_help_text is not None
    assert "is_nested_tmux_allowed" in exc_info.value.user_help_text


# Tests for run_connect_command


def test_run_connect_command_sets_env_vars_and_execs(tmp_path: Path) -> None:
    """Test that run_connect_command sets env vars and execs via sh.

    Since run_connect_command replaces the process via os.execvpe, we fork and run
    it in the child. The command writes env vars to a temp file so the parent can verify.
    """
    output_file = tmp_path / "connect_env_output.txt"
    command = f'echo "$MNGR_AGENT_NAME $MNGR_SESSION_NAME $MNGR_HOST_IS_LOCAL" > {output_file}'

    pid = os.fork()
    if pid == 0:
        run_connect_command(command, "my-agent", "mngr-my-agent", is_local=True)
        os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0

        content = output_file.read_text().strip()
        assert content == "my-agent mngr-my-agent true"


def test_run_connect_command_sets_host_is_local_false_for_remote(tmp_path: Path) -> None:
    """Test that MNGR_HOST_IS_LOCAL is 'false' when is_local=False."""
    output_file = tmp_path / "connect_env_output_remote.txt"
    command = f'echo "$MNGR_HOST_IS_LOCAL" > {output_file}'

    pid = os.fork()
    if pid == 0:
        run_connect_command(command, "remote-agent", "mngr-remote-agent", is_local=False)
        os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0

        content = output_file.read_text().strip()
        assert content == "false"


# resolve_connect_command tests


def test_resolve_connect_command_prefers_cli_option(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should prefer the CLI option over config."""
    result = resolve_connect_command("cli-command", temp_mngr_ctx)
    assert result == "cli-command"


def test_resolve_connect_command_falls_back_to_config(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should fall back to config.connect_command when CLI is None."""
    config_with_cmd = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().connect_command, "config-command"),
    )
    ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, config_with_cmd),
    )
    result = resolve_connect_command(None, ctx)
    assert result == "config-command"


def test_resolve_connect_command_returns_none_when_neither_set(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should return None when neither CLI nor config is set."""
    result = resolve_connect_command(None, temp_mngr_ctx)
    assert result is None


def test_build_attach_argv_without_attach_args_is_plain_attach() -> None:
    """With no attach_args, the attach argv is the plain `tmux attach -t =<session>`."""
    argv = build_attach_argv(TmuxSessionTarget(session_name="mngr-plain"), ())
    assert argv == ["tmux", "attach", "-t", "=mngr-plain"]


def test_build_attach_argv_inserts_attach_args_before_subcommand() -> None:
    """attach_args are tmux client flags and must appear before the `attach` subcommand,
    e.g. `tmux -CC attach` for iTerm2 control mode."""
    argv = build_attach_argv(TmuxSessionTarget(session_name="mngr-ccmode"), ("-CC",))
    assert argv == ["tmux", "-CC", "attach", "-t", "=mngr-ccmode"]


def test_build_attach_argv_uses_raw_exact_match_target() -> None:
    """The target is the raw `=name` (via TmuxSessionTarget.as_target_arg), not shell-quoted,
    since argv reaches the exec'd process verbatim."""
    argv = build_attach_argv(TmuxSessionTarget(session_name="mngr-weird name"), ("-CC", "-u"))
    assert argv == ["tmux", "-CC", "-u", "attach", "-t", "=mngr-weird name"]
