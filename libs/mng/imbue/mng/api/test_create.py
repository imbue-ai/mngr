"""Integration tests for the create API.

Note: Unit tests for provider registry and configuration are in api/providers_test.py
"""

import json
import subprocess
import time
from pathlib import Path
from typing import cast

import pluggy
import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mng import hookimpl
from imbue.mng.api.create import _call_on_before_create_hooks
from imbue.mng.api.create import create
from imbue.mng.api.data_types import CreateAgentResult
from imbue.mng.api.data_types import NewHostOptions
from imbue.mng.api.data_types import OnBeforeCreateArgs
from imbue.mng.api.providers import get_provider_instance
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import HostLocation
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists

# =============================================================================
# Create API Integration Tests
# =============================================================================


def _get_local_host_for_test(test_ctx: MngContext) -> OnlineHostInterface:
    local_provider = get_provider_instance(ProviderInstanceName(LOCAL_PROVIDER_NAME), test_ctx)
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))
    return local_host


def _get_local_host_and_location(
    temp_mng_ctx: MngContext, temp_work_dir: Path
) -> tuple[OnlineHostInterface, HostLocation]:
    local_host = _get_local_host_for_test(temp_mng_ctx)
    source_location = HostLocation(
        host=local_host,
        path=temp_work_dir,
    )
    return local_host, source_location


def _get_agent_from_create_result(result: CreateAgentResult, temp_mng_ctx: MngContext) -> AgentInterface:
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, temp_mng_ctx)
    host = cast(OnlineHostInterface, provider.get_host(result.host.id))
    agents = host.get_agents()
    agent = next((a for a in agents if a.id == result.agent.id), None)
    assert agent is not None
    return agent


def _setup_claude_trust_config(work_dir: Path, tmp_home_dir: Path) -> None:
    """Create a Claude trust config marking work_dir as trusted.

    Since the autouse setup_test_mng_env fixture sets HOME to a temp directory,
    this ends up writing ~/.claude.json for the home dir set and used in setup_test_mng_env
    """
    claude_config = {
        "effortCalloutDismissed": True,
        "projects": {
            str(work_dir): {"allowedTools": [], "hasTrustDialogAccepted": True},
        },
    }
    (tmp_home_dir / ".claude.json").write_text(json.dumps(claude_config))


@pytest.mark.tmux
def test_create_simple_echo_agent(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test creating a simple agent that runs echo."""
    agent_name = AgentName(f"test-echo-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("echo"),
            name=agent_name,
            command=CommandString("echo 'Hello from mng test' && sleep 365817"),
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        assert result.agent is not None
        assert result.host is not None
        assert result.agent.id is not None
        assert result.host.id is not None
        assert len(str(result.agent.id)) > 0
        assert len(str(result.host.id)) > 0
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"


@pytest.mark.tmux
def test_create_agent_with_new_host(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test creating an agent with explicit new host options."""
    agent_name = AgentName(f"test-new-host-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("echo"),
            name=agent_name,
            command=CommandString("echo 'Created with new host' && sleep 816394"),
        )

        target_host = NewHostOptions(
            provider=LOCAL_PROVIDER_NAME,
            name=HostName("localhost"),
        )

        result = create(
            source_location=source_location,
            target_host=target_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        assert result.agent.id is not None
        assert result.host.id is not None
        assert tmux_session_exists(session_name)


@pytest.mark.tmux
def test_create_agent_work_dir_is_created(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that the agent work_dir directory is used."""
    agent_name = AgentName(f"test-work-dir-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        marker_file = temp_work_dir / "test_marker.txt"
        marker_file.write_text("work_dir marker")

        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("test"),
            name=agent_name,
            command=CommandString("cat test_marker.txt && sleep 30"),
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        assert result.agent.id is not None
        assert result.host.id is not None


@pytest.mark.tmux
def test_agent_state_is_persisted(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
) -> None:
    """Test that agent state is persisted to disk."""
    agent_name = AgentName(f"test-persist-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("persist-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        agents_dir = local_host.host_dir / "agents"
        assert agents_dir.exists(), "agents directory should exist"

        agent_dirs = list(agents_dir.iterdir())
        assert len(agent_dirs) > 0, "should have at least one agent directory"

        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["id"] == str(result.agent.id)
        assert data["name"] == str(agent_name)
        assert data["type"] == "persist-test"


# =============================================================================
# Edge Cases
# =============================================================================


@pytest.mark.tmux
def test_create_agent_with_unknown_type_uses_type_as_command(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that creating an agent with an unknown type uses the type name as the command.

    This verifies the documented "Direct command" fallback behavior where an unrecognized
    agent type (e.g., 'echo') is treated as a command to run.
    """
    agent_name = AgentName(f"test-direct-cmd-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        # Use a custom agent type name that will be treated as a command
        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("my-custom-command"),
            name=agent_name,
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        # The agent should be created successfully
        assert result.agent.id is not None
        assert result.host.id is not None
        # The command should be the agent type name since no explicit command was provided
        assert result.agent.get_command() == "my-custom-command"


# =============================================================================
# Worktree Tests
# =============================================================================


@pytest.mark.tmux
@pytest.mark.git
def test_create_agent_with_worktree(
    temp_mng_ctx: MngContext,
    temp_git_repo: Path,
) -> None:
    """Test creating an agent using git worktree."""
    agent_name = AgentName(f"test-worktree-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    worktree_path: Path | None = None
    with tmux_session_cleanup(session_name):
        try:
            local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_git_repo)

            agent_options = CreateAgentOptions(
                agent_type=AgentTypeName("worktree-test"),
                name=agent_name,
                command=CommandString("sleep 527146"),
                git=AgentGitOptions(copy_mode=WorkDirCopyMode.WORKTREE),
            )

            result = create(
                source_location=source_location,
                target_host=local_host,
                agent_options=agent_options,
                mng_ctx=temp_mng_ctx,
            )

            assert result.agent.id is not None
            assert result.host.id is not None
            assert tmux_session_exists(session_name)

            agent = _get_agent_from_create_result(result, temp_mng_ctx)

            worktree_path = Path(agent.work_dir)
            assert worktree_path.exists()
            assert (worktree_path / "README.md").exists()

            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )
            branch_name = result.stdout.strip()
            assert branch_name.startswith("mng/")
            assert str(agent_name) in branch_name
        finally:
            if worktree_path is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=temp_git_repo,
                    capture_output=True,
                )


@pytest.mark.tmux
@pytest.mark.git
def test_worktree_with_custom_branch_name(
    tmp_home_dir: Path,
    temp_mng_ctx: MngContext,
    temp_git_repo: Path,
) -> None:
    """Test creating a worktree with a custom branch name."""
    agent_name = AgentName(f"test-worktree-custom-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"
    custom_branch = "feature/custom-branch"

    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    current_branch = branch_result.stdout.strip()

    _setup_claude_trust_config(temp_git_repo, tmp_home_dir)
    worktree_path: Path | None = None
    with tmux_session_cleanup(session_name):
        try:
            local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_git_repo)

            agent_options = CreateAgentOptions(
                agent_type=AgentTypeName("worktree-test"),
                name=agent_name,
                command=CommandString("sleep 60"),
                git=AgentGitOptions(
                    copy_mode=WorkDirCopyMode.WORKTREE,
                    base_branch=current_branch,
                    is_new_branch=True,
                    new_branch_name=custom_branch,
                ),
            )

            result = create(
                source_location=source_location,
                target_host=local_host,
                agent_options=agent_options,
                mng_ctx=temp_mng_ctx,
            )

            assert result.agent.id is not None

            agent = _get_agent_from_create_result(result, temp_mng_ctx)

            worktree_path = Path(agent.work_dir)
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )
            branch_name = result.stdout.strip()
            assert branch_name == custom_branch
        finally:
            if worktree_path is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=temp_git_repo,
                    capture_output=True,
                )


# =============================================================================
# is_generated_work_dir Tests
# =============================================================================


@pytest.mark.tmux
def test_in_place_mode_sets_is_generated_work_dir_false(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
) -> None:
    """Test that in-place mode does not track work_dir as generated."""
    agent_name = AgentName(f"test-in-place-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("in-place-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        agents_dir = local_host.host_dir / "agents"
        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["work_dir"] == str(temp_work_dir), "work_dir should be the source work_dir"

        host_data_file = local_host.host_dir / "data.json"
        host_data = json.loads(host_data_file.read_text()) if host_data_file.exists() else {}
        generated_work_dirs = host_data.get("generated_work_dirs", [])
        assert str(temp_work_dir) not in generated_work_dirs, (
            "work_dir should not be in generated_work_dirs for in-place mode"
        )


@pytest.mark.tmux
@pytest.mark.git
def test_worktree_mode_sets_is_generated_work_dir_true(
    tmp_home_dir: Path,
    temp_mng_ctx: MngContext,
    temp_git_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Test that worktree mode tracks work_dir as generated."""
    agent_name = AgentName(f"test-worktree-gen-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    _setup_claude_trust_config(temp_git_repo, tmp_home_dir)
    worktree_path: Path | None = None
    with tmux_session_cleanup(session_name):
        try:
            local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_git_repo)

            agent_options = CreateAgentOptions(
                agent_type=AgentTypeName("worktree-gen-test"),
                name=agent_name,
                command=CommandString("sleep 60"),
                git=AgentGitOptions(copy_mode=WorkDirCopyMode.WORKTREE),
            )

            result = create(
                source_location=source_location,
                target_host=local_host,
                agent_options=agent_options,
                mng_ctx=temp_mng_ctx,
            )

            agents_dir = local_host.host_dir / "agents"
            agent_dir = agents_dir / str(result.agent.id)
            data_file = agent_dir / "data.json"
            assert data_file.exists(), "agent data.json should exist"

            data = json.loads(data_file.read_text())
            assert data["work_dir"] != str(temp_git_repo), "work_dir should be different from source in worktree mode"

            agent = _get_agent_from_create_result(result, temp_mng_ctx)
            worktree_path = Path(agent.work_dir)

            host_data_file = local_host.host_dir / "data.json"
            assert host_data_file.exists(), "host data.json should exist"
            host_data = json.loads(host_data_file.read_text())
            generated_work_dirs = host_data.get("generated_work_dirs", [])
            assert str(worktree_path) in generated_work_dirs, (
                "work_dir should be in generated_work_dirs for worktree mode"
            )
        finally:
            if worktree_path is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=temp_git_repo,
                    capture_output=True,
                )


@pytest.mark.tmux
def test_target_path_different_from_source_sets_is_generated_work_dir_true(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that specifying a different target path tracks work_dir as generated."""
    agent_name = AgentName(f"test-target-diff-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"
    target_dir = tmp_path / "different_target"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("target-diff-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
            target_path=target_dir,
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        agents_dir = local_host.host_dir / "agents"
        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["work_dir"] == str(target_dir), "work_dir should be the target path"

        host_data_file = local_host.host_dir / "data.json"
        assert host_data_file.exists(), "host data.json should exist"
        host_data = json.loads(host_data_file.read_text())
        generated_work_dirs = host_data.get("generated_work_dirs", [])
        assert str(target_dir) in generated_work_dirs, (
            "work_dir should be in generated_work_dirs when target differs from source"
        )


@pytest.mark.tmux
def test_target_path_same_as_source_sets_is_generated_work_dir_false(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
) -> None:
    """Test that specifying the same target as source path does not track work_dir as generated."""
    agent_name = AgentName(f"test-target-same-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("target-same-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
            target_path=temp_work_dir,
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
        )

        agents_dir = local_host.host_dir / "agents"
        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["work_dir"] == str(temp_work_dir), "work_dir should be the source/target path"

        host_data_file = local_host.host_dir / "data.json"
        host_data = json.loads(host_data_file.read_text()) if host_data_file.exists() else {}
        generated_work_dirs = host_data.get("generated_work_dirs", [])
        assert str(temp_work_dir) not in generated_work_dirs, (
            "work_dir should not be in generated_work_dirs when target equals source"
        )


# =============================================================================
# create_work_dir=False Tests
# =============================================================================


@pytest.mark.tmux
def test_create_work_dir_false_uses_target_path(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Test that when create_work_dir=False, the agent's work_dir is set to target_path, not source_path."""
    agent_name = AgentName(f"test-no-create-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"
    target_dir = tmp_path / "target_work_dir"
    target_dir.mkdir(parents=True)

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("no-create-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
            target_path=target_dir,
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
            create_work_dir=False,
        )

        agents_dir = local_host.host_dir / "agents"
        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["work_dir"] == str(target_dir), (
            f"work_dir should be target_dir ({target_dir}), not source_path ({temp_work_dir})"
        )


@pytest.mark.tmux
def test_create_work_dir_false_without_target_path_uses_source(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    temp_host_dir: Path,
) -> None:
    """Test that when create_work_dir=False and target_path is None, the source path is used."""
    agent_name = AgentName(f"test-no-create-src-{int(time.time())}")
    session_name = f"{temp_mng_ctx.config.prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        local_host, source_location = _get_local_host_and_location(temp_mng_ctx, temp_work_dir)

        agent_options = CreateAgentOptions(
            agent_type=AgentTypeName("no-create-src-test"),
            name=agent_name,
            command=CommandString("sleep 60"),
        )

        result = create(
            source_location=source_location,
            target_host=local_host,
            agent_options=agent_options,
            mng_ctx=temp_mng_ctx,
            create_work_dir=False,
        )

        agents_dir = local_host.host_dir / "agents"
        agent_dir = agents_dir / str(result.agent.id)
        data_file = agent_dir / "data.json"
        assert data_file.exists(), "agent data.json should exist"

        data = json.loads(data_file.read_text())
        assert data["work_dir"] == str(temp_work_dir), "work_dir should be the source path when target_path is None"


# =============================================================================
# on_before_create Hook Tests
# =============================================================================


class PluginModifyingAgentOptions:
    """Test plugin that modifies agent_options."""

    @hookimpl
    def on_before_create(self, args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
        # Modify the agent name by adding a prefix
        new_options = args.agent_options.model_copy_update(
            to_update(args.agent_options.field_ref().name, AgentName(f"modified-{args.agent_options.name}")),
        )
        return args.model_copy_update(
            to_update(args.field_ref().agent_options, new_options),
        )


class PluginModifyingCreateWorkDir:
    """Test plugin that modifies create_work_dir."""

    @hookimpl
    def on_before_create(self, args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
        # Force create_work_dir to False
        return args.model_copy_update(
            to_update(args.field_ref().create_work_dir, False),
        )


class PluginReturningNone:
    """Test plugin that returns None (passes through unchanged)."""

    @hookimpl
    def on_before_create(self, args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
        return None


class PluginChainA:
    """First plugin in a chain test - adds 'A' to agent name."""

    @hookimpl
    def on_before_create(self, args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
        new_name = AgentName(f"{args.agent_options.name}-A")
        new_options = args.agent_options.model_copy_update(
            to_update(args.agent_options.field_ref().name, new_name),
        )
        return args.model_copy_update(
            to_update(args.field_ref().agent_options, new_options),
        )


class PluginChainB:
    """Second plugin in a chain test - adds 'B' to agent name."""

    @hookimpl
    def on_before_create(self, args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
        new_name = AgentName(f"{args.agent_options.name}-B")
        new_options = args.agent_options.model_copy_update(
            to_update(args.agent_options.field_ref().name, new_name),
        )
        return args.model_copy_update(
            to_update(args.field_ref().agent_options, new_options),
        )


def test_on_before_create_hook_modifies_agent_options(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that on_before_create hook can modify agent_options."""
    # Create a new plugin manager with our test plugin
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(PluginModifyingAgentOptions())

    # Create a modified context with our test plugin manager
    test_ctx = temp_mng_ctx.model_copy_update(
        to_update(temp_mng_ctx.field_ref().pm, pm),
    )

    local_host = _get_local_host_for_test(test_ctx)

    agent_options = CreateAgentOptions(
        agent_type=AgentTypeName("test"),
        name=AgentName("original-name"),
        command=CommandString("sleep 1"),
    )

    # Call the hook helper directly to verify modification
    target_host, modified_options, create_work_dir = _call_on_before_create_hooks(
        test_ctx, local_host, agent_options, True
    )

    # The plugin should have modified the name
    assert modified_options.name == AgentName("modified-original-name")
    assert create_work_dir is True


def test_on_before_create_hook_modifies_create_work_dir(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that on_before_create hook can modify create_work_dir."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(PluginModifyingCreateWorkDir())

    test_ctx = temp_mng_ctx.model_copy_update(
        to_update(temp_mng_ctx.field_ref().pm, pm),
    )

    local_host = _get_local_host_for_test(test_ctx)

    agent_options = CreateAgentOptions(
        agent_type=AgentTypeName("test"),
        name=AgentName("test-agent"),
        command=CommandString("sleep 1"),
    )

    # Call with create_work_dir=True, plugin should change it to False
    target_host, modified_options, create_work_dir = _call_on_before_create_hooks(
        test_ctx, local_host, agent_options, True
    )

    assert create_work_dir is False


def test_on_before_create_hook_returning_none_passes_through(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that on_before_create returning None passes values unchanged."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.register(PluginReturningNone())

    test_ctx = temp_mng_ctx.model_copy_update(
        to_update(temp_mng_ctx.field_ref().pm, pm),
    )

    local_host = _get_local_host_for_test(test_ctx)

    original_name = AgentName("unchanged-name")
    agent_options = CreateAgentOptions(
        agent_type=AgentTypeName("test"),
        name=original_name,
        command=CommandString("sleep 1"),
    )

    target_host, modified_options, create_work_dir = _call_on_before_create_hooks(
        test_ctx, local_host, agent_options, True
    )

    # Values should be unchanged
    assert modified_options.name == original_name
    assert create_work_dir is True


def test_on_before_create_hooks_chain_in_order(
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that multiple on_before_create hooks chain properly."""
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    # Register plugins in order A, B
    pm.register(PluginChainA())
    pm.register(PluginChainB())

    test_ctx = temp_mng_ctx.model_copy_update(
        to_update(temp_mng_ctx.field_ref().pm, pm),
    )

    local_host = _get_local_host_for_test(test_ctx)

    agent_options = CreateAgentOptions(
        agent_type=AgentTypeName("test"),
        name=AgentName("base"),
        command=CommandString("sleep 1"),
    )

    target_host, modified_options, create_work_dir = _call_on_before_create_hooks(
        test_ctx, local_host, agent_options, True
    )

    # Both plugins should have modified the name in order
    # A adds "-A", then B adds "-B" to that result
    assert modified_options.name == AgentName("base-A-B")


# Note: Agent provisioning lifecycle tests (on_before_provisioning, get_provision_file_transfers,
# provision, on_after_provisioning) are covered by agent-type specific tests since these are
# methods on the agent class rather than plugin hooks. See the "Provisioning Lifecycle Tests"
# section in claude_agent_test.py.
