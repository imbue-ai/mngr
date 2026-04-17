from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from threading import Lock
from typing import Any

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr import hookimpl
from imbue.mngr.api.discover import _all_identifiers_found
from imbue.mngr.api.discover import discover_hosts_and_agents
from imbue.mngr.api.discover import warn_on_duplicate_host_names
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.list import AgentErrorInfo
from imbue.mngr.api.list import ErrorInfo
from imbue.mngr.api.list import HostErrorInfo
from imbue.mngr.api.list import ListResult
from imbue.mngr.api.list import ProviderErrorInfo
from imbue.mngr.api.list import _ListAgentsParams
from imbue.mngr.api.list import _apply_cel_filters
from imbue.mngr.api.list import _collect_and_emit_details_for_host
from imbue.mngr.api.list import _discover_and_emit_details_for_provider
from imbue.mngr.api.list import _handle_listing_error
from imbue.mngr.api.list import _maybe_write_full_discovery_snapshot
from imbue.mngr.api.list import _process_host_with_error_handling
from imbue.mngr.api.list import agent_details_to_cel_context
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.mock_provider_test import make_offline_host
from imbue.mngr.providers.registry import _backend_registry
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.testing import capture_loguru

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
        initial_branch=f"mngr/{name}",
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

    with capture_loguru(level="WARNING") as log_output:
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

    with capture_loguru(level="WARNING") as log_output:
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

    with capture_loguru(level="WARNING") as log_output:
        warn_on_duplicate_host_names(agents_by_host)

    assert "Duplicate host name" not in log_output.getvalue()


def test_warn_on_duplicate_host_names_empty_input() -> None:
    """warn_on_duplicate_host_names should not warn with an empty input."""
    with capture_loguru(level="WARNING") as log_output:
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

    with capture_loguru(level="WARNING") as log_output:
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
        initial_branch="mngr/idle-agent",
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
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot writes a snapshot when the listing is complete and error-free."""
    host_details = _make_host_details()
    agent = _make_agent_details("snapshot-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mngr_ctx=temp_mngr_ctx,
        result=result,
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    assert events_path.exists()
    content = events_path.read_text()
    assert "DISCOVERY_FULL" in content
    assert "snapshot-agent" in content


def test_maybe_write_full_discovery_snapshot_skips_when_errors_present(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when errors are present."""
    host_details = _make_host_details()
    agent = _make_agent_details("error-agent", host_details)
    result = ListResult()
    result.agents.append(agent)
    result.errors.append(ErrorInfo.build(RuntimeError("provider failed")))

    _maybe_write_full_discovery_snapshot(
        mngr_ctx=temp_mngr_ctx,
        result=result,
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    assert not events_path.exists()


def test_maybe_write_full_discovery_snapshot_skips_when_provider_filter_set(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when provider_names is set."""
    host_details = _make_host_details()
    agent = _make_agent_details("filtered-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mngr_ctx=temp_mngr_ctx,
        result=result,
        provider_names=("local",),
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    assert not events_path.exists()


def test_maybe_write_full_discovery_snapshot_skips_when_include_filters_set(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot does not write when include_filters are set."""
    host_details = _make_host_details()
    agent = _make_agent_details("include-filtered-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mngr_ctx=temp_mngr_ctx,
        result=result,
        provider_names=None,
        include_filters=('name == "include-filtered-agent"',),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    assert not events_path.exists()


# =============================================================================
# list_agents Tests
# =============================================================================


def test_list_agents_batch_mode_no_agents_returns_empty_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents in batch mode (is_streaming=False) should return empty result when no agents exist."""
    result = list_agents(
        mngr_ctx=temp_mngr_ctx,
        is_streaming=False,
    )
    assert result.agents == []
    assert result.errors == []


def test_list_agents_streaming_mode_no_agents_returns_empty_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents in streaming mode (is_streaming=True) should return empty result when no agents exist."""
    result = list_agents(
        mngr_ctx=temp_mngr_ctx,
        is_streaming=True,
    )
    assert result.agents == []
    assert result.errors == []


@pytest.mark.tmux
def test_list_agents_batch_mode_on_agent_callback_is_called(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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
        mngr_ctx=temp_mngr_ctx,
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
    temp_mngr_ctx: MngrContext,
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
        mngr_ctx=temp_mngr_ctx,
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
    temp_mngr_ctx: MngrContext,
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
        mngr_ctx=temp_mngr_ctx,
        is_streaming=False,
        include_filters=('name == "list-include-yes"',),
    )

    local_host.destroy_agent(agent1)
    local_host.destroy_agent(agent2)

    result_names = [str(a.name) for a in result.agents]
    assert "list-include-yes" in result_names
    assert "list-include-no" not in result_names


# =============================================================================
# discover_hosts_and_agents Tests
# =============================================================================


def test_discover_hosts_and_agents_returns_empty_for_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """discover_hosts_and_agents should return empty dict when no agents exist."""
    agents_by_host, providers = discover_hosts_and_agents(
        temp_mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )
    assert isinstance(agents_by_host, dict)
    assert isinstance(providers, list)
    # At least the local provider should be present
    assert len(providers) > 0


def test_discover_hosts_and_agents_full_discovery_skips_optimization(
    temp_mngr_ctx: MngrContext,
) -> None:
    """is_full_discovery=True should do a full scan even when agent_identifiers is provided."""
    safe_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().is_full_discovery, True),
    )
    # With is_full_discovery=True, passing agent_identifiers should still
    # query all providers (not attempt event-stream resolution).
    # If it tried the optimization with a bogus identifier, it would either
    # error or return filtered results -- but with full discovery it just
    # does a normal scan and returns everything.
    agents_by_host, providers = discover_hosts_and_agents(
        safe_ctx,
        provider_names=None,
        agent_identifiers=("nonexistent-agent-xyz",),
        include_destroyed=False,
        reset_caches=False,
    )
    # Should succeed (full scan) rather than fail trying to resolve the identifier
    assert isinstance(agents_by_host, dict)
    assert len(providers) > 0


# =============================================================================
# _all_identifiers_found Tests
# =============================================================================


def test_all_identifiers_found_by_name() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("h"), provider_name=ProviderInstanceName("local")
    )
    agent = DiscoveredAgent(
        host_id=host.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("my-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    assert _all_identifiers_found(["my-agent"], {host: [agent]})


def test_all_identifiers_found_by_id() -> None:
    agent_id = AgentId.generate()
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("h"), provider_name=ProviderInstanceName("local")
    )
    agent = DiscoveredAgent(
        host_id=host.host_id,
        agent_id=agent_id,
        agent_name=AgentName("x"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    assert _all_identifiers_found([str(agent_id)], {host: [agent]})


def test_all_identifiers_found_returns_false_when_missing() -> None:
    assert not _all_identifiers_found(["missing"], {})


def test_all_identifiers_found_returns_true_for_empty_identifiers() -> None:
    assert _all_identifiers_found([], {})


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
    temp_mngr_ctx: MngrContext,
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

    result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-test")
    assert details.plugin["test_plugin"] == {"status": "active", "score": 42}


@pytest.mark.tmux
def test_field_generators_omit_none_values(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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

    result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-none")
    assert details.plugin["test_plugin"] == {"present": "yes"}


@pytest.mark.tmux
def test_field_generators_multiple_plugins(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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

    result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-multi")
    assert details.plugin["plugin_a"] == {"version": "1.0"}
    assert details.plugin["plugin_b"] == {"count": 5}


@pytest.mark.tmux
def test_field_generators_none_plugin_is_skipped(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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

    result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)
    local_host.destroy_agent(agent)

    details = _find_agent_by_name(result, "field-gen-skip-none")
    assert details.plugin["real_plugin"] == {"value": "present"}
    assert "none_plugin" not in details.plugin


def test_no_field_generators_produces_empty_plugin(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents with no field generator plugins should leave plugin={} on all agents."""
    result = list_agents(mngr_ctx=temp_mngr_ctx, is_streaming=False)
    for agent in result.agents:
        assert agent.plugin == {}


# =============================================================================
# discover_hosts_and_agents Tests
# =============================================================================


@pytest.mark.tmux
def test_discover_hosts_and_agents_groups_agents_by_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """discover_hosts_and_agents should return agents grouped by their host reference."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("grouped-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847304"),
        ),
    )

    agents_by_host, providers = discover_hosts_and_agents(
        temp_mngr_ctx,
        provider_names=None,
        agent_identifiers=None,
        include_destroyed=False,
        reset_caches=False,
    )

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


# =============================================================================
# Test Provider Implementations (for error-path coverage)
# =============================================================================


class _RaisingDiscoveryProviderInstance(MockProviderInstance):
    """Provider that raises MngrError from discover_hosts_and_agents.

    Used to exercise error-handling paths that trigger when a provider
    fails during the discovery phase (before host processing begins).
    """

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        raise MngrError("simulated discovery failure from test")


class _UnavailableDiscoveryProviderInstance(MockProviderInstance):
    """Provider that raises ProviderUnavailableError from discover_hosts_and_agents.

    Simulates a provider whose backend is unreachable (e.g. Modal environment
    deleted). Used to verify that batch discovery gracefully skips the provider.
    """

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        raise ProviderUnavailableError(self.name, "Environment 'mngr-abc123' not found")


class _RaisingDetailProviderInstance(MockProviderInstance):
    """Provider that raises MngrError from get_host_and_agent_details.

    Used to exercise host-level error-handling paths in
    _process_host_with_error_handling.
    """

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Any] | None = None,
        on_error: Any = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        raise MngrError("simulated detail retrieval failure from test")


class _MismatchedProviderInstance(MockProviderInstance):
    """Provider that returns hosts whose provider_name differs from self.name.

    Used to exercise the ProviderInstanceNotFoundError path in
    _list_agents_batch (lines 271-279).
    """

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        mismatched_host = DiscoveredHost(
            host_id=HostId.generate(),
            host_name=HostName("mismatched-host"),
            provider_name=ProviderInstanceName("nonexistent-provider-xyz"),
        )
        agent = DiscoveredAgent(
            host_id=mismatched_host.host_id,
            agent_id=AgentId.generate(),
            agent_name=AgentName("mismatched-agent"),
            provider_name=ProviderInstanceName("nonexistent-provider-xyz"),
        )
        return {mismatched_host: [agent]}


_MISMATCHED_BACKEND_NAME = ProviderBackendName("test-mismatched-backend")


class _MismatchedProviderBackend(ProviderBackendInterface):
    """Backend that creates a _MismatchedProviderInstance."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _MISMATCHED_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend that returns mismatched provider names"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        return _MismatchedProviderInstance(
            name=name,
            host_dir=mngr_ctx.config.default_host_dir,
            mngr_ctx=mngr_ctx,
        )


def _make_list_params(
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    on_error: Any = None,
    on_agent: Any = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> _ListAgentsParams:
    """Build a _ListAgentsParams for testing, with optional CEL filters."""
    compiled_include: list[Any] = []
    compiled_exclude: list[Any] = []
    if include_filters or exclude_filters:
        compiled_include, compiled_exclude = compile_cel_filters(include_filters, exclude_filters)
    return _ListAgentsParams(
        compiled_include_filters=compiled_include,
        compiled_exclude_filters=compiled_exclude,
        error_behavior=error_behavior,
        on_agent=on_agent,
        on_error=on_error,
    )


# =============================================================================
# Lines 199-205: Outer MngrError catch in list_agents (CONTINUE mode)
# =============================================================================


def test_list_agents_continue_mode_captures_top_level_mngr_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents with CONTINUE mode catches a top-level MngrError and records it.

    Adding a provider with an unknown backend to the config causes
    get_all_provider_instances to raise MngrError (UnknownBackendError).
    In CONTINUE mode, this must be caught and recorded as an error rather
    than propagated to the caller.
    """
    failing_config = ProviderInstanceConfig(backend=ProviderBackendName("nonexistent-backend-xyz"))
    updated_config = temp_mngr_ctx.config.model_copy_update(
        to_update(
            temp_mngr_ctx.config.field_ref().providers,
            {ProviderInstanceName("broken-provider"): failing_config},
        ),
    )
    failing_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, updated_config),
    )

    captured_errors: list[ErrorInfo] = []
    result = list_agents(
        mngr_ctx=failing_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        on_error=lambda e: captured_errors.append(e),
    )

    assert len(result.errors) == 1
    assert len(captured_errors) == 1
    assert captured_errors[0] is result.errors[0]
    assert "nonexistent-backend-xyz" in result.errors[0].message


def test_list_agents_abort_mode_propagates_top_level_mngr_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents with ABORT mode re-raises a top-level MngrError.

    The same unknown-backend configuration triggers an error, but in ABORT
    mode it must propagate rather than be swallowed.
    """
    failing_config = ProviderInstanceConfig(backend=ProviderBackendName("nonexistent-backend-xyz"))
    updated_config = temp_mngr_ctx.config.model_copy_update(
        to_update(
            temp_mngr_ctx.config.field_ref().providers,
            {ProviderInstanceName("broken-provider"): failing_config},
        ),
    )
    failing_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, updated_config),
    )

    with pytest.raises(MngrError, match="nonexistent-backend-xyz"):
        list_agents(
            mngr_ctx=failing_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.ABORT,
        )


# =============================================================================
# Lines 235-237: OSError when writing full discovery snapshot
# =============================================================================


def test_maybe_write_full_discovery_snapshot_logs_warning_on_oserror(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot logs a warning when OSError occurs.

    Makes the discovery events path a directory so that the write attempt
    fails with IsADirectoryError (a subclass of OSError). This works
    regardless of whether the process runs as root, unlike chmod-based
    approaches. The function should log the warning and return normally
    rather than propagating the error.
    """
    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    # Make the events path a directory -- writing to a directory raises IsADirectoryError
    events_path.mkdir(exist_ok=True)

    host_details = _make_host_details()
    agent = _make_agent_details("oserror-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    with capture_loguru(level="WARNING") as log_output:
        _maybe_write_full_discovery_snapshot(
            mngr_ctx=temp_mngr_ctx,
            result=result,
            provider_names=None,
            include_filters=(),
            exclude_filters=(),
        )

    output = log_output.getvalue()
    assert "Failed to write full discovery snapshot" in output


def test_maybe_write_full_discovery_snapshot_emits_ssh_host_info(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_maybe_write_full_discovery_snapshot emits SSH info for hosts that have it.

    When an agent's host has SSH connection info, the snapshot write should
    also call emit_host_ssh_info (line 235). The events file must contain
    both the DISCOVERY_FULL and the SSH info events.
    """
    ssh_info = SSHInfo(
        user="ubuntu",
        host="10.0.0.1",
        port=22,
        key_path=Path("/tmp/test-key"),
        command="ssh -i /tmp/test-key -p 22 ubuntu@10.0.0.1",
    )
    host_details = HostDetails(
        id=HostId.generate(),
        name="ssh-host",
        provider_name=ProviderInstanceName("local"),
        ssh=ssh_info,
    )
    agent = _make_agent_details("ssh-agent", host_details)
    result = ListResult()
    result.agents.append(agent)

    _maybe_write_full_discovery_snapshot(
        mngr_ctx=temp_mngr_ctx,
        result=result,
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
    )

    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    assert events_path.exists()
    content = events_path.read_text()
    assert "DISCOVERY_FULL" in content
    assert "ssh-agent" in content


# =============================================================================
# Lines 271-279: ProviderInstanceNotFoundError in batch mode
# =============================================================================


def test_list_agents_batch_continue_mode_handles_mismatched_provider_name(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents batch mode records a ProviderErrorInfo when a host's provider is unknown.

    The _MismatchedProviderInstance returns hosts with a provider_name that does
    not match any entry in the provider_map built by _list_agents_batch.
    In CONTINUE mode this triggers a ProviderInstanceNotFoundError that should
    be recorded (not raised).
    """
    _backend_registry[_MISMATCHED_BACKEND_NAME] = _MismatchedProviderBackend
    _provider_config_registry[_MISMATCHED_BACKEND_NAME] = ProviderInstanceConfig
    try:
        captured_errors: list[ErrorInfo] = []
        result = list_agents(
            mngr_ctx=temp_mngr_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
            on_error=lambda e: captured_errors.append(e),
        )

        provider_errors = [e for e in result.errors if isinstance(e, ProviderErrorInfo)]
        assert len(provider_errors) >= 1
        assert any("nonexistent-provider-xyz" in str(e.provider_name) for e in provider_errors)
        assert len(captured_errors) >= 1
        assert all(isinstance(e, ProviderErrorInfo) for e in captured_errors)
    finally:
        del _backend_registry[_MISMATCHED_BACKEND_NAME]
        del _provider_config_registry[_MISMATCHED_BACKEND_NAME]


def test_list_agents_batch_abort_mode_raises_for_mismatched_provider_name(
    temp_mngr_ctx: MngrContext,
) -> None:
    """list_agents batch mode propagates the error in ABORT mode.

    Same scenario as the CONTINUE test, but with ABORT mode the
    ProviderInstanceNotFoundError must propagate (wrapped by the
    ConcurrencyGroupExecutor) rather than be swallowed.
    """
    _backend_registry[_MISMATCHED_BACKEND_NAME] = _MismatchedProviderBackend
    _provider_config_registry[_MISMATCHED_BACKEND_NAME] = ProviderInstanceConfig
    try:
        with pytest.raises(ConcurrencyExceptionGroup) as exc_info:
            list_agents(
                mngr_ctx=temp_mngr_ctx,
                is_streaming=False,
                error_behavior=ErrorBehavior.ABORT,
            )
        assert exc_info.value.only_exception_is_instance_of(MngrError)
    finally:
        del _backend_registry[_MISMATCHED_BACKEND_NAME]
        del _provider_config_registry[_MISMATCHED_BACKEND_NAME]


# =============================================================================
# Lines 348-385: Provider-level MngrError in streaming mode
# =============================================================================


def test_discover_and_emit_details_streaming_skips_unavailable_provider(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_discover_and_emit_details_for_provider records ProviderUnavailableError in CONTINUE mode.

    ProviderUnavailableError is a MngrError, so the existing catch clause handles it.
    """
    provider = _UnavailableDiscoveryProviderInstance(
        name=ProviderInstanceName("unavailable-modal"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.CONTINUE)

    _discover_and_emit_details_for_provider(
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
        cg=temp_mngr_ctx.concurrency_group,
    )

    assert len(result.errors) == 1
    assert isinstance(result.errors[0], ProviderErrorInfo)
    assert result.errors[0].provider_name == ProviderInstanceName("unavailable-modal")
    assert "not available" in result.errors[0].message


def test_discover_and_emit_details_for_provider_continue_mode_records_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_discover_and_emit_details_for_provider records a ProviderErrorInfo in CONTINUE mode.

    When the provider raises MngrError from discover_hosts_and_agents, the
    error must be caught and stored as a ProviderErrorInfo on the result, and
    the on_error callback must be called.
    """
    provider = _RaisingDiscoveryProviderInstance(
        name=ProviderInstanceName("raising-provider"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    result = ListResult()
    lock = Lock()
    captured_errors: list[ErrorInfo] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_error=lambda e: captured_errors.append(e),
    )

    _discover_and_emit_details_for_provider(
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
        cg=temp_mngr_ctx.concurrency_group,
    )

    assert len(result.errors) == 1
    assert isinstance(result.errors[0], ProviderErrorInfo)
    assert result.errors[0].provider_name == ProviderInstanceName("raising-provider")
    assert "simulated discovery failure from test" in result.errors[0].message
    assert len(captured_errors) == 1
    assert captured_errors[0] is result.errors[0]


def test_discover_and_emit_details_for_provider_abort_mode_propagates_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_discover_and_emit_details_for_provider re-raises the MngrError in ABORT mode."""
    provider = _RaisingDiscoveryProviderInstance(
        name=ProviderInstanceName("raising-provider"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.ABORT)

    with pytest.raises(MngrError, match="simulated discovery failure from test"):
        _discover_and_emit_details_for_provider(
            provider=provider,
            params=params,
            result=result,
            results_lock=lock,
            cg=temp_mngr_ctx.concurrency_group,
        )

    assert result.errors == []


def test_discover_and_emit_details_for_provider_success_path_processes_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_discover_and_emit_details_for_provider processes hosts and agents when discovery succeeds.

    Uses a MockProviderInstance with a host and agents configured via mock data.
    This exercises the success path (lines 353-376): warn_on_duplicate_host_names,
    the host-submission loop, and future.result() teardown.
    """
    host_id = HostId.generate()
    provider = MockProviderInstance(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="streaming-host",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        updated_at=datetime.now(timezone.utc),
    )
    offline_host = make_offline_host(certified_data, provider, temp_mngr_ctx)
    provider.mock_hosts.append(offline_host)

    agent_id = AgentId.generate()
    provider.mock_agent_data = [
        {
            "id": str(agent_id),
            "name": "streaming-agent",
            "type": "generic",
            "command": "sleep 1",
            "work_dir": "/tmp",
        }
    ]

    result = ListResult()
    lock = Lock()
    collected_agents: list[AgentDetails] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_agent=lambda a: collected_agents.append(a),
    )

    _discover_and_emit_details_for_provider(
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
        cg=temp_mngr_ctx.concurrency_group,
    )

    assert result.errors == []
    assert len(result.agents) == 1
    assert str(result.agents[0].name) == "streaming-agent"
    assert len(collected_agents) == 1


# =============================================================================
# Lines 396-405: Error differentiation in _handle_listing_error
# =============================================================================


def test_handle_listing_error_continue_with_discovered_agent_creates_agent_error() -> None:
    """_handle_listing_error creates AgentErrorInfo when the source is a DiscoveredAgent."""
    host_id = HostId.generate()
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("erroring-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    exception = MngrError("simulated agent lookup failure from test")
    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.CONTINUE)

    _handle_listing_error(
        source=agent_ref,
        exception=exception,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(result.errors) == 1
    assert isinstance(result.errors[0], AgentErrorInfo)
    assert result.errors[0].agent_id == agent_ref.agent_id
    assert result.errors[0].message == "simulated agent lookup failure from test"


def test_handle_listing_error_continue_with_discovered_host_creates_host_error() -> None:
    """_handle_listing_error creates HostErrorInfo when the source is a DiscoveredHost."""
    host_ref = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("erroring-host"),
        provider_name=ProviderInstanceName("local"),
    )
    exception = MngrError("simulated host unreachable from test")
    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.CONTINUE)

    _handle_listing_error(
        source=host_ref,
        exception=exception,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(result.errors) == 1
    assert isinstance(result.errors[0], HostErrorInfo)
    assert result.errors[0].host_id == host_ref.host_id
    assert result.errors[0].message == "simulated host unreachable from test"


def test_handle_listing_error_continue_calls_on_error_callback() -> None:
    """_handle_listing_error invokes on_error when provided in CONTINUE mode."""
    host_id = HostId.generate()
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("callback-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    exception = MngrError("simulated callback error from test")
    result = ListResult()
    lock = Lock()
    captured: list[ErrorInfo] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_error=lambda e: captured.append(e),
    )

    _handle_listing_error(
        source=agent_ref,
        exception=exception,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(captured) == 1
    assert captured[0] is result.errors[0]


def test_handle_listing_error_abort_mode_raises() -> None:
    """_handle_listing_error re-raises the exception in ABORT mode."""
    host_ref = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("aborting-host"),
        provider_name=ProviderInstanceName("local"),
    )
    exception = MngrError("simulated abort error from test")
    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.ABORT)

    with pytest.raises(MngrError, match="simulated abort error from test"):
        _handle_listing_error(
            source=host_ref,
            exception=exception,
            params=params,
            result=result,
            results_lock=lock,
        )

    assert result.errors == []


# =============================================================================
# Lines 416-430: CEL filter application in _collect_and_emit_details_for_host
# =============================================================================


def _make_offline_test_provider(
    host_id: HostId,
    provider_name: str,
    temp_mngr_ctx: MngrContext,
) -> MockProviderInstance:
    """Create a MockProviderInstance with an OfflineHost for the given host_id."""
    provider = MockProviderInstance(
        name=ProviderInstanceName(provider_name),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        updated_at=datetime.now(timezone.utc),
    )
    offline_host = make_offline_host(certified_data, provider, temp_mngr_ctx)
    provider.mock_hosts.append(offline_host)
    return provider


def test_collect_and_emit_details_for_host_include_filter_keeps_matching_agent(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_collect_and_emit_details_for_host adds an agent that matches an include filter."""
    host_id = HostId.generate()
    provider_name = "test-local"
    provider = _make_offline_test_provider(host_id, provider_name, temp_mngr_ctx)

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName(provider_name),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("target-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"type": "generic", "command": "sleep 1", "work_dir": "/tmp"},
    )

    result = ListResult()
    lock = Lock()
    collected_agents: list[AgentDetails] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_agent=lambda a: collected_agents.append(a),
        include_filters=('name == "target-agent"',),
    )

    _collect_and_emit_details_for_host(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(result.agents) == 1
    assert str(result.agents[0].name) == "target-agent"
    assert len(collected_agents) == 1


def test_collect_and_emit_details_for_host_include_filter_drops_non_matching_agent(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_collect_and_emit_details_for_host drops an agent that fails the include filter."""
    host_id = HostId.generate()
    provider_name = "test-local"
    provider = _make_offline_test_provider(host_id, provider_name, temp_mngr_ctx)

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName(provider_name),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("other-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"type": "generic", "command": "sleep 1", "work_dir": "/tmp"},
    )

    result = ListResult()
    lock = Lock()
    collected_agents: list[AgentDetails] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_agent=lambda a: collected_agents.append(a),
        include_filters=('name == "target-agent"',),
    )

    _collect_and_emit_details_for_host(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert result.agents == []
    assert collected_agents == []


def test_collect_and_emit_details_for_host_exclude_filter_drops_matching_agent(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_collect_and_emit_details_for_host drops an agent that matches an exclude filter."""
    host_id = HostId.generate()
    provider_name = "test-local"
    provider = _make_offline_test_provider(host_id, provider_name, temp_mngr_ctx)

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName(provider_name),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("excluded-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"type": "generic", "command": "sleep 1", "work_dir": "/tmp"},
    )

    result = ListResult()
    lock = Lock()
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        exclude_filters=('name == "excluded-agent"',),
    )

    _collect_and_emit_details_for_host(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert result.agents == []


def test_collect_and_emit_details_for_host_no_filter_adds_all_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_collect_and_emit_details_for_host adds all agents when no filters are provided."""
    host_id = HostId.generate()
    provider_name = "test-local"
    provider = _make_offline_test_provider(host_id, provider_name, temp_mngr_ctx)

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName(provider_name),
    )
    agent_refs = [
        DiscoveredAgent(
            host_id=host_id,
            agent_id=AgentId.generate(),
            agent_name=AgentName(f"agent-{i}"),
            provider_name=ProviderInstanceName(provider_name),
            certified_data={"type": "generic", "command": "sleep 1", "work_dir": "/tmp"},
        )
        for i in range(3)
    ]

    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.CONTINUE)

    _collect_and_emit_details_for_host(
        host_ref=host_ref,
        agent_refs=agent_refs,
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(result.agents) == 3


# =============================================================================
# Lines 446-463: Host-level error handling in _process_host_with_error_handling
# =============================================================================


def test_process_host_with_error_handling_continue_mode_records_host_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_process_host_with_error_handling records a HostErrorInfo in CONTINUE mode.

    When the provider raises MngrError from get_host_and_agent_details, the
    error must be caught and stored as a HostErrorInfo, and on_error called.
    """
    provider = _RaisingDetailProviderInstance(
        name=ProviderInstanceName("raising-provider"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    host_id = HostId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("raising-provider"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("raising-provider"),
    )

    result = ListResult()
    lock = Lock()
    captured_errors: list[ErrorInfo] = []
    params = _make_list_params(
        error_behavior=ErrorBehavior.CONTINUE,
        on_error=lambda e: captured_errors.append(e),
    )

    _process_host_with_error_handling(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        provider=provider,
        params=params,
        result=result,
        results_lock=lock,
    )

    assert len(result.errors) == 1
    assert isinstance(result.errors[0], HostErrorInfo)
    assert result.errors[0].host_id == host_id
    assert "simulated detail retrieval failure from test" in result.errors[0].message
    assert len(captured_errors) == 1
    assert captured_errors[0] is result.errors[0]


def test_process_host_with_error_handling_abort_mode_propagates_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_process_host_with_error_handling re-raises MngrError in ABORT mode."""
    provider = _RaisingDetailProviderInstance(
        name=ProviderInstanceName("raising-provider"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    host_id = HostId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("raising-provider"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("raising-provider"),
    )

    result = ListResult()
    lock = Lock()
    params = _make_list_params(error_behavior=ErrorBehavior.ABORT)

    with pytest.raises(MngrError, match="simulated detail retrieval failure from test"):
        _process_host_with_error_handling(
            host_ref=host_ref,
            agent_refs=[agent_ref],
            provider=provider,
            params=params,
            result=result,
            results_lock=lock,
        )

    assert result.errors == []
