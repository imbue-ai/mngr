import os
import queue as queue_mod
import threading
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import WELCOME_INITIAL_MESSAGE
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _make_host_name
from imbue.minds.desktop_client.agent_creator import checkout_branch
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.agent_creator import make_log_callback
from imbue.minds.desktop_client.agent_creator import run_mngr_create
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.minds.testing import add_and_commit_git_repo
from imbue.minds.testing import init_and_commit_git_repo
from imbue.mngr.primitives import AgentId


def test_extract_repo_name_from_https_url() -> None:
    assert extract_repo_name("https://github.com/user/my-repo.git") == "my-repo"


def test_extract_repo_name_from_ssh_url() -> None:
    assert extract_repo_name("git@github.com:user/my-repo.git") == "my-repo"


def test_extract_repo_name_strips_trailing_slash() -> None:
    assert extract_repo_name("https://github.com/user/my-repo/") == "my-repo"


def test_extract_repo_name_without_git_suffix() -> None:
    assert extract_repo_name("https://github.com/user/my-repo") == "my-repo"


def test_extract_repo_name_replaces_special_chars() -> None:
    assert extract_repo_name("https://github.com/user/my repo!test") == "my-repo-test"


def test_extract_repo_name_falls_back_to_workspace() -> None:
    assert extract_repo_name("") == "workspace"
    assert extract_repo_name("/") == "workspace"
    assert extract_repo_name(".git") == "workspace"


def test_extract_repo_name_from_local_path() -> None:
    assert extract_repo_name("/home/user/my-template") == "my-template"
    assert extract_repo_name("~/project/forever-claude") == "forever-claude"


# -- _is_local_path tests --


def test_is_local_path_absolute() -> None:
    assert _is_local_path("/home/user/repo") is True


def test_is_local_path_relative() -> None:
    assert _is_local_path("./my-repo") is True


def test_is_local_path_tilde() -> None:
    assert _is_local_path("~/project/repo") is True


def test_is_local_path_url() -> None:
    assert _is_local_path("https://github.com/user/repo.git") is False
    assert _is_local_path("git@github.com:user/repo.git") is False


# -- _build_mngr_create_command tests --


def test_make_host_name() -> None:
    assert _make_host_name(AgentName("my-mind")) == "my-mind-host"


def test_build_mngr_create_command_dev_mode() -> None:
    cmd, api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.DEV,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "dev" in cmd
    assert "main" in cmd
    assert "--no-connect" in cmd
    assert "--reuse" in cmd
    assert "--update" in cmd
    assert "docker" not in cmd
    # DEV mode: address is just the agent name (no host suffix)
    assert cmd[2] == "test-agent"
    # API key is injected via --env
    assert "--env" in cmd
    env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--env"]
    assert any(v.startswith("MINDS_API_KEY=") for v in env_values)
    assert len(api_key) > 0
    # DEV mode runs on localhost: no host-env flags (the agent inherits the
    # local bootstrap-set env directly).
    assert "--host-env" not in cmd
    assert "--pass-host-env" not in cmd


def test_build_mngr_create_command_local_mode() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.LOCAL,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "docker" in cmd
    assert "main" in cmd
    assert "--reuse" in cmd
    assert "--update" in cmd
    assert "--new-host" in cmd
    assert "--idle-mode" in cmd
    assert cmd[cmd.index("--idle-mode") + 1] == "disabled"
    # LOCAL mode: address includes host name with docker provider suffix
    assert cmd[2] == "test-agent@test-agent-host.docker"
    # Remote host: MNGR_HOST_DIR forced to /mngr (container convention),
    # MNGR_PREFIX forwarded from the local shell for naming consistency.
    host_env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--host-env"]
    assert "MNGR_HOST_DIR=/mngr" in host_env_values
    pass_host_env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--pass-host-env"]
    assert "MNGR_PREFIX" in pass_host_env_values
    # We do NOT forward the local MNGR_HOST_DIR -- that's a local filesystem
    # path that doesn't exist inside the container.
    assert "MNGR_HOST_DIR" not in pass_host_env_values


def test_build_mngr_create_command_lima_mode() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.LIMA,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "lima" in cmd
    assert "main" in cmd
    assert "--reuse" in cmd
    assert "--update" in cmd
    assert "--new-host" in cmd
    assert "--idle-mode" in cmd
    assert cmd[cmd.index("--idle-mode") + 1] == "disabled"
    # LIMA mode: address includes host name with lima provider suffix
    assert cmd[2] == "test-agent@test-agent-host.lima"
    host_env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--host-env"]
    assert "MNGR_HOST_DIR=/mngr" in host_env_values
    assert "MNGR_PREFIX" in [cmd[i + 1] for i, v in enumerate(cmd) if v == "--pass-host-env"]


def test_build_mngr_create_command_adds_welcome_initial_message() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.DEV,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--message" in cmd
    # The welcome message is sent as the very first user prompt so the agent
    # renders a greeting without any other user interaction. We deliberately
    # use the `/welcome` skill invocation (see `.agents/skills/welcome/`
    # in forever-claude-template) so the template owns the greeting text
    # and every new agent shows the same polished first response.
    #
    # An earlier iteration of this test asserted the message MUST NOT start
    # with "/" out of concern that Claude Code would intercept slash
    # commands locally and skip the `UserPromptSubmit` hook that mngr's
    # send_message waits on, causing a create-time hang. Verified
    # empirically against Claude Code v2.1.116 that the hook does fire for
    # both recognized skills and unknown slash commands, so the guard was
    # guarding against a failure mode that no longer manifests.
    assert cmd[cmd.index("--message") + 1] == WELCOME_INITIAL_MESSAGE
    assert cmd[cmd.index("--message") + 1] == "/welcome"


def test_build_mngr_create_command_with_host_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\n")
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.LOCAL,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
        host_env_file=env_path,
    )
    assert "--host-env-file" in cmd
    assert cmd[cmd.index("--host-env-file") + 1] == str(env_path)


def test_build_mngr_create_command_omits_host_env_file_by_default() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.LOCAL,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--host-env-file" not in cmd


def test_build_mngr_create_command_cloud_mode() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.CLOUD,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "vultr" in cmd
    assert "main" in cmd
    assert "--reuse" in cmd
    assert "--update" in cmd
    assert "--new-host" in cmd
    assert "--idle-mode" in cmd
    assert cmd[cmd.index("--idle-mode") + 1] == "disabled"
    # CLOUD mode: address includes host name with vultr provider suffix
    assert cmd[2] == "test-agent@test-agent-host.vultr"
    host_env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--host-env"]
    assert "MNGR_HOST_DIR=/mngr" in host_env_values
    assert "MNGR_PREFIX" in [cmd[i + 1] for i, v in enumerate(cmd) if v == "--pass-host-env"]


# -- clone_git_repo tests --


def test_clone_git_repo_clones_local_repo(tmp_path: Path) -> None:
    """Verify clone_git_repo can clone a local git repo."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    init_and_commit_git_repo(source, tmp_path)

    dest = tmp_path / "dest"
    clone_git_repo(GitUrl(str(source)), dest)

    assert dest.exists()
    assert (dest / "hello.txt").read_text() == "hello"


def test_clone_git_repo_raises_on_bad_url(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(GitCloneError, match="git clone failed"):
        clone_git_repo(GitUrl("/nonexistent/path"), dest)


def test_clone_git_repo_with_branch(tmp_path: Path) -> None:
    """Verify clone_git_repo can clone a specific branch, even shallow."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    init_and_commit_git_repo(source, tmp_path)

    # Create a feature branch with a new file
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    cg = ConcurrencyGroup(name="test-branch")
    with cg:
        cg.run_process_to_completion(["git", "checkout", "-b", "feature-x"], cwd=source)
    (source / "feature.txt").write_text("feature")
    cg = ConcurrencyGroup(name="test-commit")
    with cg:
        cg.run_process_to_completion(["git", "add", "."], cwd=source)
        cg.run_process_to_completion(["git", "commit", "-m", "feature"], cwd=source, env=git_env)

    # Clone with branch specified (don't need to switch back to the default
    # branch -- we only care that the clone gets feature-x)
    dest = tmp_path / "dest"
    clone_git_repo(GitUrl(str(source)), dest, branch="feature-x")

    assert dest.exists()
    assert (dest / "feature.txt").read_text() == "feature"


# -- checkout_branch tests --


def test_checkout_branch_switches_to_existing_branch(tmp_path: Path) -> None:
    """Verify checkout_branch can switch to an existing branch in a cloned repo."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    init_and_commit_git_repo(source, tmp_path)

    # Create a branch in the source repo with a unique file
    cg_create = ConcurrencyGroup(name="test-branch-create")
    with cg_create:
        cg_create.run_process_to_completion(command=["git", "checkout", "-b", "test/feature-branch-84923"], cwd=source)
    (source / "feature.txt").write_text("feature")
    add_and_commit_git_repo(source, tmp_path, message="add feature")

    # Switch back to the default branch so that clone doesn't land on the feature branch
    cg_switch = ConcurrencyGroup(name="test-branch-switch")
    with cg_switch:
        cg_switch.run_process_to_completion(
            command=["git", "checkout", "-"],
            cwd=source,
        )

    # Clone and checkout the branch
    dest = tmp_path / "dest"
    clone_git_repo(GitUrl(str(source)), dest)

    # The feature file should NOT be present on the default branch
    assert not (dest / "feature.txt").exists()

    checkout_branch(dest, GitBranch("test/feature-branch-84923"))

    # After checkout, the feature file should be present
    assert (dest / "feature.txt").read_text() == "feature"


def test_checkout_branch_raises_on_nonexistent_branch(tmp_path: Path) -> None:
    """Verify checkout_branch raises GitOperationError for a missing branch."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    init_and_commit_git_repo(source, tmp_path)

    dest = tmp_path / "dest"
    clone_git_repo(GitUrl(str(source)), dest)

    with pytest.raises(GitOperationError, match="git checkout failed"):
        checkout_branch(dest, GitBranch("nonexistent/branch-72391"))


# -- AgentCreator tests --


def test_agent_creator_get_creation_info_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
    )
    assert creator.get_creation_info(AgentId()) is None


def test_agent_creator_start_creation_returns_agent_id_and_tracks_status(tmp_path: Path) -> None:
    """Verify start_creation returns an agent ID and sets initial CLONING status.

    The actual background thread will fail (since the git URL is invalid),
    but the initial status should be immediately available.
    """
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
    )

    agent_id = creator.start_creation("file:///nonexistent-repo")
    info = creator.get_creation_info(agent_id)

    assert info is not None
    assert info.agent_id == agent_id
    assert info.status == AgentCreationStatus.CLONING
    creator.wait_for_all()


def test_agent_creator_start_creation_with_custom_name(tmp_path: Path) -> None:
    """Verify start_creation accepts a custom agent name."""
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo", agent_name="my-agent")
    info = creator.get_creation_info(agent_id)
    assert info is not None
    creator.wait_for_all()


def test_agent_creator_get_log_queue_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
    )
    assert creator.get_log_queue(AgentId()) is None


def test_agent_creator_get_log_queue_returns_queue_for_tracked() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo")
    q = creator.get_log_queue(agent_id)
    assert q is not None
    creator.wait_for_all()


def test_agent_creator_start_creation_with_local_path(tmp_path: Path) -> None:
    """Verify start_creation with a nonexistent local path eventually reaches FAILED status."""
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
    )
    agent_id = creator.start_creation("/nonexistent/local/path", agent_name="local-test")
    # The background thread runs immediately and fails because the path doesn't exist.
    # Wait for it to finish.
    for _ in range(50):
        info = creator.get_creation_info(agent_id)
        if info is not None and info.status == AgentCreationStatus.FAILED:
            break
        threading.Event().wait(0.1)
    info = creator.get_creation_info(agent_id)
    assert info is not None
    assert info.status == AgentCreationStatus.FAILED


@pytest.mark.timeout(30)
def test_run_mngr_create_raises_on_failure(tmp_path: Path) -> None:
    """Verify run_mngr_create raises MngrCommandError when mngr create fails."""
    with pytest.raises(MngrCommandError, match="mngr create failed"):
        run_mngr_create(
            launch_mode=LaunchMode.DEV,
            workspace_dir=tmp_path,
            agent_name=AgentName("test"),
            agent_id=AgentId(),
        )


def test_make_log_callback_puts_lines_into_queue() -> None:
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()
    callback = make_log_callback(log_queue)
    callback("hello\n", True)
    callback("world\n", False)
    assert log_queue.get_nowait() == "hello"
    assert log_queue.get_nowait() == "world"
