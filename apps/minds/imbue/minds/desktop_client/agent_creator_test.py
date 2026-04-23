import queue as queue_mod
import threading
import tomllib
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _load_lease_info
from imbue.minds.desktop_client.agent_creator import _load_or_create_leased_host_keypair
from imbue.minds.desktop_client.agent_creator import _make_host_name
from imbue.minds.desktop_client.agent_creator import _remove_dynamic_host_entry
from imbue.minds.desktop_client.agent_creator import _remove_lease_info
from imbue.minds.desktop_client.agent_creator import _save_lease_info
from imbue.minds.desktop_client.agent_creator import _write_dynamic_host_entry
from imbue.minds.desktop_client.agent_creator import checkout_branch
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.agent_creator import make_log_callback
from imbue.minds.desktop_client.agent_creator import run_mngr_create
from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
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
    # The welcome message is sent as the very first user prompt so a /welcome
    # skill can produce a greeting without any other user interaction.
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


def test_agent_creator_accepts_server_port(tmp_path: Path) -> None:
    """AgentCreator exposes its configured server_port for redirect-URL construction.

    Regression guard: the happy-path redirect URL for a newly-created agent is
    built as ``http://<agent-id>.localhost:<server_port>/`` inside the creation
    thread. Earlier iterations of this branch emitted ``/forwarding/<id>/`` which
    404'd after the legacy forwarding routes were deleted.
    """
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        server_port=12345,
    )
    assert creator.server_port == 12345


def test_agent_creator_server_port_defaults_to_zero() -> None:
    """AgentCreator.server_port defaults to 0 for legacy test callers.

    Tests that don't exercise the happy-path redirect can construct an
    AgentCreator without explicitly passing a port.
    """
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
    )
    assert creator.server_port == 0


# -- LEASED mode tests --


def test_build_mngr_create_command_raises_for_leased_mode() -> None:
    """LEASED mode should not use mngr create and must raise."""
    with pytest.raises(MngrCommandError, match="LEASED mode does not use mngr create"):
        _build_mngr_create_command(
            launch_mode=LaunchMode.LEASED,
            agent_name=AgentName("test-agent"),
            agent_id=AgentId(),
        )


# -- _load_or_create_leased_host_keypair tests --


def test_load_or_create_leased_host_keypair_generates_new_key(tmp_path: Path) -> None:
    """First call should generate a new ed25519 keypair."""
    private_key_path, public_key = _load_or_create_leased_host_keypair(tmp_path)

    assert private_key_path.exists()
    assert private_key_path.parent == tmp_path / "ssh" / "keys" / "leased_host"
    assert private_key_path.name == "id_ed25519"
    assert (private_key_path.parent / "id_ed25519.pub").exists()
    assert public_key.startswith("ssh-ed25519 ")


def test_load_or_create_leased_host_keypair_reuses_existing_key(tmp_path: Path) -> None:
    """Second call should return the same keypair without regenerating."""
    private_key_path_1, public_key_1 = _load_or_create_leased_host_keypair(tmp_path)
    private_key_path_2, public_key_2 = _load_or_create_leased_host_keypair(tmp_path)

    assert private_key_path_1 == private_key_path_2
    assert public_key_1 == public_key_2


# -- _write_dynamic_host_entry tests --


def test_write_dynamic_host_entry_creates_valid_toml(tmp_path: Path) -> None:
    """Writing a host entry should produce a valid TOML file."""
    hosts_file = tmp_path / "dynamic_hosts.toml"
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="test-host",
        address="10.0.0.1",
        port=2222,
        user="root",
        key_file=Path("/home/user/.ssh/id_ed25519"),
    )

    content = tomllib.loads(hosts_file.read_text())
    assert "test-host" in content
    assert content["test-host"]["address"] == "10.0.0.1"
    assert content["test-host"]["port"] == 2222
    assert content["test-host"]["user"] == "root"
    assert content["test-host"]["key_file"] == "/home/user/.ssh/id_ed25519"


def test_write_dynamic_host_entry_appends_to_existing(tmp_path: Path) -> None:
    """Writing a second host entry should preserve the first."""
    hosts_file = tmp_path / "dynamic_hosts.toml"
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="host-a",
        address="10.0.0.1",
        port=22,
        user="root",
        key_file=Path("/key1"),
    )
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="host-b",
        address="10.0.0.2",
        port=2222,
        user="ubuntu",
        key_file=Path("/key2"),
    )

    content = tomllib.loads(hosts_file.read_text())
    assert "host-a" in content
    assert "host-b" in content
    assert content["host-a"]["address"] == "10.0.0.1"
    assert content["host-b"]["address"] == "10.0.0.2"


def test_write_dynamic_host_entry_creates_parent_directories(tmp_path: Path) -> None:
    """The function should create parent directories if they do not exist."""
    hosts_file = tmp_path / "nested" / "dir" / "dynamic_hosts.toml"
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="test-host",
        address="10.0.0.1",
        port=22,
        user="root",
        key_file=Path("/key"),
    )
    assert hosts_file.exists()


# -- _remove_dynamic_host_entry tests --


def test_remove_dynamic_host_entry_removes_section(tmp_path: Path) -> None:
    """Removing a host entry should delete its section from the TOML file."""
    hosts_file = tmp_path / "dynamic_hosts.toml"
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="host-a",
        address="10.0.0.1",
        port=22,
        user="root",
        key_file=Path("/key1"),
    )
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="host-b",
        address="10.0.0.2",
        port=2222,
        user="ubuntu",
        key_file=Path("/key2"),
    )

    _remove_dynamic_host_entry(hosts_file, "host-a")

    content = tomllib.loads(hosts_file.read_text())
    assert "host-a" not in content
    assert "host-b" in content


def test_remove_dynamic_host_entry_noop_for_missing_file(tmp_path: Path) -> None:
    """Removing from a nonexistent file should be a no-op."""
    hosts_file = tmp_path / "nonexistent.toml"
    _remove_dynamic_host_entry(hosts_file, "host-a")
    assert not hosts_file.exists()


def test_remove_dynamic_host_entry_noop_for_missing_section(tmp_path: Path) -> None:
    """Removing a nonexistent section should be a no-op."""
    hosts_file = tmp_path / "dynamic_hosts.toml"
    _write_dynamic_host_entry(
        dynamic_hosts_file=hosts_file,
        host_name="host-a",
        address="10.0.0.1",
        port=22,
        user="root",
        key_file=Path("/key"),
    )

    _remove_dynamic_host_entry(hosts_file, "host-b")

    content = tomllib.loads(hosts_file.read_text())
    assert "host-a" in content


# -- _save_lease_info / _load_lease_info / _remove_lease_info tests --


def test_save_and_load_lease_info(tmp_path: Path) -> None:
    agent_id = AgentId()
    _save_lease_info(tmp_path, agent_id, 42)
    loaded = _load_lease_info(tmp_path, agent_id)
    assert loaded == 42


def test_load_lease_info_returns_none_for_missing(tmp_path: Path) -> None:
    result = _load_lease_info(tmp_path, AgentId())
    assert result is None


def test_remove_lease_info_deletes_file(tmp_path: Path) -> None:
    agent_id = AgentId()
    _save_lease_info(tmp_path, agent_id, 99)
    _remove_lease_info(tmp_path, agent_id)
    assert _load_lease_info(tmp_path, agent_id) is None


def test_remove_lease_info_noop_for_missing(tmp_path: Path) -> None:
    _remove_lease_info(tmp_path, AgentId())


# -- release_leased_host tests --


def test_release_leased_host_with_pool_client(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
) -> None:
    """release_leased_host removes the dynamic host entry, calls release, and removes lease info."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
    )

    # Set up state: lease info and a dynamic host entry
    _save_lease_info(tmp_path, agent_id, 7)
    dynamic_hosts_file = tmp_path / "ssh" / "dynamic_hosts.toml"
    host_name = "leased-{}".format(agent_id)
    _write_dynamic_host_entry(
        dynamic_hosts_file=dynamic_hosts_file,
        host_name=host_name,
        address="10.0.0.1",
        port=2222,
        user="root",
        key_file=Path("/tmp/key"),
    )

    creator.release_leased_host(agent_id, access_token="test-token")

    # Lease info should be removed
    assert _load_lease_info(tmp_path, agent_id) is None
    # Dynamic host entry should be removed
    content = tomllib.loads(dynamic_hosts_file.read_text())
    assert host_name not in content


def test_release_leased_host_noop_when_no_lease_info(tmp_path: Path) -> None:
    """release_leased_host is a no-op when there is no lease info for the agent."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(paths=paths)
    creator.release_leased_host(AgentId(), access_token="test-token")


def test_release_leased_host_without_pool_client(tmp_path: Path) -> None:
    """release_leased_host logs a warning but does not crash when host_pool_client is None."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    creator = AgentCreator(paths=paths)

    _save_lease_info(tmp_path, agent_id, 7)
    creator.release_leased_host(agent_id, access_token="test-token")

    # Lease info should NOT be removed (release was not successful)
    assert _load_lease_info(tmp_path, agent_id) == 7


def test_agent_creator_has_host_pool_client_field(tmp_path: Path) -> None:
    """AgentCreator accepts an optional host_pool_client field."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator_without = AgentCreator(paths=paths)
    assert creator_without.host_pool_client is None

    client = HostPoolClient(connector_url=RemoteServiceConnectorUrl("http://example.com"))
    creator_with = AgentCreator(paths=paths, host_pool_client=client)
    assert creator_with.host_pool_client is not None


def test_start_creation_accepts_access_token_and_version(tmp_path: Path) -> None:
    """start_creation accepts access_token and version kwargs without error."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(paths=paths)
    # LEASED mode will fail in the background thread (no host_pool_client),
    # but start_creation itself should return immediately with an agent ID.
    agent_id = creator.start_creation(
        repo_source="https://example.com/repo.git",
        agent_name="test",
        launch_mode=LaunchMode.LEASED,
        access_token="test-token",
        version="v0.1.0",
    )
    assert agent_id is not None
    creator.wait_for_all(timeout=5.0)
    info = creator.get_creation_info(agent_id)
    assert info is not None
    # Should fail because host_pool_client is None
    assert info.status == AgentCreationStatus.FAILED
