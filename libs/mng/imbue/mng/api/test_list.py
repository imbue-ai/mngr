"""Integration tests for the list API module."""

import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.list import AgentErrorInfo
from imbue.mng.api.list import ErrorInfo
from imbue.mng.api.list import HostErrorInfo
from imbue.mng.api.list import ListResult
from imbue.mng.api.list import ProviderErrorInfo
from imbue.mng.api.list import _agent_to_cel_context
from imbue.mng.api.list import _apply_cel_filters
from imbue.mng.api.list import list_agents
from imbue.mng.cli.create import create
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import CpuResources
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import SSHInfo
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostState
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.cel_utils import compile_cel_filters
from imbue.mng.utils.testing import tmux_session_cleanup

# =============================================================================
# Error Info Tests
# =============================================================================


def test_error_info_build_creates_error_info() -> None:
    """Test that ErrorInfo.build creates an error info from an exception."""
    exception = RuntimeError("Test error message")

    error_info = ErrorInfo.build(exception)

    assert error_info.exception_type == "RuntimeError"
    assert error_info.message == "Test error message"


def test_error_info_build_handles_mng_error() -> None:
    """Test that ErrorInfo.build handles MngError subclasses."""

    class CustomMngError(MngError):
        """Custom test error."""

    exception = CustomMngError("Custom error")

    error_info = ErrorInfo.build(exception)

    assert error_info.exception_type == "CustomMngError"
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


def test_list_result_defaults_to_empty_lists() -> None:
    """Test that ListResult defaults to empty lists."""
    result = ListResult()

    assert result.agents == []
    assert result.errors == []


def test_agent_to_cel_context_basic_fields() -> None:
    """Test that _agent_to_cel_context converts basic AgentInfo fields."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["resource_type"] == "agent"
    assert context["type"] == "claude"
    assert context["name"] == "test-agent"
    assert context["host"]["name"] == "test-host"
    assert context["host"]["provider"] == "local"
    assert "age" in context


def test_agent_to_cel_context_with_runtime() -> None:
    """Test that _agent_to_cel_context includes runtime when available."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        runtime_seconds=123.45,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["runtime"] == 123.45


def test_agent_to_cel_context_with_activity_time() -> None:
    """Test that _agent_to_cel_context computes idle from activity times."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    activity_time = datetime.now(timezone.utc)
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        user_activity_time=activity_time,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    # Idle should be computed and be very small (just computed)
    assert "idle" in context
    assert context["idle"] >= 0


def test_agent_to_cel_context_with_state() -> None:
    """Test that _agent_to_cel_context flattens state enum to lowercase string."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.STOPPED,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["state"] == AgentLifecycleState.STOPPED.value


def test_apply_cel_filters_with_include_filter() -> None:
    """Test that _apply_cel_filters includes matching agents."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("my-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('name == "my-agent"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_non_matching_include() -> None:
    """Test that _apply_cel_filters excludes non-matching agents."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("other-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('name == "my-agent"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is False


def test_apply_cel_filters_with_exclude_filter() -> None:
    """Test that _apply_cel_filters excludes matching agents."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("excluded-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(),
        exclude_filters=('name == "excluded-agent"',),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is False


def test_apply_cel_filters_with_state_filter() -> None:
    """Test filtering by lifecycle state."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(f'state == "{AgentLifecycleState.RUNNING.value}"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_host_provider_filter() -> None:
    """Test filtering by host provider using dot notation."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('host.provider == "local"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_list_agents_returns_empty_when_no_agents(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that list_agents returns empty result when no agents exist."""
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=False,
    )

    assert result.agents == []
    assert result.errors == []


@pytest.mark.tmux
def test_list_agents_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents returns agents that exist."""
    agent_name = f"test-list-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent first
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847291",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        # Now list agents
        result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)

        assert len(result.agents) >= 1
        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in agent_names


@pytest.mark.tmux
def test_list_agents_with_include_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents applies include filters correctly."""
    agent_name = f"test-filter-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 938274",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0

        # List with filter that matches
        result = list_agents(
            mng_ctx=temp_mng_ctx,
            include_filters=(f'name == "{agent_name}"',),
            is_streaming=False,
        )

        assert len(result.agents) == 1
        assert result.agents[0].name == AgentName(agent_name)


@pytest.mark.tmux
def test_list_agents_with_exclude_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents applies exclude filters correctly."""
    agent_name = f"test-exclude-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 726485",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0

        # List with filter that excludes the agent
        result = list_agents(
            mng_ctx=temp_mng_ctx,
            exclude_filters=(f'name == "{agent_name}"',),
            is_streaming=False,
        )

        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) not in agent_names


@pytest.mark.tmux
def test_list_agents_with_callbacks(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents calls on_agent callback for each agent."""
    agent_name = f"test-callback-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    agents_received: list[AgentInfo] = []

    def on_agent(agent: AgentInfo) -> None:
        agents_received.append(agent)

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 619274",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0

        # List with callback
        result = list_agents(
            mng_ctx=temp_mng_ctx,
            on_agent=on_agent,
            is_streaming=False,
        )

        # Callback should have been called for each agent
        assert len(agents_received) == len(result.agents)
        if result.agents:
            assert agents_received[0].name == result.agents[0].name


def test_list_agents_with_error_behavior_continue(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that list_agents with CONTINUE error behavior doesn't raise."""
    # This should not raise even if there are issues
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        error_behavior=ErrorBehavior.CONTINUE,
        is_streaming=False,
    )

    # Should return a result, possibly empty
    assert isinstance(result, ListResult)


# =============================================================================
# Extended HostInfo Field Tests
# =============================================================================


def test_agent_to_cel_context_with_host_state() -> None:
    """Test that _agent_to_cel_context includes host.state field."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        state=HostState.RUNNING,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["host"]["state"] == HostState.RUNNING.value


def test_agent_to_cel_context_with_host_resources() -> None:
    """Test that _agent_to_cel_context includes host.resource fields."""
    resources = HostResources(cpu=CpuResources(count=4), memory_gb=16.0, disk_gb=100.0)
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
        resource=resources,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["host"]["resource"]["memory_gb"] == 16.0
    assert context["host"]["resource"]["disk_gb"] == 100.0


def test_agent_to_cel_context_with_host_ssh() -> None:
    """Test that _agent_to_cel_context includes host.ssh fields."""
    ssh_info = SSHInfo(
        user="root",
        host="example.com",
        port=22,
        key_path=Path("/keys/id_rsa"),
        command="ssh -i /keys/id_rsa -p 22 root@example.com",
    )
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("docker"),
        ssh=ssh_info,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["host"]["ssh"]["user"] == "root"
    assert context["host"]["ssh"]["host"] == "example.com"
    assert context["host"]["ssh"]["port"] == 22


def test_apply_cel_filters_with_host_state_filter() -> None:
    """Test filtering by host.state."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        state=HostState.RUNNING,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(f'host.state == "{HostState.RUNNING.value}"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_host_resource_filter() -> None:
    """Test filtering by host.resource.memory_gb."""
    resources = HostResources(cpu=CpuResources(count=8), memory_gb=32.0, disk_gb=500.0)
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
        resource=resources,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=("host.resource.memory_gb >= 16",),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_agent_to_cel_context_with_host_lock_fields() -> None:
    """Test that _agent_to_cel_context includes host.is_locked and host.locked_time fields."""
    lock_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        is_locked=True,
        locked_time=lock_time,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["host"]["is_locked"] is True
    assert context["host"]["locked_time"] is not None


def test_agent_to_cel_context_with_host_not_locked() -> None:
    """Test that _agent_to_cel_context includes is_locked=False when no lock file exists."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        is_locked=False,
        locked_time=None,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["host"]["is_locked"] is False
    assert context["host"]["locked_time"] is None


def test_apply_cel_filters_with_host_is_locked_filter() -> None:
    """Test filtering by host.is_locked."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        is_locked=True,
        locked_time=datetime.now(timezone.utc),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=("host.is_locked == true",),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_host_uptime_filter() -> None:
    """Test filtering by host.uptime_seconds."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        # More than a day (86400 seconds)
        uptime_seconds=100000.0,
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    # Filter for hosts running more than a day (86400 seconds)
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=("host.uptime_seconds > 86400",),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_host_tags_filter() -> None:
    """Test filtering by host.tags."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
        tags={"env": "production", "team": "ml"},
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('host.tags.env == "production"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


# =============================================================================
# Idle Mode and Idle Seconds Tests
# =============================================================================


def test_agent_to_cel_context_with_idle_mode() -> None:
    """Test that _agent_to_cel_context includes idle_mode field."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_mode=IdleMode.AGENT.value,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["idle_mode"] == IdleMode.AGENT.value


def test_agent_to_cel_context_with_idle_seconds() -> None:
    """Test that _agent_to_cel_context includes idle_seconds field."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_seconds=300.5,
        host=host_info,
    )

    context = _agent_to_cel_context(agent_info)

    assert context["idle_seconds"] == 300.5


def test_apply_cel_filters_with_idle_mode_filter() -> None:
    """Test filtering by idle_mode."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_mode=IdleMode.USER.value,
        host=host_info,
    )

    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(f'idle_mode == "{IdleMode.USER.value}"',),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


def test_apply_cel_filters_with_idle_seconds_filter() -> None:
    """Test filtering by idle_seconds."""
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )
    agent_info = AgentInfo(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work/dir"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        idle_seconds=600.0,
        host=host_info,
    )

    # Filter for agents idle more than 5 minutes (300 seconds)
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=("idle_seconds > 300",),
        exclude_filters=(),
    )

    result = _apply_cel_filters(agent_info, include_filters, exclude_filters)

    assert result is True


@pytest.mark.tmux
def test_list_agents_populates_idle_mode(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents populates idle_mode from the host's activity config."""
    agent_name = f"test-idle-mode-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 123456",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        # List agents and check idle_mode is populated
        result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)

        # Find our agent
        our_agent = next((a for a in result.agents if a.name == AgentName(agent_name)), None)
        assert our_agent is not None, f"Agent {agent_name} not found in list"

        # idle_mode should be populated (default is "agent")
        assert our_agent.idle_mode is not None
        assert our_agent.idle_mode == IdleMode.IO.value


@pytest.mark.tmux
def test_list_agents_populates_lock_fields_for_online_host(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents populates is_locked and locked_time for online hosts."""
    agent_name = f"test-lock-fields-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847292",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)

        our_agent = next((a for a in result.agents if a.name == AgentName(agent_name)), None)
        assert our_agent is not None, f"Agent {agent_name} not found in list"

        # No lock is held during list, so is_locked should be False.
        # locked_time may be non-None since the lock file persists on local hosts
        # after flock release, but is_lock_held() correctly detects the lock is not active.
        assert our_agent.host.is_locked is False


@pytest.mark.tmux
def test_list_agents_streaming_with_callback(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents with is_streaming=True delivers agents via on_agent callback."""
    agent_name = f"test-stream-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    agents_received: list[AgentInfo] = []

    def on_agent(agent: AgentInfo) -> None:
        agents_received.append(agent)

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 519283",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        # List with streaming mode and callback
        result = list_agents(
            mng_ctx=temp_mng_ctx,
            on_agent=on_agent,
            is_streaming=True,
        )

        # Callback should have been called for each agent
        assert len(agents_received) >= 1
        assert len(agents_received) == len(result.agents)

        # The agent we created should be in the results
        agent_names = [a.name for a in agents_received]
        assert AgentName(agent_name) in agent_names

        # Result object should also be populated
        result_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in result_names


def test_list_agents_streaming_returns_empty_when_no_agents(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that streaming list_agents returns empty result when no agents exist."""
    agents_received: list[AgentInfo] = []

    def on_agent(agent: AgentInfo) -> None:
        agents_received.append(agent)

    result = list_agents(
        mng_ctx=temp_mng_ctx,
        on_agent=on_agent,
        is_streaming=True,
    )

    assert result.agents == []
    assert result.errors == []
    assert len(agents_received) == 0


def test_list_agents_streaming_with_error_behavior_continue(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that streaming list_agents with CONTINUE error behavior doesn't raise."""
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        error_behavior=ErrorBehavior.CONTINUE,
        is_streaming=True,
    )

    assert isinstance(result, ListResult)


@pytest.mark.tmux
def test_list_agents_with_provider_names_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that list_agents filters by provider_names."""
    agent_name = f"test-provider-filter-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent on the local provider
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 234567",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        # List agents filtering to local provider - should find the agent
        result = list_agents(mng_ctx=temp_mng_ctx, provider_names=("local",), is_streaming=False)

        agent_names = [a.name for a in result.agents]
        assert AgentName(agent_name) in agent_names

        # List agents filtering to nonexistent provider - should not find any agents
        result_empty = list_agents(mng_ctx=temp_mng_ctx, provider_names=("nonexistent",), is_streaming=False)

        assert len(result_empty.agents) == 0
