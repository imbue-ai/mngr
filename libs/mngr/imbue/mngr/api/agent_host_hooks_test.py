"""Unit tests for agent and host lifecycle hooks.

Tests verify that hooks fire in the correct order during create and destroy flows.
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mngr import hookimpl
from imbue.mngr.api.cleanup import execute_cleanup
from imbue.mngr.api.cleanup import find_agents_for_cleanup
from imbue.mngr.api.create import create
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import make_ctx_with_plugins
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr.utils.testing import tmux_session_cleanup


class _AgentHostHookTracker:
    """Test plugin that records lifecycle hook invocations in order."""

    def __init__(self) -> None:
        self.hook_log: list[str] = []
        self.hook_data: dict[str, Any] = {}

    @hookimpl
    def on_before_host_create(
        self, name: HostName, provider_name: ProviderInstanceName, mngr_ctx: MngrContext
    ) -> None:
        self.hook_log.append("on_before_host_create")
        self.hook_data["before_host_create_name"] = name
        self.hook_data["before_host_create_provider"] = provider_name
        self.hook_data["before_host_create_ctx"] = mngr_ctx

    @hookimpl
    def on_host_created(self, host: Any, mngr_ctx: MngrContext) -> None:
        self.hook_log.append("on_host_created")

    @hookimpl
    def on_before_initial_file_copy(self, agent_options: Any, host: Any) -> None:
        self.hook_log.append("on_before_initial_file_copy")

    @hookimpl
    def on_after_initial_file_copy(self, agent_options: Any, host: Any, work_dir_path: Path) -> None:
        self.hook_log.append("on_after_initial_file_copy")
        self.hook_data["work_dir_path"] = work_dir_path

    @hookimpl
    def on_agent_state_dir_created(self, agent: AgentInterface, host: Any) -> None:
        self.hook_log.append("on_agent_state_dir_created")
        self.hook_data["state_dir_agent_name"] = agent.name

    @hookimpl
    def on_before_provisioning(self, agent: AgentInterface, host: Any, mngr_ctx: Any) -> None:
        self.hook_log.append("on_before_provisioning")

    @hookimpl
    def on_after_provisioning(self, agent: AgentInterface, host: Any, mngr_ctx: Any) -> None:
        self.hook_log.append("on_after_provisioning")

    @hookimpl
    def on_agent_created(self, agent: AgentInterface, host: Any) -> None:
        self.hook_log.append("on_agent_created")

    @hookimpl
    def on_before_agent_destroy(self, agent: AgentInterface, host: Any) -> None:
        self.hook_log.append("on_before_agent_destroy")
        self.hook_data["destroy_agent_name"] = agent.name

    @hookimpl
    def on_agent_destroyed(self, agent: AgentInterface, host: Any) -> None:
        self.hook_log.append("on_agent_destroyed")

    @hookimpl
    def on_before_host_destroy(self, host: Any, mngr_ctx: Any) -> None:
        self.hook_log.append("on_before_host_destroy")

    @hookimpl
    def on_host_destroyed(self, host: Any, mngr_ctx: Any) -> None:
        self.hook_log.append("on_host_destroyed")


def _make_tracker_ctx(
    temp_mngr_ctx: MngrContext,
    tracker: _AgentHostHookTracker,
) -> MngrContext:
    """Create a MngrContext with the tracker plugin registered."""
    return make_ctx_with_plugins(temp_mngr_ctx, [tracker], load_backends=True)


def _get_local_host(ctx: MngrContext) -> OnlineHostInterface:
    provider = get_provider_instance(ProviderInstanceName(LOCAL_PROVIDER_NAME), ctx)
    return cast(OnlineHostInterface, provider.get_host(HostName(LOCAL_HOST_NAME)))


# --- Create flow tests ---


@pytest.mark.tmux
def test_create_hooks_fire_in_order_with_existing_host(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Hooks fire in correct order during create with an existing host."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-create-hooks-existing")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("generic"),
                name=agent_name,
                command=CommandString("sleep 482917"),
            ),
            mngr_ctx=ctx,
        )

        assert result.agent is not None

    # With an existing host, no host create hooks should fire
    assert tracker.hook_log == [
        "on_before_initial_file_copy",
        "on_after_initial_file_copy",
        "on_agent_state_dir_created",
        "on_before_provisioning",
        "on_after_provisioning",
        "on_agent_created",
    ]


@pytest.mark.tmux
def test_create_hooks_fire_in_order_with_new_host(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Hooks fire in correct order during create with a new host (includes host hooks)."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)
    agent_name = AgentName("test-create-hooks-new-host")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Use the local provider to determine source location
        host = _get_local_host(ctx)
        # Passing NewHostOptions(provider=local, name=LOCAL_HOST_NAME) forces create()
        # down its "new host" branch even though the local host is a singleton that
        # already exists on disk. For the local provider this is intentional: the
        # new-host branch still fires the host-create hooks against the singleton
        # local host (there is no separate machine to provision), so the asserted
        # ordering below -- host-create hooks before agent hooks -- is the deliberate,
        # documented behaviour for the local provider, not an accident of reusing the
        # singleton.
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=NewHostOptions(
                provider=LOCAL_PROVIDER_NAME,
                name=HostName(LOCAL_HOST_NAME),
            ),
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("generic"),
                name=agent_name,
                command=CommandString("sleep 719283"),
            ),
            mngr_ctx=ctx,
        )

        assert result.agent is not None

    # With a new host, host create hooks should fire before agent hooks
    assert tracker.hook_log == [
        "on_before_host_create",
        "on_host_created",
        "on_before_initial_file_copy",
        "on_after_initial_file_copy",
        "on_agent_state_dir_created",
        "on_before_provisioning",
        "on_after_provisioning",
        "on_agent_created",
    ]


@pytest.mark.tmux
def test_create_hooks_receive_correct_data(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Hook callbacks receive the expected arguments."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)
    agent_name = AgentName("test-create-data")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        host = _get_local_host(ctx)
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=NewHostOptions(
                provider=LOCAL_PROVIDER_NAME,
                name=HostName(LOCAL_HOST_NAME),
            ),
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("generic"),
                name=agent_name,
                command=CommandString("sleep 391847"),
            ),
            mngr_ctx=ctx,
        )

        assert result.agent is not None

    # Verify on_before_host_create received correct args
    assert tracker.hook_data["before_host_create_name"] == HostName(LOCAL_HOST_NAME)
    assert tracker.hook_data["before_host_create_provider"] == ProviderInstanceName(LOCAL_PROVIDER_NAME)
    assert tracker.hook_data["before_host_create_ctx"] is ctx

    # Verify on_agent_state_dir_created received the agent
    assert tracker.hook_data["state_dir_agent_name"] == agent_name

    # Verify on_after_initial_file_copy received a work_dir_path
    assert tracker.hook_data["work_dir_path"] is not None


@pytest.mark.tmux
def test_create_without_work_dir_skips_file_copy_hooks(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """When create_work_dir=False, file copy hooks do not fire."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-no-copy-hooks")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("generic"),
                name=agent_name,
                command=CommandString("sleep 284719"),
            ),
            mngr_ctx=ctx,
            create_work_dir=False,
        )

        assert result.agent is not None

    assert "on_before_initial_file_copy" not in tracker.hook_log
    assert "on_after_initial_file_copy" not in tracker.hook_log
    # Other hooks should still fire
    assert "on_agent_state_dir_created" in tracker.hook_log
    assert "on_before_provisioning" in tracker.hook_log
    assert "on_after_provisioning" in tracker.hook_log
    assert "on_agent_created" in tracker.hook_log


# --- Destroy flow tests ---


@contextmanager
def _injected_provider(
    name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    instance: LocalProviderInstance,
) -> Generator[None, None, None]:
    """Temporarily inject a provider instance into the provider cache.

    execute_cleanup resolves the host's provider via get_provider_instance, which
    consults this cache (keyed by (name, id(mngr_ctx))). Injecting lets a test drive
    the real cleanup destroy path against a custom provider without going over the
    network or touching real infrastructure.
    """
    cache_key = (name, id(mngr_ctx))
    _instance_cache[cache_key] = instance
    try:
        yield
    finally:
        _instance_cache.pop(cache_key, None)


class _OfflineHostDestroyableProvider(LocalProviderInstance):
    """Local provider whose get_host() returns an OfflineHost and whose destroy_host()
    succeeds (no-op).

    Used to drive the real host-destroy path in execute_cleanup: when a cleanup target
    resolves to an offline host, _execute_destroy fires on_before_host_destroy, calls
    provider.destroy_host(), then fires on_host_destroyed. A plain LocalProviderInstance
    cannot be used here because its destroy_host() always raises
    LocalHostNotDestroyableError, which would short-circuit before on_host_destroyed.

    get_host() has no return type annotation because it returns OfflineHost, which
    satisfies HostInterface but is not a subclass of Host (the parent's declared return
    type). Adding a return annotation would produce a type error.
    """

    def get_host(self, host: HostId | HostName):
        host_id = host if isinstance(host, HostId) else HostId.generate()
        now = datetime.now(timezone.utc)
        certified_data = CertifiedHostData(
            created_at=now,
            updated_at=now,
            host_id=str(host_id),
            host_name="test-host-destroy-hooks",
        )
        return OfflineHost(
            id=host_id,
            certified_host_data=certified_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )

    def destroy_host(self, host: HostInterface | HostId) -> None:
        pass


@pytest.mark.tmux
def test_destroy_agent_hooks_fire_in_order(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """on_before_agent_destroy and on_agent_destroyed fire (in order) when the real
    cleanup destroy path destroys an agent on an online host.

    Drives execute_cleanup(CleanupAction.DESTROY) -- the production code that wraps
    host.destroy_agent() with the destroy hooks -- rather than firing the hooks
    manually. A bug such as dropping a hook call, reordering them, or passing the
    wrong agent would therefore be caught here.
    """
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-destroy-hooks")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("generic"),
                name=agent_name,
                command=CommandString("sleep 573918"),
            ),
            mngr_ctx=ctx,
        )

        # Clear the create hooks from the log so we only observe destroy-flow hooks.
        tracker.hook_log.clear()
        tracker.hook_data.clear()

        # Resolve the just-created agent through the real find path, then destroy it
        # through the real cleanup destroy path (which fires the destroy hooks).
        agents = find_agents_for_cleanup(
            mngr_ctx=ctx,
            include_filters=(f'name == "{agent_name}"',),
            exclude_filters=(),
            error_behavior=ErrorBehavior.CONTINUE,
        )
        assert len(agents) == 1

        result = execute_cleanup(
            mngr_ctx=ctx,
            agents=agents,
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )
        assert agent_name in result.destroyed_agents

    assert tracker.hook_log == [
        "on_before_agent_destroy",
        "on_agent_destroyed",
    ]
    # The destroy hooks received the agent we actually destroyed.
    assert tracker.hook_data["destroy_agent_name"] == agent_name


def test_host_destroy_hooks_fire_in_order(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """on_before_host_destroy and on_host_destroyed fire (in order) when the real
    cleanup destroy path destroys an offline host.

    Local online hosts cannot be destroyed (provider.destroy_host raises), so the
    host-destroy hooks only fire for offline hosts. This drives the real
    execute_cleanup(CleanupAction.DESTROY) offline-host branch against a provider
    whose destroy_host() succeeds, exercising the production code that wraps
    provider.destroy_host() with the host-destroy hooks -- rather than firing the
    hooks manually.
    """
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mngr_ctx, tracker)

    provider_name = ProviderInstanceName("offline-host-destroy-provider")
    provider = _OfflineHostDestroyableProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=ctx,
    )

    # The cleanup target references the offline host by id; _execute_destroy groups
    # by host, resolves it via the injected provider, sees an offline host, and runs
    # the host-destruction branch (firing the host-destroy hooks).
    agent_details = make_test_agent_details(
        name="test-host-destroy-agent",
        host_id=HostId.generate(),
        provider_name=provider_name,
    )

    with _injected_provider(provider_name, ctx, provider):
        result = execute_cleanup(
            mngr_ctx=ctx,
            agents=[agent_details],
            action=CleanupAction.DESTROY,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

    assert result.errors == []
    assert AgentName("test-host-destroy-agent") in result.destroyed_agents
    assert tracker.hook_log == [
        "on_before_host_destroy",
        "on_host_destroyed",
    ]
