from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from io import StringIO
from pathlib import Path
from typing import Any

import pluggy
import pytest
from loguru import logger

from imbue.mng import hookimpl
from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.discover import warn_on_duplicate_host_names
from imbue.mng.api.discovery_events import get_discovery_events_path
from imbue.mng.api.list import AgentErrorInfo
from imbue.mng.api.list import ErrorInfo
from imbue.mng.api.list import HostErrorInfo
from imbue.mng.api.list import ListResult
from imbue.mng.api.list import ProviderErrorInfo
from imbue.mng.api.list import _apply_cel_filters
from imbue.mng.api.list import _maybe_write_full_discovery_snapshot
from imbue.mng.api.list import agent_details_to_cel_context
from imbue.mng.api.list import list_agents
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.data_types import HostDetails
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.cel_utils import compile_cel_filters

# =============================================================================
# Helpers
# =============================================================================


def _make_host_details() -> HostDetails:
    return HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
    )


def _make_agent_details(name: str, host_details: HostDetails) -> AgentDetails:
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        initial_branch=f"mng/{name}",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )


# =============================================================================
# Duplicate Host Name Warning Tests
# =============================================================================


def _make_discovered_host(
    host_name: str,
    provider_name: str = "modal",
) -> DiscoveredHost:
    return DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName(host_name),
        provider_name=ProviderInstanceName(provider_name),
    )


def _make_discovered_agent(host_id: HostId, provider_name: str = "modal") -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName(provider_name),
    )


@contextmanager
def _capture_loguru_warnings() -> Iterator[StringIO]:
    """Capture loguru WARNING-level output into a StringIO buffer."""
    log_output = StringIO()
    sink_id = logger.add(log_output, level="WARNING", format="{message}")
    try:
        yield log_output
    finally:
        logger.remove(sink_id)


def test_warn_on_duplicate_host_names_no_warning_for_unique_names() -> None:
    """warn_on_duplicate_host_names should not warn when all host names are unique."""
    ref_alpha = _make_discovered_host("host-alpha")
    ref_beta = _make_discovered_host("host-beta")
    ref_gamma = _make_discovered_host("host-gamma")
    agents_by_host = {
        ref_alpha: [_make_discovered_agent(ref_alpha.host_id)],
        ref_beta: [_make_discovered_agent(ref_beta.host_id)],
        ref_gamma: [_make_discovered_agent(ref_gamma.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_warns_on_duplicate_within_same_provider() -> None:
    """warn_on_duplicate_host_names should warn when the same name appears twice on the same provider."""
    ref_dup_1 = _make_discovered_host("duplicated-name", "modal")
    ref_dup_2 = _make_discovered_host("duplicated-name", "modal")
    ref_unique = _make_discovered_host("unique-name", "modal")
    agents_by_host = {
        ref_dup_1: [_make_discovered_agent(ref_dup_1.host_id)],
        ref_dup_2: [_make_discovered_agent(ref_dup_2.host_id)],
        ref_unique: [_make_discovered_agent(ref_unique.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        warn_on_duplicate_host_names(agents_by_host)

    output = log_output.getvalue()
    assert "Duplicate host name" in output
    assert "duplicated-name" in output
    assert "modal" in output


def test_warn_on_duplicate_host_names_no_warning_for_same_name_on_different_providers() -> None:
    """warn_on_duplicate_host_names should not warn when the same name exists on different providers."""
    ref_modal = _make_discovered_host("shared-name", "modal")
    ref_docker = _make_discovered_host("shared-name", "docker")
    agents_by_host = {
        ref_modal: [_make_discovered_agent(ref_modal.host_id, "modal")],
        ref_docker: [_make_discovered_agent(ref_docker.host_id, "docker")],
    }

    with _capture_loguru_warnings() as log_output:
        warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_empty_input() -> None:
    """warn_on_duplicate_host_names should not warn with an empty input."""
    with _capture_loguru_warnings() as log_output:
        warn_on_duplicate_host_names({})

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_no_warning_when_destroyed_host_shares_name() -> None:
    """warn_on_duplicate_host_names should not warn when a destroyed host (no agents) shares a name with an active host."""
    ref_destroyed = _make_discovered_host("reused-name", "modal")
    ref_active = _make_discovered_host("reused-name", "modal")
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {
        ref_destroyed: [],
        ref_active: [_make_discovered_agent(ref_active.host_id)],
    }

    with _capture_loguru_warnings() as log_output:
        warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


# =============================================================================
# ErrorInfo Tests
# =============================================================================


def test_error_info_build_creates_correct_error_from_exception() -> None:
    """ErrorInfo.build() should capture the exception type and message."""
    exception = RuntimeError("something went wrong")
    error = ErrorInfo.build(exception)
    assert error.exception_type == "RuntimeError"
    assert error.message == "something went wrong"


def test_error_info_build_captures_custom_exception_type() -> None:
    """ErrorInfo.build() should capture custom exception class names."""
    exception = ValueError("bad value")
    error = ErrorInfo.build(exception)
    assert error.exception_type == "ValueError"
    assert error.message == "bad value"


# =============================================================================
# ProviderErrorInfo Tests
# =============================================================================


def test_provider_error_info_build_for_provider() -> None:
    """ProviderErrorInfo.build_for_provider() should include the provider name."""
    exception = ConnectionError("cannot connect")
    provider_name = ProviderInstanceName("my-provider")
    error = ProviderErrorInfo.build_for_provider(exception, provider_name)
    assert error.exception_type == "ConnectionError"
    assert error.message == "cannot connect"
    assert error.provider_name == provider_name


# =============================================================================
# HostErrorInfo Tests
# =============================================================================


def test_host_error_info_build_for_host() -> None:
    """HostErrorInfo.build_for_host() should include the host ID."""
    exception = TimeoutError("host unreachable")
    host_id = HostId.generate()
    error = HostErrorInfo.build_for_host(exception, host_id)
    assert error.exception_type == "TimeoutError"
    assert error.message == "host unreachable"
    assert error.host_id == host_id


# =============================================================================
# AgentErrorInfo Tests
# =============================================================================


def test_agent_error_info_build_for_agent() -> None:
    """AgentErrorInfo.build_for_agent() should include the agent ID."""
    exception = OSError("agent process died")
    agent_id = AgentId.generate()
    error = AgentErrorInfo.build_for_agent(exception, agent_id)
    assert error.exception_type == "OSError"
    assert error.message == "agent process died"
    assert error.agent_id == agent_id


# =============================================================================
# ListResult Tests
# =============================================================================


def test_list_result_initializes_with_empty_lists() -> None:
    """ListResult should initialize with empty agents and errors lists."""
    result = ListResult()
    assert result.agents == []
    assert result.errors == []


def test_list_result_allows_appending() -> None:
    """ListResult agents and errors lists should be mutable."""
    result = ListResult()
    host_details = _make_host_details()
    agent = _make_agent_details("test-agent", host_details)
    result.agents.append(agent)
    assert len(result.agents) == 1
    assert result.agents[0].name == AgentName("test-agent")

    error = ErrorInfo.build(RuntimeError("oops"))
    result.errors.append(error)
    assert len(result.errors) == 1


# =============================================================================
# agent_details_to_cel_context Tests
# =============================================================================


def test_agent_details_to_cel_context_basic_fields() -> None:
    """agent_details_to_cel_context should convert AgentDetails to a dict with basic fields."""
    host_details = _make_host_details()
    agent = _make_agent_details("my-agent", host_details)
    context = agent_details_to_cel_context(agent)

    assert context["name"] == "my-agent"
    assert context["type"] == "claude"
    assert context["state"] == "RUNNING"
    assert context["command"] == "sleep 100"


def test_agent_details_to_cel_context_computes_age() -> None:
    """agent_details_to_cel_context should compute 'age' from create_time."""
    host_details = _make_host_details()
    create_time = datetime.now(timezone.utc) - timedelta(hours=2)
    agent = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("aging-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        initial_branch=None,
        create_time=create_time,
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=host_details,
    )
    context = agent_details_to_cel_context(agent)

    assert "age" in context
    # Age should be approximately 7200 seconds (2 hours), with some tolerance
    assert context["age"] > 7000
    assert context["age"] < 7400


def test_agent_details_to_cel_context_computes_runtime() -> None:
    """agent_details_to_cel_context should set 'runtime' from runtime_seconds."""
    host_details = _make_host_details()
    agent = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("running-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        initial_branch="feature/custom-work",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        runtime_seconds=3600.0,
        host=host_details,
    )
    context = agent_details_to_cel_context(agent)

    assert context["runtime"] == 3600.0


def test_agent_details_to_cel_context_computes_idle() -> None:
    """agent_details_to_cel_context should compute 'idle' from activity times."""
    host_details = _make_host_details()
    activity_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    agent = AgentDetails(
        id=AgentId.generate(),
        name=AgentName("idle-agent"),
        type="claude",
        command=CommandString("sleep 100"),
        work_dir=Path("/work"),
        initial_branch="mng/idle-agent",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        user_activity_time=activity_time,
        host=host_details,
    )
    context = agent_details_to_cel_context(agent)

    assert "idle" in context
    # Idle should be approximately 300 seconds (5 minutes)
    assert context["idle"] > 280
    assert context["idle"] < 320


def test_agent_details_to_cel_context_normalizes_host_provider() -> None:
    """agent_details_to_cel_context should rename host.provider_name to host.provider."""
    host_details = HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("modal"),
    )
    agent = _make_agent_details("test-agent", host_details)
    context = agent_details_to_cel_context(agent)

    assert "host" in context
    host = context["host"]
    assert "provider" in host
    assert "provider_name" not in host
    assert host["provider"] == "modal"


# =============================================================================
# _apply_cel_filters Tests
# =============================================================================


def test_apply_cel_filters_includes_matching_agent() -> None:
    """_apply_cel_filters should return True when agent matches include filter."""
    host_details = _make_host_details()
    agent = _make_agent_details("target-agent", host_details)
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('name == "target-agent"',),
        exclude_filters=(),
    )
    assert _apply_cel_filters(agent, include_filters, exclude_filters) is True


def test_apply_cel_filters_excludes_non_matching_agent() -> None:
    """_apply_cel_filters should return False when agent does not match include filter."""
    host_details = _make_host_details()
    agent = _make_agent_details("other-agent", host_details)
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=('name == "target-agent"',),
        exclude_filters=(),
    )
    assert _apply_cel_filters(agent, include_filters, exclude_filters) is False


def test_apply_cel_filters_exclude_filter_removes_agent() -> None:
    """_apply_cel_filters should return False when agent matches exclude filter."""
    host_details = _make_host_details()
    agent = _make_agent_details("unwanted-agent", host_details)
    include_filters, exclude_filters = compile_cel_filters(
        include_filters=(),
        exclude_filters=('name == "unwanted-agent"',),
    )
    assert _apply_cel_filters(agent, include_filters, exclude_filters) is False


def test_apply_cel_filters_no_filters_includes_all() -> None:
    """_apply_cel_filters should return True when no filters are provided."""
    host_details = _make_host_details()
    agent = _make_agent_details("any-agent", host_details)
    assert _apply_cel_filters(agent, [], []) is True


# =============================================================================
# _maybe_write_full_discovery_snapshot Tests
# =============================================================================


def test_maybe_write_full_discovery_snapshot_writes_when_unfiltered_and_error_free(
    temp_mng_ctx: MngContext,
) -> None:
    """_maybe_write_full_discovery_snapshot writes a snapshot when the listing is complete and error-free."""
    host_details = _make_host_details()
    agent = _make_agent_details("snapshot-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mng_ctx=temp_mng_ctx,
        result=result,
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mng_ctx.config)
    assert events_path.exists()
    content = events_path.read_text()
    assert "DISCOVERY_FULL" in content
    assert "snapshot-agent" in content


def test_maybe_write_full_discovery_snapshot_skips_when_errors_present(
    temp_mng_ctx: MngContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when errors are present."""
    host_details = _make_host_details()
    agent = _make_agent_details("error-agent", host_details)
    result = ListResult()
    result.agents.append(agent)
    result.errors.append(ErrorInfo.build(RuntimeError("provider failed")))

    _maybe_write_full_discovery_snapshot(
        mng_ctx=temp_mng_ctx,
        result=result,
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mng_ctx.config)
    assert not events_path.exists()


def test_maybe_write_full_discovery_snapshot_skips_when_provider_filter_set(
    temp_mng_ctx: MngContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when provider_names is set."""
    host_details = _make_host_details()
    agent = _make_agent_details("filtered-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mng_ctx=temp_mng_ctx,
        result=result,
        provider_names=("local",),
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mng_ctx.config)
    assert not events_path.exists()


def test_maybe_write_full_discovery_snapshot_skips_when_include_filters_set(
    temp_mng_ctx: MngContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when include_filters are set."""
    host_details = _make_host_details()
    agent = _make_agent_details("include-filtered-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mng_ctx=temp_mng_ctx,
        result=result,
        provider_names=None,
        include_filters=('name == "include-filtered-agent"',),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mng_ctx.config)
    assert not events_path.exists()


# =============================================================================
# list_agents Tests
# =============================================================================


def test_list_agents_batch_mode_no_agents_returns_empty_result(
    temp_mng_ctx: MngContext,
) -> None:
    """list_agents in batch mode (is_streaming=False) should return empty result when no agents exist."""
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=False,
    )
    assert result.agents == []
    assert result.errors == []


def test_list_agents_streaming_mode_no_agents_returns_empty_result(
    temp_mng_ctx: MngContext,
) -> None:
    """list_agents in streaming mode (is_streaming=True) should return empty result when no agents exist."""
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=True,
    )
    assert result.agents == []
    assert result.errors == []


@pytest.mark.tmux
def test_list_agents_batch_mode_on_agent_callback_is_called(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
) -> None:
    """list_agents should call on_agent for each found agent in batch mode."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("list-callback-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847300"),
        ),
    )

    local_host.start_agents([agent.id])

    found_agents: list[AgentDetails] = []
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=False,
        on_agent=lambda a: found_agents.append(a),
    )

    local_host.destroy_agent(agent)

    assert len(result.agents) >= 1
    assert len(found_agents) >= 1
    found_names = [str(a.name) for a in found_agents]
    assert "list-callback-test" in found_names


@pytest.mark.tmux
def test_list_agents_streaming_mode_on_agent_callback_is_called(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
) -> None:
    """list_agents should call on_agent for each found agent in streaming mode."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("list-stream-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847301"),
        ),
    )

    local_host.start_agents([agent.id])

    found_agents: list[AgentDetails] = []
    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=True,
        on_agent=lambda a: found_agents.append(a),
    )

    local_host.destroy_agent(agent)

    assert len(result.agents) >= 1
    assert len(found_agents) >= 1
    found_names = [str(a.name) for a in found_agents]
    assert "list-stream-test" in found_names


@pytest.mark.tmux
def test_list_agents_with_include_filter_excludes_non_matching(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
) -> None:
    """list_agents with a CEL include filter should exclude non-matching agents."""
    agent1 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("list-include-yes"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847302"),
        ),
    )
    agent2 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("list-include-no"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847303"),
        ),
    )

    local_host.start_agents([agent1.id, agent2.id])

    result = list_agents(
        mng_ctx=temp_mng_ctx,
        is_streaming=False,
        include_filters=('name == "list-include-yes"',),
    )

    local_host.destroy_agent(agent1)
    local_host.destroy_agent(agent2)

    result_names = [str(a.name) for a in result.agents]
    assert "list-include-yes" in result_names
    assert "list-include-no" not in result_names


# =============================================================================
# discover_all_hosts_and_agents Tests
# =============================================================================


def test_discover_all_hosts_and_agents_returns_empty_for_no_agents(
    temp_mng_ctx: MngContext,
) -> None:
    """discover_all_hosts_and_agents should return empty dict when no agents exist."""
    agents_by_host, providers = discover_all_hosts_and_agents(temp_mng_ctx)
    assert isinstance(agents_by_host, dict)
    assert isinstance(providers, list)
    # At least the local provider should be present
    assert len(providers) > 0


# =============================================================================
# agent_field_generators integration tests
# =============================================================================


class _FieldGeneratorPlugin:
    """Test plugin that registers field generators for agent listing."""

    def __init__(self, plugin_name: str, generators: dict[str, Any]) -> None:
        self._plugin_name = plugin_name
        self._generators = generators

    @hookimpl
    def agent_field_generators(self) -> tuple[str, dict[str, Any]] | None:
        return (self._plugin_name, self._generators)


class _NoneFieldGeneratorPlugin:
    """Test plugin that returns None from agent_field_generators."""

    @hookimpl
    def agent_field_generators(self) -> None:
        return None


def _find_agent_by_name(result: ListResult, name: str) -> AgentDetails:
    """Find an agent by name in a ListResult, or raise."""
    for agent in result.agents:
        if str(agent.name) == name:
            return agent
    found = [str(a.name) for a in result.agents]
    raise AssertionError(f"Agent {name!r} not found in results: {found}")


@pytest.mark.tmux
def test_field_generators_populate_plugin_data(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """list_agents should populate plugin data from registered field generators."""
    plugin_manager.register(
        _FieldGeneratorPlugin(
            "test_plugin",
            {
                "status": lambda a, h: "active",
                "score": lambda a, h: 42,
            },
        )
    )

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("field-gen-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847310"),
        ),
    )
    local_host.start_agents([agent.id])

    result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-test")
    assert details.plugin["test_plugin"] == {"status": "active", "score": 42}


@pytest.mark.tmux
def test_field_generators_omit_none_values(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """list_agents should omit fields where the generator returns None."""
    plugin_manager.register(
        _FieldGeneratorPlugin(
            "test_plugin",
            {
                "present": lambda a, h: "yes",
                "absent": lambda a, h: None,
            },
        )
    )

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("field-gen-none"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847311"),
        ),
    )
    local_host.start_agents([agent.id])

    result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-none")
    assert details.plugin["test_plugin"] == {"present": "yes"}


@pytest.mark.tmux
def test_field_generators_multiple_plugins(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """list_agents should collect fields from multiple plugin generators."""
    plugin_manager.register(_FieldGeneratorPlugin("plugin_a", {"version": lambda a, h: "1.0"}))
    plugin_manager.register(_FieldGeneratorPlugin("plugin_b", {"count": lambda a, h: 5}))

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("field-gen-multi"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847312"),
        ),
    )
    local_host.start_agents([agent.id])

    result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-multi")
    assert details.plugin["plugin_a"] == {"version": "1.0"}
    assert details.plugin["plugin_b"] == {"count": 5}


@pytest.mark.tmux
def test_field_generators_none_plugin_is_skipped(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """list_agents should skip plugins that return None from agent_field_generators."""
    plugin_manager.register(_NoneFieldGeneratorPlugin())
    plugin_manager.register(_FieldGeneratorPlugin("real_plugin", {"value": lambda a, h: "present"}))

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("field-gen-skip-none"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847313"),
        ),
    )
    local_host.start_agents([agent.id])

    result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-skip-none")
    assert details.plugin["real_plugin"] == {"value": "present"}
    assert "none_plugin" not in details.plugin


def test_no_field_generators_produces_empty_plugin(
    temp_mng_ctx: MngContext,
) -> None:
    """list_agents with no field generator plugins should leave plugin={} on all agents."""
    result = list_agents(mng_ctx=temp_mng_ctx, is_streaming=False)
    for agent in result.agents:
        assert agent.plugin == {}


# =============================================================================
# discover_all_hosts_and_agents Tests
# =============================================================================


@pytest.mark.tmux
def test_discover_all_hosts_and_agents_groups_agents_by_host(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_host: Host,
) -> None:
    """discover_all_hosts_and_agents should return agents grouped by their host reference."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("grouped-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847304"),
        ),
    )

    agents_by_host, providers = discover_all_hosts_and_agents(temp_mng_ctx)

    local_host.destroy_agent(agent)

    # There should be at least one host with agents
    non_empty_hosts = {k: v for k, v in agents_by_host.items() if v}
    assert len(non_empty_hosts) >= 1

    # Find our agent in the results
    found_agent = False
    for _host_ref, agent_refs in agents_by_host.items():
        for ref in agent_refs:
            if str(ref.agent_name) == "grouped-test":
                found_agent = True
                break
    assert found_agent
