"""Unit tests for cleanup API functions."""

from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pluggy
import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr import hookimpl
from imbue.mngr.api.cleanup import _run_post_cleanup_gc
from imbue.mngr.api.cleanup import execute_cleanup
from imbue.mngr.api.cleanup import find_agents_for_cleanup
from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.data_types import CleanupResult
from imbue.mngr.api.providers import register_provider_instance
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_test_agent_details


class _DestroyErrorPlugin:
    """Test plugin that raises MngrError from on_before_agent_destroy."""

    @hookimpl
    def on_before_agent_destroy(self, agent: Any, host: Any) -> None:
        raise MngrError("Simulated destroy hook error")


def _make_ctx_with_plugin(temp_mngr_ctx: MngrContext, plugin: object) -> MngrContext:
    """Create a MngrContext that has the given plugin registered in its plugin manager."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(plugin)
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().pm, pm))


class _OfflineHostProvider(LocalProviderInstance):
    """Provider that returns an offline HostInterface from get_host().

    destroy_host() raises LocalHostNotDestroyableError (a MngrError), which
    triggers the error path in _execute_destroy when the match arm falls through
    to HostInterface (the offline branch).

    The return type annotation on get_host is intentionally omitted: the parent
    declares `-> Host` but this class returns `OfflineHost`, which satisfies
    HostInterface but is not a Host subclass. Omitting the annotation lets the
    type checker (ty) skip the incompatibility check.
    """

    def get_host(self, host: HostId | HostName):
        host_id = host if isinstance(host, HostId) else HostId.generate()
        now = datetime.now(timezone.utc)
        certified_data = CertifiedHostData(
            created_at=now,
            updated_at=now,
            host_id=str(host_id),
            host_name="test-offline-host",
        )
        return OfflineHost(
            id=host_id,
            certified_host_data=certified_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )


class _OfflineHostSuccessProvider(_OfflineHostProvider):
    """Provider that returns an offline HostInterface from get_host() and
    whose destroy_host() succeeds (no-op).

    Used to test the success path in _execute_destroy for offline hosts
    (lines 135-139 of cleanup.py).
    """

    def destroy_host(self, host: HostInterface | HostId) -> None:
        pass


class _StopFailingHost(Host):
    """Host subclass whose stop_agents always raises MngrError.

    Used to test that _execute_stop records stop errors and respects ABORT.
    """

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        raise MngrError("Simulated stop error")


class _StopFailingProvider(LocalProviderInstance):
    """Provider that returns a _StopFailingHost from get_host().

    Injects the stop-error path into _execute_stop without requiring tmux.
    """

    def get_host(self, host: HostId | HostName) -> _StopFailingHost:
        pyinfra_host = self._create_local_pyinfra_host()
        connector = PyinfraConnector(pyinfra_host)
        return _StopFailingHost(
            id=self.host_id,
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )


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


def test_execute_cleanup_dry_run_returns_cleanup_result_type(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run should return a CleanupResult instance."""
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[make_test_agent_details("test-agent")],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert isinstance(result, CleanupResult)


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

    This exercises lines 119-123 of cleanup.py (the for/else branch).
    """
    # Create an AgentDetails that references the real local host but with a
    # non-existent agent ID.  get_agents() on the local host will return an
    # empty list, so the for-loop's else clause fires.
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
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    local_host: Host,
) -> None:
    """When on_before_agent_destroy raises MngrError with ABORT, the error is recorded and
    processing stops immediately without destroying subsequent agents.

    This exercises lines 124-129 of cleanup.py.
    """
    # Create a real agent state so get_agents() finds it and fires the hook.
    real_agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-hook-error-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99"),
        ),
    )

    try:
        host_details = HostDetails(
            id=local_provider.host_id,
            name="localhost",
            provider_name=LOCAL_PROVIDER_NAME,
        )
        # The first AgentDetails uses the real agent ID so the hook fires.
        first_agent = AgentDetails(
            id=real_agent.id,
            name=AgentName("cleanup-hook-error-agent"),
            type="generic",
            command=CommandString("sleep 99"),
            work_dir=temp_work_dir,
            initial_branch=None,
            create_time=datetime.now(timezone.utc),
            start_on_boot=False,
            state=AgentLifecycleState.STOPPED,
            host=host_details,
        )
        # The second AgentDetails is on the same host.  With ABORT, it should
        # never be processed after the first agent's hook raises.
        second_agent = make_test_agent_details(
            name="cleanup-second-agent",
            host_id=local_provider.host_id,
            provider_name=LOCAL_PROVIDER_NAME,
        )

        ctx = _make_ctx_with_plugin(temp_mngr_ctx, _DestroyErrorPlugin())

        result = execute_cleanup(
            mngr_ctx=ctx,
            agents=[first_agent, second_agent],
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
        )

        # The hook error must be recorded.
        assert len(result.errors) == 1
        assert "Simulated destroy hook error" in result.errors[0]
        assert "cleanup-hook-error-agent" in result.errors[0]
        # First agent was not destroyed (hook raised before destroy_agent).
        assert AgentName("cleanup-hook-error-agent") not in result.destroyed_agents
        # Second agent was never processed because ABORT caused an early return.
        assert AgentName("cleanup-second-agent") not in result.destroyed_agents
    finally:
        # Clean up the real agent state regardless of test outcome.
        local_host.destroy_agent(real_agent)


def test_execute_cleanup_destroy_offline_host_error_with_abort(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When destroying an offline host raises MngrError with ABORT, the error is
    recorded and processing stops immediately.

    This exercises lines 140-145 of cleanup.py.
    """
    # Register a custom provider that always returns an OfflineHost from get_host().
    # LocalProviderInstance.destroy_host() always raises LocalHostNotDestroyableError
    # (a MngrError), which is exactly the error path we want to exercise.
    provider_name = ProviderInstanceName("offline-test-provider")
    offline_provider = _OfflineHostProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    register_provider_instance(provider_name, temp_mngr_ctx, offline_provider)

    try:
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

        # The destroy error must be recorded.
        assert len(result.errors) >= 1
        assert any("Cannot destroy the local host" in e for e in result.errors)
        # No agents should have been reported as destroyed.
        assert result.destroyed_agents == []
    finally:
        reset_provider_instances()


def test_execute_cleanup_destroy_unknown_provider_with_abort_stops_processing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When accessing a provider during destroy raises MngrError with ABORT, the error is
    recorded and processing stops, leaving subsequent hosts unprocessed.

    This exercises lines 148-153 of cleanup.py.
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
    assert len(result.errors) >= 1
    assert any("Error accessing host" in e for e in result.errors)
    # Nothing was destroyed.
    assert result.destroyed_agents == []
    assert result.stopped_agents == []


def test_execute_cleanup_stop_error_with_abort_stops_processing(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When stop_agents raises MngrError with ABORT, the error is recorded and
    processing stops immediately.

    This exercises lines 178-183 of cleanup.py.

    The error is triggered by injecting a _StopFailingProvider into the instance
    cache.  Its get_host() returns a _StopFailingHost whose stop_agents() always
    raises MngrError, bypassing any tmux infrastructure.
    """
    provider_name = ProviderInstanceName("stop-error-test-provider")
    stop_provider = _StopFailingProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    register_provider_instance(provider_name, temp_mngr_ctx, stop_provider)

    try:
        host_details = HostDetails(
            id=stop_provider.host_id,
            name="localhost",
            provider_name=provider_name,
        )
        first_agent = AgentDetails(
            id=AgentId.generate(),
            name=AgentName("stop-error-agent-one"),
            type="generic",
            command=CommandString("sleep 99"),
            work_dir=Path("/tmp/work"),
            initial_branch=None,
            create_time=datetime.now(timezone.utc),
            start_on_boot=False,
            state=AgentLifecycleState.STOPPED,
            host=host_details,
        )
        second_agent = AgentDetails(
            id=AgentId.generate(),
            name=AgentName("stop-error-agent-two"),
            type="generic",
            command=CommandString("sleep 99"),
            work_dir=Path("/tmp/work"),
            initial_branch=None,
            create_time=datetime.now(timezone.utc),
            start_on_boot=False,
            state=AgentLifecycleState.STOPPED,
            host=host_details,
        )

        result = execute_cleanup(
            mngr_ctx=temp_mngr_ctx,
            agents=[first_agent, second_agent],
            action=CleanupAction.STOP,
            is_dry_run=False,
            error_behavior=ErrorBehavior.ABORT,
        )

        # The stop error must be recorded.
        assert len(result.errors) == 1
        assert "Error stopping agents on host" in result.errors[0]
        assert result.stopped_agents == []
    finally:
        reset_provider_instances()


def test_execute_cleanup_stop_unknown_provider_with_abort_stops_processing(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When accessing a provider during stop raises MngrError with ABORT, the error is
    recorded and processing stops, leaving subsequent hosts unprocessed.

    This exercises lines 193-198 of cleanup.py.
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
    assert len(result.errors) >= 1
    assert any("Error accessing host" in e for e in result.errors)
    # Nothing was stopped.
    assert result.stopped_agents == []
    assert result.destroyed_agents == []


def test_run_post_cleanup_gc_provider_error_is_recorded_in_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """When get_all_provider_instances raises MngrError, _run_post_cleanup_gc
    catches it and appends a descriptive error to the result.

    This exercises lines 227-230 of cleanup.py.
    """
    # Inject a provider config with a non-existent backend.  When
    # get_all_provider_instances() iterates configured providers and tries to
    # build this instance, it calls get_backend("nonexistent-gc-backend") which
    # raises UnknownBackendError (a MngrError).
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


def test_execute_cleanup_destroy_offline_host_success(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When destroying an offline host succeeds, agents are added to destroyed_agents.

    This exercises lines 135-139 of cleanup.py (the success path in the offline host
    destroy branch).
    """
    provider_name = ProviderInstanceName("offline-success-provider")
    success_provider = _OfflineHostSuccessProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    register_provider_instance(provider_name, temp_mngr_ctx, success_provider)

    try:
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

        assert result.errors == []
        assert AgentName("offline-success-agent-one") in result.destroyed_agents
        assert AgentName("offline-success-agent-two") in result.destroyed_agents
    finally:
        reset_provider_instances()


def test_execute_cleanup_stop_on_offline_host_skips_with_warning(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When a STOP action is attempted on an offline host, the host is skipped with a warning.

    This exercises lines 184-192 of cleanup.py (the offline-host skip branch in
    _execute_stop).
    """
    provider_name = ProviderInstanceName("offline-stop-provider")
    offline_provider = _OfflineHostProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    register_provider_instance(provider_name, temp_mngr_ctx, offline_provider)

    try:
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

        # Offline host agents are not stopped, a warning is recorded instead.
        assert result.stopped_agents == []
        assert len(result.errors) == 1
        assert "Skipping" in result.errors[0]
        assert "offline host" in result.errors[0]
    finally:
        reset_provider_instances()
