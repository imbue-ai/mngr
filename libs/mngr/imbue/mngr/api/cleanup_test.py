"""Unit tests for cleanup API functions."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr import hookimpl
from imbue.mngr.api.cleanup import _run_post_cleanup_gc
from imbue.mngr.api.cleanup import execute_cleanup
from imbue.mngr.api.cleanup import find_agents_for_cleanup
from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.data_types import CleanupResult
from imbue.mngr.api.testing import inject_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import OfflineHostDestroyableProvider
from imbue.mngr.providers.mock_provider_test import OfflineHostProvider
from imbue.mngr.providers.mock_provider_test import StopFailingProvider
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_ctx_with_plugins
from imbue.mngr.utils.testing import make_test_agent_details


class _DestroyErrorPlugin:
    """Test plugin that raises MngrError from on_before_agent_destroy."""

    @hookimpl
    def on_before_agent_destroy(self, agent: Any, host: Any) -> None:
        raise MngrError("Simulated destroy hook error")


class _VolumeGcErrorProvider(OfflineHostProvider):
    """Provider whose post-cleanup garbage collection records (but does not raise) an error.

    get_host() is inherited from OfflineHostProvider and returns an offline host, so
    the work-dir and machine GC passes skip it without touching tmux or raising.
    list_volumes() reports one volume attached to an unknown host (so it is treated as
    orphaned), and delete_volume() raises MngrError. With ErrorBehavior.CONTINUE (which
    _run_post_cleanup_gc always uses), gc_volumes catches that error and appends it to
    GcResult.errors rather than propagating it -- exactly the success-of-gc,
    error-in-result branch that _run_post_cleanup_gc forwards with the "GC: " prefix.

    delete_volume_call_count records invocations so the test can confirm the GC
    delete path was actually exercised.
    """

    delete_volume_call_count: int = Field(default=0)

    def list_volumes(self) -> list[VolumeInfo]:
        return [
            VolumeInfo(
                volume_id=VolumeId.generate(),
                name="orphaned-gc-error-volume",
                size_bytes=0,
                host_id=None,
            )
        ]

    def delete_volume(self, volume_id: VolumeId) -> None:
        self.delete_volume_call_count += 1
        raise MngrError("Simulated volume deletion failure")


def test_execute_cleanup_dry_run_destroy_populates_destroyed_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run destroy should list all agent names in destroyed_agents."""
    agents = [
        make_test_agent_details("agent-alpha"),
        make_test_agent_details("agent-beta"),
        make_test_agent_details("agent-gamma"),
    ]

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.destroyed_agents == [
        AgentName("agent-alpha"),
        AgentName("agent-beta"),
        AgentName("agent-gamma"),
    ]
    assert result.stopped_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_stop_populates_stopped_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run stop should list all agent names in stopped_agents."""
    agents = [
        make_test_agent_details("agent-one"),
        make_test_agent_details("agent-two"),
    ]

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.STOP,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.stopped_agents == [
        AgentName("agent-one"),
        AgentName("agent-two"),
    ]
    assert result.destroyed_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_with_no_agents_returns_empty_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run with an empty agent list should return an empty result."""
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.destroyed_agents == []
    assert result.stopped_agents == []
    assert result.errors == []


# --- Integration tests with real local provider ---


@pytest.mark.tmux
def test_find_agents_for_cleanup_returns_matching_agents(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_for_cleanup should return agents matching include filters."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-find-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99001"),
        ),
    )
    local_host.start_agents([agent.id])

    try:
        agents = find_agents_for_cleanup(
            mngr_ctx=temp_mngr_ctx,
            include_filters=('name == "cleanup-find-test"',),
            exclude_filters=(),
            error_behavior=ErrorBehavior.CONTINUE,
        )

        assert len(agents) == 1
        assert agents[0].name == AgentName("cleanup-find-test")
    finally:
        local_host.destroy_agent(agent)


def test_find_agents_for_cleanup_returns_empty_when_no_match(
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agents_for_cleanup should return empty list when no agents match."""
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "nonexistent-agent-xyz"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert agents == []


@pytest.mark.tmux
def test_execute_cleanup_destroy_on_online_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """execute_cleanup with DESTROY action should destroy agents on an online host."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-destroy-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99002"),
        ),
    )
    local_host.start_agents([agent.id])

    # Find the agent via the API
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Destroy it (non-dry-run)
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.DESTROY,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert AgentName("cleanup-destroy-test") in result.destroyed_agents
    assert result.stopped_agents == []

    # Verify the agent no longer exists on the host
    remaining = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(remaining) == 0


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_execute_cleanup_stop_on_online_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """execute_cleanup with STOP action should stop agents on an online host."""

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-stop-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99003"),
        ),
    )
    local_host.start_agents([agent.id])

    # Wait for agent to be alive before stop (race: tmux may not have started the
    # sleep process yet when get_lifecycle_state is called immediately)
    wait_for(
        lambda: agent.get_lifecycle_state() in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
        error_message="Expected agent lifecycle state to be RUNNING or WAITING",
    )

    # Find the agent via the API
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-stop-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Stop it (non-dry-run)
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.STOP,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert AgentName("cleanup-stop-test") in result.stopped_agents
    assert result.destroyed_agents == []

    # Verify the agent is now stopped
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    # Clean up
    local_host.destroy_agent(agent)


# --- Error path tests ---


def test_execute_cleanup_destroy_agent_not_found_on_host_treated_as_destroyed(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """When the agent is not found on the host during destroy, it is treated as already destroyed.

    Covers the case where the agent has already been removed from the host.
    """
    # Create an AgentDetails that references the real local host but with a
    # non-existent agent ID so the host won't find it during destroy.
    agent_details = make_test_agent_details(
        name="cleanup-not-found-agent",
        host_id=local_provider.host_id,
        provider_name=LOCAL_PROVIDER_NAME,
    )

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[agent_details],
        action=CleanupAction.DESTROY,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    # Agent is treated as already destroyed (graceful degradation).
    assert AgentName("cleanup-not-found-agent") in result.destroyed_agents
    assert result.stopped_agents == []
    assert result.errors == []


@pytest.mark.tmux
def test_execute_cleanup_destroy_hook_error_with_abort_stops_processing(
    temp_work_dir: Path,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """When on_before_agent_destroy raises MngrError with ABORT, the error is recorded and
    processing stops immediately without destroying subsequent agents.

    """
    second_work_dir = tmp_path / "work_dir_2"
    second_work_dir.mkdir()

    # Create two real agents so both are discoverable on the host.
    first_agent_state = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-hook-error-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99"),
        ),
    )
    second_agent_state = local_host.create_agent_state(
        work_dir_path=second_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-second-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99"),
        ),
    )

    try:
        agents = find_agents_for_cleanup(
            mngr_ctx=temp_mngr_ctx,
            include_filters=('name == "cleanup-hook-error-agent" || name == "cleanup-second-agent"',),
            exclude_filters=(),
            error_behavior=ErrorBehavior.ABORT,
        )
        assert len(agents) == 2

        ctx = make_ctx_with_plugins(temp_mngr_ctx, [_DestroyErrorPlugin()])

        result = execute_cleanup(
            mngr_ctx=ctx,
            agents=agents,
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
        )

        # The hook error must be recorded.
        assert len(result.errors) == 1
        assert "Simulated destroy hook error" in result.errors[0]
        # First agent was not destroyed (hook raised before destroy_agent).
        assert AgentName("cleanup-hook-error-agent") not in result.destroyed_agents
        # Second agent was never processed because ABORT caused an early return.
        assert AgentName("cleanup-second-agent") not in result.destroyed_agents
    finally:
        local_host.destroy_agent(first_agent_state)
        local_host.destroy_agent(second_agent_state)


@pytest.mark.allow_warnings(match=r"^Error destroying offline host")
def test_execute_cleanup_destroy_offline_host_error_with_abort(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When destroying an offline host raises MngrError with ABORT, the error is
    recorded and processing stops immediately.

    """
    # Register a custom provider that always returns an OfflineHost from get_host().
    # LocalProviderInstance.destroy_host() always raises LocalHostNotDestroyableError
    # (a MngrError), which is exactly the error path we want to exercise.
    provider_name = ProviderInstanceName("offline-test-provider")
    offline_provider = OfflineHostProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    with inject_provider_instance(offline_provider, temp_mngr_ctx):
        # Create two agents on the fake offline host so we can verify ABORT stops
        # processing after the first host's error.
        first_agent = make_test_agent_details(
            name="offline-host-agent-one",
            host_id=HostId.generate(),
            provider_name=provider_name,
        )
        second_agent = make_test_agent_details(
            name="offline-host-agent-two",
            host_id=HostId.generate(),
            provider_name=provider_name,
        )

        result = execute_cleanup(
            mngr_ctx=temp_mngr_ctx,
            agents=[first_agent, second_agent],
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
        )

        # The injected provider must actually have been consulted (guards against
        # a cache-key mismatch silently resolving a real provider instead).
        assert offline_provider.get_host_call_count >= 1
        # The destroy error must be recorded.
        assert len(result.errors) == 1
        assert any("Cannot destroy the local host" in e for e in result.errors)
        # No agents should have been reported as destroyed.
        assert result.destroyed_agents == []


@pytest.mark.allow_warnings(match=r"^Error accessing host")
def test_execute_cleanup_destroy_unknown_provider_with_abort_stops_processing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When an agent references a non-existent provider, the destroy path catches the
    MngrError from get_provider_instance() and returns early in ABORT mode.
    """
    unknown_provider = ProviderInstanceName("unknown-destroy-provider")
    first_agent = make_test_agent_details(
        name="bad-provider-agent-one",
        host_id=HostId.generate(),
        provider_name=unknown_provider,
    )
    # Second agent on a different host (also unknown provider).
    second_agent = make_test_agent_details(
        name="bad-provider-agent-two",
        host_id=HostId.generate(),
        provider_name=unknown_provider,
    )

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[first_agent, second_agent],
        action=CleanupAction.DESTROY,
        is_dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    # At least the first error is recorded (provider access fails).
    assert len(result.errors) == 1
    assert any("Error accessing host" in e for e in result.errors)
    # Nothing was destroyed.
    assert result.destroyed_agents == []
    assert result.stopped_agents == []


@pytest.mark.allow_warnings(match=r"^Error stopping agents on host")
def test_execute_cleanup_stop_error_with_abort_stops_processing(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When stop_agents raises MngrError with ABORT, the error is recorded and
    processing stops immediately.


    The error is triggered by injecting a StopFailingProvider into the instance
    cache.  Its get_host() returns a StopFailingHost whose stop_agents() always
    raises MngrError, bypassing any tmux infrastructure.
    """
    provider_name = ProviderInstanceName("stop-error-test-provider")
    stop_provider = StopFailingProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    with inject_provider_instance(stop_provider, temp_mngr_ctx):
        first_agent = make_test_agent_details(
            name="stop-error-agent-one",
            host_id=stop_provider.host_id,
            provider_name=provider_name,
            state=AgentLifecycleState.STOPPED,
        )
        second_agent = make_test_agent_details(
            name="stop-error-agent-two",
            host_id=stop_provider.host_id,
            provider_name=provider_name,
            state=AgentLifecycleState.STOPPED,
        )

        result = execute_cleanup(
            mngr_ctx=temp_mngr_ctx,
            agents=[first_agent, second_agent],
            action=CleanupAction.STOP,
            is_dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
        )

        # The injected provider must actually have been consulted (guards against
        # a cache-key mismatch silently resolving a real provider instead).
        assert stop_provider.get_host_call_count >= 1
        # The stop error must be recorded.
        assert len(result.errors) == 1
        assert "Error stopping agents on host" in result.errors[0]
        assert result.stopped_agents == []


@pytest.mark.allow_warnings(match=r"^Error accessing host")
def test_execute_cleanup_stop_unknown_provider_with_abort_stops_processing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When an agent references a non-existent provider, the stop path catches the
    MngrError from get_provider_instance() and returns early in ABORT mode.
    """
    unknown_provider = ProviderInstanceName("unknown-stop-provider")
    first_agent = make_test_agent_details(
        name="stop-bad-provider-agent-one",
        host_id=HostId.generate(),
        provider_name=unknown_provider,
    )
    second_agent = make_test_agent_details(
        name="stop-bad-provider-agent-two",
        host_id=HostId.generate(),
        provider_name=unknown_provider,
    )

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[first_agent, second_agent],
        action=CleanupAction.STOP,
        is_dry_run=False,
        error_behavior=ErrorBehavior.ABORT,
    )

    # At least the first error is recorded (provider access fails).
    assert len(result.errors) == 1
    assert any("Error accessing host" in e for e in result.errors)
    # Nothing was stopped.
    assert result.stopped_agents == []
    assert result.destroyed_agents == []


@pytest.mark.allow_warnings(
    match=r"^Post-cleanup garbage collection failed: Unknown provider backend: nonexistent-gc-backend"
)
def test_run_post_cleanup_gc_provider_error_is_recorded_in_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When get_all_provider_instances raises MngrError (the default ABORT behavior),
    _run_post_cleanup_gc catches it and appends a descriptive error to the result.
    """
    bad_providers = {
        ProviderInstanceName("bad-gc-provider"): ProviderInstanceConfig(
            backend=ProviderBackendName("nonexistent-gc-backend"),
        )
    }
    bad_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, bad_providers)
    )
    bad_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, bad_config))

    result = CleanupResult()
    _run_post_cleanup_gc(bad_ctx, result)

    assert len(result.errors) == 1
    assert result.errors[0].startswith("Post-cleanup garbage collection failed:")


@pytest.mark.allow_warnings(match=r"Failed to delete volume orphaned-gc-error-volume")
def test_run_post_cleanup_gc_forwards_gc_errors_with_prefix(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When garbage collection itself succeeds but records non-fatal errors, those
    errors are forwarded into the cleanup result with the "GC: " prefix.

    This exercises the success branch of _run_post_cleanup_gc (cleanup.py:217-226):
    api_gc runs without raising, but gc_volumes records a delete failure in
    gc_result.errors, which _run_post_cleanup_gc must re-emit verbatim under "GC: ".
    """
    provider_name = LOCAL_PROVIDER_NAME
    gc_error_provider = _VolumeGcErrorProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    with inject_provider_instance(gc_error_provider, temp_mngr_ctx):
        result = CleanupResult()
        _run_post_cleanup_gc(temp_mngr_ctx, result)

    # The GC delete path was actually exercised (the injected provider was consulted).
    assert gc_error_provider.delete_volume_call_count >= 1
    # The recorded GC error is forwarded with the "GC: " prefix.
    gc_errors = [e for e in result.errors if e.startswith("GC: ")]
    assert len(gc_errors) == 1
    assert "Simulated volume deletion failure" in gc_errors[0]


def test_execute_cleanup_destroy_offline_host_success(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When destroying an offline host succeeds, agents are added to destroyed_agents."""
    provider_name = ProviderInstanceName("offline-success-provider")
    success_provider = OfflineHostDestroyableProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    with inject_provider_instance(success_provider, temp_mngr_ctx):
        first_agent = make_test_agent_details(
            name="offline-success-agent-one",
            host_id=HostId.generate(),
            provider_name=provider_name,
        )
        second_agent = make_test_agent_details(
            name="offline-success-agent-two",
            host_id=HostId.generate(),
            provider_name=provider_name,
        )

        result = execute_cleanup(
            mngr_ctx=temp_mngr_ctx,
            agents=[first_agent, second_agent],
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        # The injected provider must actually have been consulted (guards against
        # a cache-key mismatch silently resolving a real provider instead).
        assert success_provider.get_host_call_count >= 1
        assert result.errors == []
        assert AgentName("offline-success-agent-one") in result.destroyed_agents
        assert AgentName("offline-success-agent-two") in result.destroyed_agents


@pytest.mark.allow_warnings(match=r"^Skipping 1 agent\(s\) on offline host")
def test_execute_cleanup_stop_on_offline_host_skips_with_warning(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When a STOP action is attempted on an offline host, the host is skipped with a warning."""
    provider_name = ProviderInstanceName("offline-stop-provider")
    offline_provider = OfflineHostProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    with inject_provider_instance(offline_provider, temp_mngr_ctx):
        agent = make_test_agent_details(
            name="offline-stop-agent",
            host_id=HostId.generate(),
            provider_name=provider_name,
        )

        result = execute_cleanup(
            mngr_ctx=temp_mngr_ctx,
            agents=[agent],
            action=CleanupAction.STOP,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        # The injected provider must actually have been consulted (guards against
        # a cache-key mismatch silently resolving a real provider instead).
        assert offline_provider.get_host_call_count >= 1
        # Offline host agents are not stopped, a warning is recorded instead.
        assert result.stopped_agents == []
        assert len(result.errors) == 1
        assert "Skipping" in result.errors[0]
        assert "offline host" in result.errors[0]
