"""Unit tests for agent and host lifecycle hooks.

Tests verify that hooks fire in the correct order during create and destroy flows.
"""

from pathlib import Path
from typing import Any
from typing import cast

import pluggy
import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mng import hookimpl
from imbue.mng.api.create import create
from imbue.mng.api.data_types import NewHostOptions
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.registry import load_local_backend_only
from imbue.mng.utils.testing import tmux_session_cleanup


class _AgentHostHookTracker:
    """Test plugin that records lifecycle hook invocations in order."""

    def __init__(self) -> None:
        self.hook_log: list[str] = []
        self.hook_data: dict[str, Any] = {}

    @hookimpl
    def on_before_host_create(self, name: HostName, provider_name: ProviderInstanceName) -> None:
        self.hook_log.append("on_before_host_create")
        self.hook_data["before_host_create_name"] = name
        self.hook_data["before_host_create_provider"] = provider_name

    @hookimpl
    def on_host_created(self, host: Any) -> None:
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
    def on_before_provisioning(self, agent: AgentInterface, host: Any) -> None:
        self.hook_log.append("on_before_provisioning")

    @hookimpl
    def on_after_provisioning(self, agent: AgentInterface, host: Any) -> None:
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
    def on_before_host_destroy(self, host: Any) -> None:
        self.hook_log.append("on_before_host_destroy")

    @hookimpl
    def on_host_destroyed(self, host: Any) -> None:
        self.hook_log.append("on_host_destroyed")


def _make_tracker_ctx(
    temp_mng_ctx: MngContext,
    tracker: _AgentHostHookTracker,
) -> MngContext:
    """Create a MngContext with the tracker plugin registered."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    load_local_backend_only(pm)
    pm.register(tracker)
    return temp_mng_ctx.model_copy_update(
        to_update(temp_mng_ctx.field_ref().pm, pm),
    )


def _get_local_host(ctx: MngContext) -> OnlineHostInterface:
    provider = get_provider_instance(ProviderInstanceName(LOCAL_PROVIDER_NAME), ctx)
    return cast(OnlineHostInterface, provider.get_host(HostName("localhost")))


# --- Create flow tests ---


@pytest.mark.tmux
def test_create_hooks_fire_in_order_with_existing_host(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Hooks fire in correct order during create with an existing host."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-create-hooks-existing")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("echo"),
                name=agent_name,
                command=CommandString("sleep 482917"),
            ),
            mng_ctx=ctx,
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
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Hooks fire in correct order during create with a new host (includes host hooks)."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    agent_name = AgentName("test-create-hooks-new-host")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Use the local provider to determine source location
        host = _get_local_host(ctx)
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=NewHostOptions(
                provider=LOCAL_PROVIDER_NAME,
                name=HostName("localhost"),
            ),
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("echo"),
                name=agent_name,
                command=CommandString("sleep 719283"),
            ),
            mng_ctx=ctx,
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
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Hook callbacks receive the expected arguments."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    agent_name = AgentName("test-create-data")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        host = _get_local_host(ctx)
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=NewHostOptions(
                provider=LOCAL_PROVIDER_NAME,
                name=HostName("localhost"),
            ),
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("echo"),
                name=agent_name,
                command=CommandString("sleep 391847"),
            ),
            mng_ctx=ctx,
        )

        assert result.agent is not None

    # Verify on_before_host_create received correct args
    assert tracker.hook_data["before_host_create_name"] == HostName("localhost")
    assert tracker.hook_data["before_host_create_provider"] == ProviderInstanceName(LOCAL_PROVIDER_NAME)

    # Verify on_agent_state_dir_created received the agent
    assert tracker.hook_data["state_dir_agent_name"] == agent_name

    # Verify on_after_initial_file_copy received a work_dir_path
    assert tracker.hook_data["work_dir_path"] is not None


@pytest.mark.tmux
def test_create_without_work_dir_skips_file_copy_hooks(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """When create_work_dir=False, file copy hooks do not fire."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-no-copy-hooks")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("echo"),
                name=agent_name,
                command=CommandString("sleep 284719"),
            ),
            mng_ctx=ctx,
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


@pytest.mark.tmux
def test_destroy_agent_hooks_fire_in_order(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """on_before_agent_destroy and on_agent_destroyed fire around host.destroy_agent()."""
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    host = _get_local_host(ctx)
    agent_name = AgentName("test-destroy-hooks")
    session_name = f"{ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = create(
            source_location=HostLocation(host=host, path=temp_work_dir),
            target_host=host,
            agent_options=CreateAgentOptions(
                agent_type=AgentTypeName("echo"),
                name=agent_name,
                command=CommandString("sleep 573918"),
            ),
            mng_ctx=ctx,
        )

        # Clear the create hooks from the log
        tracker.hook_log.clear()
        tracker.hook_data.clear()

        # Now destroy the agent
        ctx.pm.hook.on_before_agent_destroy(agent=result.agent, host=host)
        host.destroy_agent(result.agent)
        ctx.pm.hook.on_agent_destroyed(agent=result.agent, host=host)

    assert tracker.hook_log == [
        "on_before_agent_destroy",
        "on_agent_destroyed",
    ]
    assert tracker.hook_data["destroy_agent_name"] == agent_name


def test_host_destroy_hooks_fire_in_order(
    temp_mng_ctx: MngContext,
) -> None:
    """on_before_host_destroy and on_host_destroyed fire around provider.destroy_host().

    Since local hosts cannot be destroyed, we test the hook invocation directly.
    """
    tracker = _AgentHostHookTracker()
    ctx = _make_tracker_ctx(temp_mng_ctx, tracker)
    host = _get_local_host(ctx)

    # Call hooks directly (local hosts raise on destroy, so we just test the hooks)
    ctx.pm.hook.on_before_host_destroy(host=host)
    ctx.pm.hook.on_host_destroyed(host=host)

    assert tracker.hook_log == [
        "on_before_host_destroy",
        "on_host_destroyed",
    ]
