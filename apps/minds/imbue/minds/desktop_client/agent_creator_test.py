import queue as queue_mod
import threading
import time
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import WORKSPACE_SERVER_SERVICE_NAME
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _make_host_name
from imbue.minds.desktop_client.agent_creator import build_post_creation_redirect_url
from imbue.minds.desktop_client.agent_creator import checkout_branch
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.agent_creator import make_log_callback
from imbue.minds.desktop_client.agent_creator import run_mngr_create
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import GitOperationError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import ServiceName
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


def _make_empty_resolver() -> StaticBackendResolver:
    """Build a backend resolver with no registered services.

    Tests that exercise the failure paths of ``start_creation`` never reach the
    workspace-readiness poll, so an empty resolver is sufficient.
    """
    return StaticBackendResolver(url_by_agent_and_service={})


def test_agent_creator_get_creation_info_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
    )
    assert creator.get_creation_info(AgentId()) is None


def test_agent_creator_start_creation_returns_agent_id_and_tracks_status(tmp_path: Path) -> None:
    """Verify start_creation returns an agent ID and sets initial CLONING status.

    The actual background thread will fail (since the git URL is invalid),
    but the initial status should be immediately available.
    """
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_make_empty_resolver(),
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
        backend_resolver=_make_empty_resolver(),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo", agent_name="my-agent")
    info = creator.get_creation_info(agent_id)
    assert info is not None
    creator.wait_for_all()


def test_agent_creator_get_log_queue_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
    )
    assert creator.get_log_queue(AgentId()) is None


def test_agent_creator_get_log_queue_returns_queue_for_tracked() -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
    )
    agent_id = creator.start_creation("file:///nonexistent-repo")
    q = creator.get_log_queue(agent_id)
    assert q is not None
    creator.wait_for_all()


def test_agent_creator_start_creation_with_local_path(tmp_path: Path) -> None:
    """Verify start_creation with a nonexistent local path eventually reaches FAILED status."""
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_make_empty_resolver(),
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


def test_build_post_creation_redirect_url_uses_goto_bridge() -> None:
    """The happy-path redirect URL routes through the ``/goto/<id>/`` auth bridge.

    Regression guard: earlier iterations emitted ``http://<id>.localhost:<port>/``
    directly, which fails auth because Chromium treats ``localhost`` as a public
    suffix (so the ``Domain=localhost`` session cookie does not carry into
    ``<id>.localhost``) and bounces the user back to the bare-origin landing
    page -- which lists all minds. The redirect must stay on the bare origin so
    that the ``/goto/`` handler can mint a subdomain-auth token before the
    browser navigates to the workspace subdomain.
    """
    agent_id = AgentId()
    assert build_post_creation_redirect_url(agent_id) == f"/goto/{agent_id}/"


# -- _wait_for_workspace_ready tests --


class _CountdownReadyResolver(BackendResolverInterface):
    """A resolver that returns None for the first N ``get_backend_url`` calls.

    After ``calls_before_ready`` calls return None, subsequent calls return
    ``url``. Used to verify the poll loop actually iterates rather than just
    short-circuiting on the first attempt.
    """

    url: str = Field(description="URL returned once the countdown elapses")
    calls_before_ready: int = Field(description="Number of None returns before the URL appears")
    _call_count: int = PrivateAttr(default=0)

    def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
        self._call_count += 1
        if self._call_count > self.calls_before_ready:
            return self.url
        return None

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return ()

    def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
        return ()


def test_wait_for_workspace_ready_returns_true_once_url_appears(tmp_path: Path) -> None:
    """Polling returns True after the workspace server registers its URL.

    The countdown resolver forces at least a few poll iterations, so this
    also guards against a regression where the loop exits too early.
    """
    agent_id = AgentId()
    resolver = _CountdownReadyResolver(url="http://workspace-backend", calls_before_ready=3)
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=resolver,
        workspace_ready_timeout_seconds=5.0,
        workspace_ready_poll_interval_seconds=0.01,
    )
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()

    assert creator._wait_for_workspace_ready(agent_id, log_queue) is True


def test_wait_for_workspace_ready_returns_false_on_timeout(tmp_path: Path) -> None:
    """Polling returns False after the timeout elapses without the URL ever appearing.

    The creation flow still completes after timeout -- the subdomain forwarder's
    auto-refresh retry page covers the remaining gap -- so a timeout should be
    logged but not raised.
    """
    agent_id = AgentId()
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=resolver,
        workspace_ready_timeout_seconds=0.05,
        workspace_ready_poll_interval_seconds=0.01,
    )
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()

    start = time.monotonic()
    assert creator._wait_for_workspace_ready(agent_id, log_queue) is False
    elapsed = time.monotonic() - start

    assert elapsed >= 0.05
    # A warning log line must be emitted so the user sees why navigation
    # happened before the server confirmed readiness.
    lines = []
    while not log_queue.empty():
        lines.append(log_queue.get_nowait())
    assert any("did not register" in line for line in lines), lines


def test_wait_for_workspace_ready_uses_the_workspace_service_name(tmp_path: Path) -> None:
    """Polling looks up the workspace server under ``WORKSPACE_SERVER_SERVICE_NAME``.

    Regression guard: if this constant drifted away from what the subdomain
    forwarder checks, creation would complete instantly (seeing some other
    service's URL) and the user would still land on the retry page.
    """
    agent_id = AgentId()

    observed_service_names: list[ServiceName] = []

    class _RecordingResolver(BackendResolverInterface):
        def get_backend_url(self, agent_id: AgentId, service_name: ServiceName) -> str | None:
            observed_service_names.append(service_name)
            return "http://workspace-backend"

        def list_known_agent_ids(self) -> tuple[AgentId, ...]:
            return ()

        def list_services_for_agent(self, agent_id: AgentId) -> tuple[ServiceName, ...]:
            return ()

    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_RecordingResolver(),
        workspace_ready_timeout_seconds=1.0,
        workspace_ready_poll_interval_seconds=0.01,
    )

    assert creator._wait_for_workspace_ready(agent_id, queue_mod.Queue()) is True
    assert observed_service_names == [WORKSPACE_SERVER_SERVICE_NAME]
