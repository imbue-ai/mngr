import queue as queue_mod
import threading
from pathlib import Path
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import MindPaths
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.forwarding_server.agent_creator import AgentCreationStatus
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.agent_creator import checkout_branch
from imbue.minds.forwarding_server.agent_creator import clone_git_repo
from imbue.minds.forwarding_server.agent_creator import _build_mngr_create_command
from imbue.minds.forwarding_server.agent_creator import _is_local_path
from imbue.minds.forwarding_server.cloudflare_client import CloudflareForwardingClient
from imbue.minds.forwarding_server.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.forwarding_server.cloudflare_client import CloudflareSecret
from imbue.minds.forwarding_server.cloudflare_client import CloudflareUsername
from imbue.minds.forwarding_server.cloudflare_client import OwnerEmail
from imbue.minds.forwarding_server.agent_creator import extract_repo_name
from imbue.minds.forwarding_server.agent_creator import make_log_callback
from imbue.minds.forwarding_server.agent_creator import run_mngr_create
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
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


def test_extract_repo_name_falls_back_to_mind() -> None:
    assert extract_repo_name("") == "mind"
    assert extract_repo_name("/") == "mind"
    assert extract_repo_name(".git") == "mind"


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


def test_build_mngr_create_command_dev_mode() -> None:
    cmd = _build_mngr_create_command(
        launch_mode=LaunchMode.DEV,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "dev" in cmd
    assert "main" in cmd
    assert "--no-connect" in cmd
    assert "docker" not in cmd


def test_build_mngr_create_command_local_mode() -> None:
    cmd = _build_mngr_create_command(
        launch_mode=LaunchMode.LOCAL,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    assert "--template" in cmd
    assert "docker" in cmd
    assert "main" in cmd


def test_build_mngr_create_command_cloud_mode_raises() -> None:
    with pytest.raises(NotImplementedError, match="Cloud launch mode"):
        _build_mngr_create_command(
            launch_mode=LaunchMode.CLOUD,
            agent_name=AgentName("test-agent"),
            agent_id=AgentId(),
        )


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
        paths=MindPaths(data_dir=Path("/tmp/test")),
    )
    assert creator.get_creation_info(AgentId()) is None


def test_agent_creator_start_creation_returns_agent_id_and_tracks_status(tmp_path: Path) -> None:
    """Verify start_creation returns an agent ID and sets initial CLONING status.

    The actual background thread will fail (since the git URL is invalid),
    but the initial status should be immediately available.
    """
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
    )

    agent_id = creator.start_creation("file:///nonexistent-repo")
    info = creator.get_creation_info(agent_id)

    assert info is not None
    assert info.agent_id == agent_id
    assert info.status == AgentCreationStatus.CLONING


def test_agent_creator_start_creation_with_custom_name(tmp_path: Path) -> None:
    """Verify start_creation accepts a custom agent name."""
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo", agent_name="my-agent")
    info = creator.get_creation_info(agent_id)
    assert info is not None


def test_agent_creator_get_log_queue_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=MindPaths(data_dir=Path("/tmp/test")),
    )
    assert creator.get_log_queue(AgentId()) is None


def test_agent_creator_get_log_queue_returns_queue_for_tracked() -> None:
    creator = AgentCreator(
        paths=MindPaths(data_dir=Path("/tmp/test")),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo")
    q = creator.get_log_queue(agent_id)
    assert q is not None


def test_agent_creator_start_creation_with_local_path(tmp_path: Path) -> None:
    """Verify start_creation with a nonexistent local path eventually reaches FAILED status."""
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
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


def test_setup_cloudflare_tunnel_skips_when_no_client(tmp_path: Path) -> None:
    """Verify _setup_cloudflare_tunnel does nothing when cloudflare_client is None."""
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
    )
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()
    creator._setup_cloudflare_tunnel(AgentId(), log_queue)
    messages = []
    while not log_queue.empty():
        messages.append(log_queue.get_nowait())
    assert any("not configured" in m for m in messages)


def test_setup_cloudflare_tunnel_with_client_logs_creation(tmp_path: Path) -> None:
    """Verify _setup_cloudflare_tunnel calls the client and logs progress."""
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        username=CloudflareUsername("testuser"),
        secret=CloudflareSecret("testsecret"),
        owner_email=OwnerEmail("test@example.com"),
    )
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
        cloudflare_client=client,
    )
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()
    creator._setup_cloudflare_tunnel(AgentId(), log_queue)
    messages = []
    while not log_queue.empty():
        messages.append(log_queue.get_nowait())
    assert any("Creating Cloudflare tunnel" in m for m in messages)
    assert any("WARNING" in m or "failed" in m.lower() for m in messages)


def test_run_mngr_create_raises_on_failure(tmp_path: Path) -> None:
    """Verify run_mngr_create raises MngrCommandError when mngr create fails."""
    with pytest.raises(MngrCommandError, match="mngr create failed"):
        run_mngr_create(
            launch_mode=LaunchMode.DEV,
            mind_dir=tmp_path,
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
