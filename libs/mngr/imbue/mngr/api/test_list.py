"""Integration tests for the list API module.

NOTE: this module also still contains a number of fast, no-network pure-function
unit tests (e.g. the ErrorInfo.build / agent_details_to_cel_context / _apply_cel_filters
cases below) that, by convention, belong in the unit module ``list_test.py`` rather
than an integration (``test_*.py``) module. They live here for historical reasons;
the genuinely-integration tmux/``list_agents`` tests are the ones that justify this
file's location. New pure-function unit tests should go in ``list_test.py``. A bulk
relocation of the existing pure tests is intentionally deferred to avoid high-churn,
low-correctness movement.
"""

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.list import AgentErrorInfo
from imbue.mngr.api.list import ErrorInfo
from imbue.mngr.api.list import HostErrorInfo
from imbue.mngr.api.list import ProviderErrorInfo
from imbue.mngr.api.list import _apply_cel_filters
from imbue.mngr.api.list import agent_details_to_cel_context
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import tmux_session_cleanup

# =============================================================================
# Error Info Tests
# =============================================================================


def test_error_info_build_creates_error_info() -> None:
    """Test that ErrorInfo.build creates an error info from an exception."""
    exception = RuntimeError("Test error message")

    error_info = ErrorInfo.build(exception)

    assert error_info.exception_type == "RuntimeError"
    assert error_info.message == "Test error message"


def test_error_info_build_handles_mngr_error() -> None:
    """Test that ErrorInfo.build handles MngrError subclasses."""

    class CustomMngrError(MngrError):
        """Custom test error."""

    exception = CustomMngrError("Custom error")

    error_info = ErrorInfo.build(exception)

    assert error_info.exception_type == "CustomMngrError"
    assert error_info.message == "Custom error"


def test_provider_error_info_build_for_provider() -> None:
    """Test that ProviderErrorInfo.build_for_provider creates error with provider context."""
    exception = RuntimeError("Provider failed")
    provider_name = ProviderInstanceName("test-provider")

    error_info = ProviderErrorInfo.build_for_provider(exception, provider_name)

    assert error_info.exception_type == "RuntimeError"
    assert error_info.message == "Provider failed"
    assert error_info.provider_name == provider_name


def test_host_error_info_build_for_host() -> None:
    """Test that HostErrorInfo.build_for_host creates error with host context."""
    exception = RuntimeError("Host failed")
    host_id = HostId.generate()

    error_info = HostErrorInfo.build_for_host(exception, host_id)

    assert error_info.exception_type == "RuntimeError"
    assert error_info.message == "Host failed"
    assert error_info.host_id == host_id


def test_agent_error_info_build_for_agent() -> None:
    """Test that AgentErrorInfo.build_for_agent creates error with agent context."""
    exception = RuntimeError("Agent failed")
    agent_id = AgentId.generate()

    error_info = AgentErrorInfo.build_for_agent(exception, agent_id)

    assert error_info.exception_type == "RuntimeError"
    assert error_info.message == "Agent failed"
    assert error_info.agent_id == agent_id


def test_agent_details_to_cel_context_basic_fields() -> None:
    """Test that agent_details_to_cel_context converts basic AgentDetails fields."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["resource_type"] == "agent"
    assert context["type"] == "claude"
    assert context["name"] == "test-agent"
    assert context["host"]["name"] == "test-host"
    # Both names are exposed so CEL filters and templates can use either.
    assert context["host"]["provider"] == "local"
    assert context["host"]["provider_name"] == "local"
    assert "age" in context


def test_agent_details_to_cel_context_with_runtime() -> None:
    """Test that agent_details_to_cel_context includes runtime when available."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch=None,
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        runtime_seconds=123.45,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["runtime"] == 123.45


def test_agent_details_to_cel_context_with_activity_time() -> None:
    """Test that agent_details_to_cel_context computes idle from activity times."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    # Use a fixed past point so we can assert idle is computed from the
    # activity time (not from create_time or some other reference). A bug
    # computing idle with the wrong sign/reference would land outside this band.
    activity_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="feature/custom-branch",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        user_activity_time=activity_time,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    # Idle should be ~120 seconds, derived from user_activity_time.
    assert "idle" in context
    assert context["idle"] == pytest.approx(120, abs=10)


def test_agent_details_to_cel_context_with_state() -> None:
    """Test that agent_details_to_cel_context flattens state enum to lowercase string."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.STOPPED,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["state"] == AgentLifecycleState.STOPPED.value


def _make_filter_test_agent(
    *,
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    provider_name: str = "local",
    host_state: HostState | None = None,
    resource: HostResources | None = None,
    uptime_seconds: float | None = None,
    is_locked: bool | None = None,
    locked_time: datetime | None = None,
    tags: dict[str, str] | None = None,
    idle_mode: str | None = None,
    idle_seconds: float | None = None,
) -> AgentDetails:
    """Build an AgentDetails for the CEL-filter parametrize cases.

    Only the fields actually exercised by a filter expression are configurable;
    everything else uses a fixed default so the cases differ by exactly one input.
    """
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName(provider_name),
        state=host_state,
        resource=resource,
        uptime_seconds=uptime_seconds,
        is_locked=is_locked,
        locked_time=locked_time,
        tags=tags or {},
    )
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=state,
        idle_mode=idle_mode,
        idle_seconds=idle_seconds,
        host=host_details,
    )


@pytest.mark.parametrize(
    ("agent", "include_filter", "exclude_filter", "expected"),
    [
        # include matches / does not match.
        (_make_filter_test_agent(name="my-agent"), 'name == "my-agent"', None, True),
        (_make_filter_test_agent(name="other-agent"), 'name == "my-agent"', None, False),
        # exclude matches / does not match (the does-not-match -> True case was previously missing).
        (_make_filter_test_agent(name="excluded-agent"), None, 'name == "excluded-agent"', False),
        (_make_filter_test_agent(name="kept-agent"), None, 'name == "excluded-agent"', True),
        # include + exclude together: include matches but exclude also matches -> excluded
        # (this combination was previously untested).
        (_make_filter_test_agent(name="my-agent"), 'name == "my-agent"', 'host.provider == "local"', False),
        # agent-level state.
        (_make_filter_test_agent(), f'state == "{AgentLifecycleState.RUNNING.value}"', None, True),
        # host provider exposed under both names.
        (_make_filter_test_agent(), 'host.provider == "local"', None, True),
        (_make_filter_test_agent(), 'host.provider_name == "local"', None, True),
        # host state.
        (
            _make_filter_test_agent(host_state=HostState.RUNNING),
            f'host.state == "{HostState.RUNNING.value}"',
            None,
            True,
        ),
        # host resources.
        (
            _make_filter_test_agent(
                provider_name="modal",
                resource=HostResources(cpu=CpuResources(count=8), memory_gb=32.0, disk_gb=500.0),
            ),
            "host.resource.memory_gb >= 16",
            None,
            True,
        ),
        # host lock flag.
        (
            _make_filter_test_agent(is_locked=True, locked_time=datetime.now(timezone.utc)),
            "host.is_locked == true",
            None,
            True,
        ),
        # host uptime.
        (_make_filter_test_agent(uptime_seconds=100000.0), "host.uptime_seconds > 86400", None, True),
        # host tags (schemaless dict).
        (
            _make_filter_test_agent(provider_name="modal", tags={"env": "production", "team": "ml"}),
            'host.tags.env == "production"',
            None,
            True,
        ),
        # agent idle_mode / idle_seconds.
        (_make_filter_test_agent(idle_mode=IdleMode.USER.value), f'idle_mode == "{IdleMode.USER.value}"', None, True),
        (_make_filter_test_agent(idle_seconds=600.0), "idle_seconds > 300", None, True),
    ],
)
def test_apply_cel_filters_single_expression(
    agent: AgentDetails,
    include_filter: str | None,
    exclude_filter: str | None,
    expected: bool,
) -> None:
    """_apply_cel_filters returns the expected include/exclude decision for one CEL expression.

    Consolidates the previously-duplicated single-field-delta filter tests, and adds the
    exclude-does-not-match -> True and include+exclude-both-match -> False combinations.
    """
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(include_filter,) if include_filter is not None else (),
        exclude_filters=(exclude_filter,) if exclude_filter is not None else (),
    )

    result = _apply_cel_filters(agent, include_filters, exclude_filters)

    assert result is expected


def test_list_agents_returns_empty_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that list_agents returns empty result when no agents exist."""
    result = list_agents(
        mngr_ctx=temp_mngr_ctx,
        is_streaming=False,
    )

    assert result.agents == []
    assert result.errors == []


@pytest.mark.tmux
def test_list_agents_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents returns agents that exist."""
    agent_name = f"test-list-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 847291"
        )

        result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)

        assert len(result.agents) >= 1
        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in agent_names


@pytest.mark.tmux
def test_list_agents_with_include_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents applies include filters correctly."""
    agent_name = f"test-filter-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 938274"
        )

        result = list_agents(
            mngr_ctx=temp_mngr_ctx,
            include_filters=(f'name == "{agent_name}"',),
            is_streaming=False,
        )

        assert len(result.agents) == 1
        assert result.agents[0].name == AgentName(agent_name)


@pytest.mark.tmux
def test_list_agents_with_exclude_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents applies exclude filters correctly."""
    agent_name = f"test-exclude-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 726485"
        )

        result = list_agents(
            mngr_ctx=temp_mngr_ctx,
            exclude_filters=(f'name == "{agent_name}"',),
            is_streaming=False,
        )

        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) not in agent_names


@pytest.mark.tmux
def test_list_agents_with_callbacks(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents calls on_agent callback for each agent."""
    agent_name = f"test-callback-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    agents_received: list[AgentDetails] = []

    def on_agent(agent: AgentDetails) -> None:
        agents_received.append(agent)

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 619274"
        )

        result = list_agents(
            mngr_ctx=temp_mngr_ctx,
            on_agent=on_agent,
            is_streaming=False,
        )

        # The created agent must surface both in the result and via the callback.
        result_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in result_names

        assert len(agents_received) == len(result.agents)
        assert len(agents_received) >= 1
        received_names = [a.name for a in agents_received]
        assert AgentName(agent_name) in received_names


# NOTE: the CONTINUE-error-behavior paths (a failing provider recorded as a
# ProviderErrorInfo rather than raising) are covered behaviorally in the unit
# module list_test.py via injected failing providers
# (test_list_agents_batch_continue_mode_records_failing_provider_error and the
# streaming counterpart). A bare "doesn't raise on an empty ctx" test here would
# never exercise the CONTINUE branch, so it is intentionally omitted.


# =============================================================================
# Extended HostDetails Field Tests
# =============================================================================


def test_agent_details_to_cel_context_with_host_state() -> None:
    """Test that agent_details_to_cel_context includes host.state field."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        state=HostState.RUNNING,
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["host"]["state"] == HostState.RUNNING.value


def test_agent_details_to_cel_context_with_host_resources() -> None:
    """Test that agent_details_to_cel_context includes host.resource fields."""
    resources = HostResources(cpu=CpuResources(count=4), memory_gb=16.0, disk_gb=100.0)
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
        resource=resources,
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["host"]["resource"]["memory_gb"] == 16.0
    assert context["host"]["resource"]["disk_gb"] == 100.0


def test_agent_details_to_cel_context_with_host_ssh() -> None:
    """Test that agent_details_to_cel_context includes host.ssh fields."""
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/keys/id_rsa"),
        command="ssh -i /keys/id_rsa -p 22 root@example.com",
    )
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("docker"),
        ssh=ssh_info,
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["host"]["ssh"]["user"] == "root"
    assert context["host"]["ssh"]["host"] == "example.com"
    assert context["host"]["ssh"]["port"] == 22


def test_agent_details_to_cel_context_with_host_lock_fields() -> None:
    """Test that agent_details_to_cel_context includes host.is_locked and host.locked_time fields."""
    lock_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        is_locked=True,
        locked_time=lock_time,
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["host"]["is_locked"] is True
    assert context["host"]["locked_time"] is not None


def test_agent_details_to_cel_context_with_host_not_locked() -> None:
    """Test that agent_details_to_cel_context includes is_locked=False when no lock file exists."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        is_locked=False,
        locked_time=None,
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["host"]["is_locked"] is False
    assert context["host"]["locked_time"] is None


# =============================================================================
# Idle Mode and Idle Seconds Tests
# =============================================================================


def test_agent_details_to_cel_context_with_idle_mode() -> None:
    """Test that agent_details_to_cel_context includes idle_mode field."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_mode=IdleMode.AGENT.value,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["idle_mode"] == IdleMode.AGENT.value


def test_agent_details_to_cel_context_with_idle_seconds() -> None:
    """Test that agent_details_to_cel_context includes idle_seconds field."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_details = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        initial_branch="mngr/test-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_seconds=300.5,
        host=host_details,
    )

    context = agent_details_to_cel_context(agent_details)

    assert context["idle_seconds"] == 300.5


@pytest.mark.tmux
def test_list_agents_populates_idle_mode(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents populates idle_mode from the host's activity config."""
    agent_name = f"test-idle-mode-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 123456"
        )

        result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)

        our_agent = next((a for a in result.agents if a.name == AgentName(agent_name)), None)
        assert our_agent is not None, f"Agent {agent_name} not found in list"

        assert our_agent.idle_mode is not None
        assert our_agent.idle_mode == IdleMode.IO.value


@pytest.mark.tmux
def test_list_agents_reports_unlocked_host_when_no_lock_is_held(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """list_agents reports is_locked=False for an online host while no lock is held.

    This only exercises the unlocked path. Asserting the locked path
    (is_locked=True, locked_time populated) is not feasible here: for local
    hosts, Host.is_lock_held() probes the lock via a non-blocking flock, and
    flock is process-scoped -- a lock acquired by this same process is not seen
    as "held" by a re-probe in the same process. Proving the locked path would
    require a second OS process to hold the flock, which is too flaky for a
    tmux integration test. The locked-path field mapping is instead covered at
    the pure-context layer by test_agent_details_to_cel_context_with_host_lock_fields.
    """
    agent_name = f"test-lock-fields-{get_short_random_string()}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 847292"
        )

        result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)

        our_agent = next((a for a in result.agents if a.name == AgentName(agent_name)), None)
        assert our_agent is not None, f"Agent {agent_name} not found in list"

        # No lock is held during list, so is_lock_held() must report False even
        # though the lock file may persist on disk after a prior flock release.
        assert our_agent.host.is_locked is False


@pytest.mark.tmux
def test_list_agents_streaming_with_callback(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents with is_streaming=True delivers agents via on_agent callback."""
    agent_name = f"test-stream-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    agents_received: list[AgentDetails] = []

    def on_agent(agent: AgentDetails) -> None:
        agents_received.append(agent)

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 519283"
        )

        result = list_agents(
            mngr_ctx=temp_mngr_ctx,
            on_agent=on_agent,
            is_streaming=True,
        )

        assert len(agents_received) >= 1
        assert len(agents_received) == len(result.agents)

        agent_names = [a.name for a in agents_received]
        assert AgentName(agent_name) in agent_names

        result_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in result_names


def test_list_agents_streaming_returns_empty_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that streaming list_agents returns empty result when no agents exist."""
    agents_received: list[AgentDetails] = []

    def on_agent(agent: AgentDetails) -> None:
        agents_received.append(agent)

    result = list_agents(
        mngr_ctx=temp_mngr_ctx,
        on_agent=on_agent,
        is_streaming=True,
    )

    assert result.agents == []
    assert result.errors == []
    assert len(agents_received) == 0


# NOTE: streaming CONTINUE-mode error recording is covered behaviorally in
# list_test.py (test_list_agents_streaming_continue_mode_records_failing_provider_error).


@pytest.mark.tmux
def test_list_agents_with_provider_names_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents filters by provider_names."""
    agent_name = f"test-provider-filter-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 234567"
        )

        result = list_agents(mngr_ctx=temp_mngr_ctx, provider_names=("local",), is_streaming=False, reset_caches=True)

        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in agent_names

        # reset_caches=True is required when calling list_agents a second time in
        # the same process: without it the negative assertion below could pass
        # because of cache bleed rather than the provider_names filter.
        result_empty = list_agents(
            mngr_ctx=temp_mngr_ctx, provider_names=("nonexistent",), is_streaming=False, reset_caches=True
        )

        assert len(result_empty.agents) == 0
