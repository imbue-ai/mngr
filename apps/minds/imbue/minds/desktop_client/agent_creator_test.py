import queue as queue_mod
import threading
import time
import tomllib
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import PLACEHOLDER_ANTHROPIC_API_KEY
from imbue.minds.desktop_client.agent_creator import WORKSPACE_SERVER_SERVICE_NAME
from imbue.minds.desktop_client.agent_creator import _RedactingOutputCallback
from imbue.minds.desktop_client.agent_creator import _build_inject_anthropic_command
from imbue.minds.desktop_client.agent_creator import _build_latchkey_gateway_url
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _build_patch_claude_config_command
from imbue.minds.desktop_client.agent_creator import _is_git_worktree
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _leased_agent_address
from imbue.minds.desktop_client.agent_creator import _load_lease_info
from imbue.minds.desktop_client.agent_creator import _load_or_create_leased_host_keypair
from imbue.minds.desktop_client.agent_creator import _make_host_name
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials_in_text
from imbue.minds.desktop_client.agent_creator import _remove_dynamic_host_entry
from imbue.minds.desktop_client.agent_creator import _remove_lease_info
from imbue.minds.desktop_client.agent_creator import _save_lease_info
from imbue.minds.desktop_client.agent_creator import _write_dynamic_host_entry
from imbue.minds.desktop_client.agent_creator import checkout_branch
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.agent_creator import make_log_callback
from imbue.minds.desktop_client.agent_creator import run_mngr_create
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.latchkey.gateway import AGENT_SIDE_LATCHKEY_PORT
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayManager
from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyClient
from imbue.minds.desktop_client.notification import NotificationDispatcher
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


def _make_empty_resolver() -> StaticBackendResolver:
    """Build a backend resolver with no registered services.

    Tests that exercise the failure paths of ``start_creation`` never reach
    the workspace-readiness poll, so an empty resolver is sufficient.
    """
    return StaticBackendResolver(url_by_agent_and_service={})


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


# -- _redact_url_credentials tests --


def test_redact_url_credentials_strips_user_password() -> None:
    assert (
        _redact_url_credentials("https://x-access-token:ghp_secret@github.com/user/repo.git")
        == "https://github.com/user/repo.git"
    )


def test_redact_url_credentials_leaves_plain_url_untouched() -> None:
    assert _redact_url_credentials("https://github.com/user/repo.git") == "https://github.com/user/repo.git"


def test_redact_url_credentials_leaves_ssh_url_untouched() -> None:
    # SSH-style URLs (git@host:path) have no scheme, so urlsplit returns an
    # empty netloc. The function must pass them through unchanged.
    assert _redact_url_credentials("git@github.com:user/repo.git") == "git@github.com:user/repo.git"


def test_redact_url_credentials_leaves_local_path_untouched() -> None:
    assert _redact_url_credentials("/home/user/my-template") == "/home/user/my-template"


# -- _redact_url_credentials_in_text tests --


def test_redact_url_credentials_in_text_strips_embedded_url() -> None:
    # Typical git stderr on auth failure -- we must scrub the token but keep
    # the rest of the message for debuggability.
    line = "fatal: unable to access 'https://x-access-token:ghp_secret@github.com/user/repo.git/': 403"
    assert _redact_url_credentials_in_text(line) == "fatal: unable to access 'https://github.com/user/repo.git/': 403"


def test_redact_url_credentials_in_text_leaves_plain_text_untouched() -> None:
    assert _redact_url_credentials_in_text("no URLs here") == "no URLs here"
    assert _redact_url_credentials_in_text("Cloning into 'my-repo'...") == "Cloning into 'my-repo'..."


def test_redact_url_credentials_in_text_strips_multiple_urls() -> None:
    text = "a https://u:p@host1/x and b https://user@host2/y"
    assert _redact_url_credentials_in_text(text) == "a https://host1/x and b https://host2/y"


def test_redact_url_credentials_in_text_leaves_bare_scp_url_untouched() -> None:
    # SCP-style SSH URLs don't have a scheme, so the regex won't match and
    # the user@host prefix (not a secret) is preserved.
    assert (
        _redact_url_credentials_in_text("cloning from git@github.com:user/repo.git")
        == "cloning from git@github.com:user/repo.git"
    )


@pytest.mark.timeout(30)
def test_clone_git_repo_redacts_credentials_in_error(tmp_path: Path) -> None:
    """GitCloneError must not leak embedded credentials from git's stderr."""
    # Point at a clearly-bogus credentialed URL; git will fail and echo the
    # URL in its stderr. The raised error must have the token stripped.
    # Bound the run time: git's default connect behaviour is unspecified and
    # a restrictive-firewall configuration could otherwise let this hang.
    dest = tmp_path / "dest"
    secret_token = "ghp_thisshouldneverappear"
    bad_url = f"https://x-access-token:{secret_token}@127.0.0.1:1/does-not-exist.git"
    with pytest.raises(GitCloneError) as excinfo:
        clone_git_repo(GitUrl(bad_url), dest)
    assert secret_token not in str(excinfo.value)


# -- _RedactingOutputCallback tests --


def test_redacting_output_callback_redacts_and_forwards_is_stdout_flag() -> None:
    """The wrapper must scrub credentials and pass the is_stdout flag through."""
    received: list[tuple[str, bool]] = []

    def inner(line: str, is_stdout: bool) -> None:
        received.append((line, is_stdout))

    wrapper = _RedactingOutputCallback(inner=inner)
    # A line with an embedded credentialed URL -- userinfo must be stripped,
    # and the is_stdout flag must be forwarded verbatim.
    wrapper("fatal: unable to access 'https://x-access-token:ghp_secret@github.com/u/r.git/'", False)
    # A line with no URL -- must pass through unchanged, with is_stdout=True preserved.
    wrapper("Cloning into 'my-repo'...", True)
    assert received == [
        ("fatal: unable to access 'https://github.com/u/r.git/'", False),
        ("Cloning into 'my-repo'...", True),
    ]


# -- _build_mngr_create_command tests --


def test_leased_agent_address_uses_ssh_provider_and_leased_host_name() -> None:
    """The explicit address matches the SSH provider + host-name pattern that
    ``imbue.minds.bootstrap._ensure_mngr_settings`` sets up, so mngr discovery
    only hits the SSH provider and skips ID lookup entirely."""
    agent_id = AgentId()
    address = _leased_agent_address(agent_id)
    assert address == f"{agent_id}@leased-{agent_id}.ssh"


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


def test_agent_creator_get_creation_info_returns_none_for_unknown(
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    assert creator.get_creation_info(AgentId()) is None


def test_agent_creator_start_creation_returns_agent_id_and_tracks_status(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Verify start_creation returns an agent ID and sets initial CLONING status.

    The actual background thread will fail (since the git URL is invalid),
    but the initial status should be immediately available.
    """
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )

    agent_id = creator.start_creation("file:///nonexistent-repo")
    info = creator.get_creation_info(agent_id)

    assert info is not None
    assert info.agent_id == agent_id
    assert info.status == AgentCreationStatus.CLONING
    creator.wait_for_all()


def test_agent_creator_start_creation_with_custom_name(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Verify start_creation accepts a custom agent name."""
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    agent_id = creator.start_creation("file:///nonexistent-repo", agent_name="my-agent")
    info = creator.get_creation_info(agent_id)
    assert info is not None
    creator.wait_for_all()


def test_agent_creator_get_log_queue_returns_none_for_unknown(
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    assert creator.get_log_queue(AgentId()) is None


def test_agent_creator_get_log_queue_returns_queue_for_tracked(
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=Path("/tmp/test")),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    agent_id = creator.start_creation("file:///nonexistent-repo")
    q = creator.get_log_queue(agent_id)
    assert q is not None
    creator.wait_for_all()


def test_agent_creator_start_creation_with_local_path(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Verify start_creation with a nonexistent local path eventually reaches FAILED status."""
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
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


def _mock_transport_always_connect_error(request: httpx.Request) -> httpx.Response:
    """Simulate a workspace server whose URL is registered but the port isn't up yet."""
    raise httpx.ConnectError("connection refused")


def _mock_transport_always_ok(request: httpx.Request) -> httpx.Response:
    """Simulate a workspace server that's answering HTTP."""
    return httpx.Response(200, text="ok")


def _mock_transport_always_503(request: httpx.Request) -> httpx.Response:
    """Simulate a workspace server that's answering HTTP but isn't ready yet."""
    return httpx.Response(503, text="not ready")


def _make_workspace_ready_creator(
    *,
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
    backend_resolver: BackendResolverInterface,
    transport: Callable[[httpx.Request], httpx.Response] | None = _mock_transport_always_ok,
    workspace_ready_timeout_seconds: float = 1.0,
    workspace_ready_poll_interval_seconds: float = 0.01,
    workspace_ready_probe_timeout_seconds: float = 0.05,
) -> AgentCreator:
    """Construct an ``AgentCreator`` wired up for ``_wait_for_workspace_ready`` tests.

    All five readiness tests share the same constructor boilerplate; this
    helper centralizes it so each test only has to specify what it actually
    cares about (the resolver and the simulated HTTP probe behavior).
    Pass ``transport=None`` to skip injecting a probe client and let the
    default ``httpx.Client`` factory run -- the URL-registration timeout
    test never reaches the probe stage and so doesn't need one.
    """
    kwargs: dict[str, object] = dict(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        backend_resolver=backend_resolver,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        workspace_ready_timeout_seconds=workspace_ready_timeout_seconds,
        workspace_ready_poll_interval_seconds=workspace_ready_poll_interval_seconds,
        workspace_ready_probe_timeout_seconds=workspace_ready_probe_timeout_seconds,
    )
    if transport is not None:
        kwargs["probe_http_client"] = httpx.Client(transport=httpx.MockTransport(transport))
    return AgentCreator(**kwargs)


def test_wait_for_workspace_ready_returns_true_once_url_appears(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Polling returns True after the workspace server registers its URL.

    The countdown resolver forces at least a few poll iterations, so this
    also guards against a regression where the loop exits too early. A
    MockTransport stands in for the HTTP probe so tests don't need a real
    server bound to the stub URL; the probe behavior itself is covered by
    dedicated tests below.
    """
    agent_id = AgentId()
    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=_CountdownReadyResolver(url="http://workspace-backend", calls_before_ready=3),
        workspace_ready_timeout_seconds=5.0,
    )
    log_queue: queue_mod.Queue[str] = queue_mod.Queue()

    assert creator._wait_for_workspace_ready(agent_id, log_queue) is True


def test_wait_for_workspace_ready_returns_false_on_timeout(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Polling returns False after the timeout elapses without the URL ever appearing.

    The creation flow still completes after timeout (the user is redirected
    and sees whatever the forwarder produces), so a timeout should be logged
    but not raised.
    """
    agent_id = AgentId()
    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        # Stage-1 timeout fires before the probe runs, so no transport needed.
        transport=None,
        workspace_ready_timeout_seconds=0.05,
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


def test_wait_for_workspace_ready_uses_the_workspace_service_name(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Polling looks up the workspace server under ``WORKSPACE_SERVER_SERVICE_NAME``.

    Regression guard: if this constant drifted away from what the subdomain
    forwarder checks, creation would complete against some other service's
    URL and the user would land on a workspace that isn't actually ready.
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

    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=_RecordingResolver(),
    )

    assert creator._wait_for_workspace_ready(agent_id, queue_mod.Queue()) is True
    assert observed_service_names == [WORKSPACE_SERVER_SERVICE_NAME]


# -- workspace_ready HTTP probe tests --


def test_wait_for_workspace_ready_requires_http_readiness_not_just_url(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A registered URL whose server isn't answering must NOT satisfy readiness.

    Reproduces the production bug: the agent writes its URL to events.jsonl
    around server-socket bind, but the ASGI app can still be starting up.
    Previously this returned True as soon as the resolver had a URL, and the
    browser got redirected straight into a dead page. The probe must keep
    polling until the server actually responds with a 200.
    """
    agent_id = AgentId()
    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=StaticBackendResolver(
            url_by_agent_and_service={str(agent_id): {"system_interface": "http://workspace-backend"}},
        ),
        transport=_mock_transport_always_connect_error,
        workspace_ready_timeout_seconds=0.15,
    )

    start = time.monotonic()
    assert creator._wait_for_workspace_ready(agent_id, queue_mod.Queue()) is False
    elapsed = time.monotonic() - start
    # Must have actually waited for the timeout -- not short-circuited on
    # the "URL exists" check.
    assert elapsed >= 0.15


def test_wait_for_workspace_ready_rejects_non_200_response(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A server that answers HTTP but with a non-200 status is not yet ready.

    Without this guard the probe would say "ready" the moment the workspace
    server's TCP layer returned any response, even an error page produced
    while initialization is still in progress.
    """
    agent_id = AgentId()
    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=StaticBackendResolver(
            url_by_agent_and_service={str(agent_id): {"system_interface": "http://workspace-backend"}},
        ),
        transport=_mock_transport_always_503,
        workspace_ready_timeout_seconds=0.1,
    )

    assert creator._wait_for_workspace_ready(agent_id, queue_mod.Queue()) is False


def test_wait_for_workspace_ready_returns_true_once_server_answers(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """Once the HTTP probe gets a 200, readiness is satisfied."""
    agent_id = AgentId()
    creator = _make_workspace_ready_creator(
        tmp_path=tmp_path,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=StaticBackendResolver(
            url_by_agent_and_service={str(agent_id): {"system_interface": "http://workspace-backend"}},
        ),
        workspace_ready_timeout_seconds=2.0,
    )

    assert creator._wait_for_workspace_ready(agent_id, queue_mod.Queue()) is True


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
    test_uuid = UUID("a1b2c3d4-0000-0000-0000-000000000001")
    _save_lease_info(tmp_path, agent_id, test_uuid)
    loaded = _load_lease_info(tmp_path, agent_id)
    assert loaded == test_uuid


def test_load_lease_info_returns_none_for_missing(tmp_path: Path) -> None:
    result = _load_lease_info(tmp_path, AgentId())
    assert result is None


def test_remove_lease_info_deletes_file(tmp_path: Path) -> None:
    agent_id = AgentId()
    _save_lease_info(tmp_path, agent_id, UUID("e5f60000-0000-0000-0000-000000000002"))
    _remove_lease_info(tmp_path, agent_id)
    assert _load_lease_info(tmp_path, agent_id) is None


def test_remove_lease_info_noop_for_missing(tmp_path: Path) -> None:
    _remove_lease_info(tmp_path, AgentId())


# -- release_leased_host tests --


def test_release_leased_host_with_pool_client(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """release_leased_host removes the dynamic host entry, calls release, and removes lease info."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )

    # Set up state: lease info and a dynamic host entry
    _save_lease_info(tmp_path, agent_id, UUID("00000000-0000-0000-0000-000000000007"))
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


def test_release_leased_host_noop_when_no_lease_info(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """release_leased_host is a no-op when there is no lease info for the agent."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    creator.release_leased_host(AgentId(), access_token="test-token")


def test_release_leased_host_without_pool_client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """release_leased_host logs a warning but does not crash when host_pool_client is None."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    creator = AgentCreator(
        paths=paths,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )

    test_uuid = UUID("00000000-0000-0000-0000-000000000007")
    _save_lease_info(tmp_path, agent_id, test_uuid)
    creator.release_leased_host(agent_id, access_token="test-token")

    # Lease info should NOT be removed (release was not successful)
    assert _load_lease_info(tmp_path, agent_id) == test_uuid


def test_agent_creator_has_host_pool_client_field(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """AgentCreator accepts an optional host_pool_client field."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator_without = AgentCreator(
        paths=paths,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    assert creator_without.host_pool_client is None

    client = HostPoolClient(connector_url=RemoteServiceConnectorUrl("http://example.com"))
    creator_with = AgentCreator(
        paths=paths,
        host_pool_client=client,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    assert creator_with.host_pool_client is not None


def test_start_creation_leased_raises_without_pool_client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """start_creation with LEASED mode raises immediately if no host_pool_client."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    with pytest.raises(MngrCommandError, match="host_pool_client"):
        creator.start_creation(
            repo_source="https://example.com/repo.git",
            agent_name="test",
            launch_mode=LaunchMode.LEASED,
            access_token="test-token",
            version="v0.1.0",
        )


def test_create_leased_agent_fails_without_access_token(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """start_creation raises synchronously when access_token is empty for LEASED mode."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    with pytest.raises(MngrCommandError, match="access_token"):
        creator.start_creation(
            repo_source="https://example.com/repo.git",
            agent_name="test",
            launch_mode=LaunchMode.LEASED,
            access_token="",
            version="v0.1.0",
        )


def test_create_leased_agent_fails_without_version(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """start_creation raises synchronously when version is empty for LEASED mode."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    with pytest.raises(MngrCommandError, match="version"):
        creator.start_creation(
            repo_source="https://example.com/repo.git",
            agent_name="test",
            launch_mode=LaunchMode.LEASED,
            access_token="test-token",
            version="",
        )


def test_create_leased_agent_leases_and_writes_dynamic_host(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """_create_leased_agent leases a host, writes dynamic host entry and lease info.

    The mngr rename/start will fail (no real mngr), but the lease and
    setup steps should complete, and cleanup should release the host.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    agent_id = creator.start_creation(
        repo_source="https://example.com/repo.git",
        agent_name="test-workspace",
        launch_mode=LaunchMode.LEASED,
        access_token="test-token",
        version="v0.1.0",
    )
    creator.wait_for_all(timeout=10.0)
    info = creator.get_creation_info(agent_id)
    assert info is not None
    # Will fail on mngr rename (not installed), but the lease should have been
    # attempted and then cleaned up
    assert info.status == AgentCreationStatus.FAILED


def test_cleanup_failed_lease(
    tmp_path: Path,
    fake_pool_server: HostPoolClient,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """_cleanup_failed_lease removes dynamic host entry, releases host, and removes lease info."""
    paths = WorkspacePaths(data_dir=tmp_path)
    creator = AgentCreator(
        paths=paths,
        host_pool_client=fake_pool_server,
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    agent_id = AgentId()
    dynamic_hosts_file = tmp_path / "ssh" / "dynamic_hosts.toml"
    host_entry_name = "leased-{}".format(agent_id)

    # Set up state as if a lease succeeded but setup failed
    cleanup_uuid = UUID("00000000-0000-0000-0000-000000000099")
    _save_lease_info(tmp_path, agent_id, cleanup_uuid)
    _write_dynamic_host_entry(
        dynamic_hosts_file=dynamic_hosts_file,
        host_name=host_entry_name,
        address="10.0.0.1",
        port=2222,
        user="root",
        key_file=Path("/tmp/key"),
    )

    log_queue: queue_mod.Queue[str] = queue_mod.Queue()
    creator._cleanup_failed_lease(
        agent_id=agent_id,
        host_db_id=cleanup_uuid,
        access_token="test-token",
        dynamic_hosts_file=dynamic_hosts_file,
        host_entry_name=host_entry_name,
        log_queue=log_queue,
    )

    # Dynamic host entry should be removed
    content = tomllib.loads(dynamic_hosts_file.read_text())
    assert host_entry_name not in content
    # Lease info should be removed
    assert _load_lease_info(tmp_path, agent_id) is None


# -- Latchkey gateway env plumbing --


def _sample_gateway_info(port: int) -> LatchkeyGatewayInfo:
    return LatchkeyGatewayInfo(
        agent_id=AgentId(),
        host="127.0.0.1",
        port=port,
        pid=99999,
        started_at=datetime.now(timezone.utc),
    )


def test_build_latchkey_gateway_url_dev_uses_dynamic_host_port() -> None:
    url = _build_latchkey_gateway_url(LaunchMode.DEV, _sample_gateway_info(port=54321))
    assert url == "http://127.0.0.1:54321"


@pytest.mark.parametrize("launch_mode", [LaunchMode.LOCAL, LaunchMode.LIMA, LaunchMode.CLOUD])
def test_build_latchkey_gateway_url_containerized_uses_fixed_agent_side_port(launch_mode: LaunchMode) -> None:
    # Containerized / VM / VPS agents see the gateway on a reverse-tunneled
    # fixed port inside their own 127.0.0.1 -- regardless of the dynamic
    # host-side port the gateway is actually listening on.
    url = _build_latchkey_gateway_url(launch_mode, _sample_gateway_info(port=54321))
    assert url == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"


def test_build_mngr_create_command_injects_latchkey_gateway_env() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.DEV,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
        latchkey_gateway_url="http://127.0.0.1:5050",
    )
    env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--env"]
    assert "LATCHKEY_GATEWAY=http://127.0.0.1:5050" in env_values


def test_build_mngr_create_command_omits_latchkey_gateway_env_by_default() -> None:
    cmd, _api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.DEV,
        agent_name=AgentName("test-agent"),
        agent_id=AgentId(),
    )
    env_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--env"]
    assert not any(v.startswith("LATCHKEY_GATEWAY=") for v in env_values)


@pytest.mark.timeout(30)
def test_agent_creator_cleans_up_pre_spawned_latchkey_gateway_on_failure(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """When mngr create fails, any latchkey gateway pre-spawned for the agent must be torn down.

    Uses a fake ``latchkey`` binary so this test does not require a real
    Latchkey install. The agent creation itself fails because the "local
    path" does not exist, which is the shortest path to exercising the
    failure-cleanup branch.
    """
    fake_binary = tmp_path / "latchkey"
    fake_binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    fake_binary.chmod(0o755)

    gateway_manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    gateway_manager.start(data_dir=tmp_path / "gateway-data")
    try:
        creator = AgentCreator(
            paths=WorkspacePaths(data_dir=tmp_path / "minds"),
            backend_resolver=_make_empty_resolver(),
            latchkey_gateway_manager=gateway_manager,
            root_concurrency_group=root_concurrency_group,
            notification_dispatcher=notification_dispatcher,
        )
        # "Local path" that does not exist -- mngr create will not even get
        # the chance to fail; _create_agent_background aborts with MngrCommandError.
        agent_id = creator.start_creation("/definitely/not/here", launch_mode=LaunchMode.DEV)

        for _ in range(100):
            info = creator.get_creation_info(agent_id)
            if info is not None and info.status == AgentCreationStatus.FAILED:
                break
            threading.Event().wait(0.05)
        info = creator.get_creation_info(agent_id)
        assert info is not None
        assert info.status == AgentCreationStatus.FAILED
        creator.wait_for_all()

        # No lingering gateway or record for this agent.
        assert gateway_manager.get_gateway_info(agent_id) is None
    finally:
        gateway_manager.stop()


def test_is_git_worktree_returns_false_for_normal_repo(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    assert _is_git_worktree(tmp_path) is False


def test_is_git_worktree_returns_true_for_worktree(tmp_path: Path) -> None:
    git_file = tmp_path / ".git"
    git_file.write_text("gitdir: /some/other/path/.git/worktrees/foo")
    assert _is_git_worktree(tmp_path) is True


def test_is_git_worktree_returns_false_when_no_git(tmp_path: Path) -> None:
    assert _is_git_worktree(tmp_path) is False


def test_make_host_name_appends_host_suffix() -> None:
    result = _make_host_name(AgentName("my-agent"))
    assert result == "my-agent-host"


def test_placeholder_anthropic_api_key_has_correct_prefix() -> None:
    assert PLACEHOLDER_ANTHROPIC_API_KEY.startswith("sk-ant-api03-")


def test_placeholder_anthropic_api_key_has_realistic_length() -> None:
    assert len(PLACEHOLDER_ANTHROPIC_API_KEY) >= 100


def test_agent_creator_accepts_litellm_key_client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client = LiteLLMKeyClient(connector_url=RemoteServiceConnectorUrl("http://127.0.0.1:1"))
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        litellm_key_client=client,
    )
    assert creator.litellm_key_client is client


def test_agent_creator_litellm_key_client_defaults_to_none(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path),
        backend_resolver=_make_empty_resolver(),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    assert creator.litellm_key_client is None


def test_build_inject_anthropic_command_sets_key_and_base_url() -> None:
    cmd = _build_inject_anthropic_command(
        litellm_key="sk-litellm-real-key-abc123",
        litellm_base_url="https://proxy.modal.run/anthropic",
        env_path="/mngr/agents/test-id/env",
    )
    assert "ANTHROPIC_API_KEY=sk-litellm-real-key-abc123" in cmd
    assert "ANTHROPIC_BASE_URL=https://proxy.modal.run/anthropic" in cmd
    assert "/mngr/agents/test-id/env" in cmd


def test_build_inject_anthropic_command_removes_old_values_first() -> None:
    cmd = _build_inject_anthropic_command(
        litellm_key="sk-test",
        litellm_base_url="https://example.com/anthropic",
        env_path="/tmp/env",
    )
    assert "sed -i '/^ANTHROPIC_API_KEY=/d'" in cmd
    assert "sed -i '/^ANTHROPIC_BASE_URL=/d'" in cmd


def test_build_patch_claude_config_command_targets_correct_path() -> None:
    agent_id = AgentId()
    cmd = _build_patch_claude_config_command(
        litellm_key="sk-litellm-real-key-xyz",
        agent_id=agent_id,
    )
    expected_path = "/mngr/agents/{}/plugin/claude/anthropic/.claude.json".format(agent_id)
    assert expected_path in cmd
    assert "sk-litellm-real-key-xyz" in cmd


def test_build_patch_claude_config_command_uses_python_json() -> None:
    cmd = _build_patch_claude_config_command(
        litellm_key="sk-test-key-0123456789",
        agent_id=AgentId(),
    )
    assert "python3" in cmd
    assert "primaryApiKey" in cmd
    assert "customApiKeyResponses" in cmd
    assert "sk-test-key-0123456789"[-20:] in cmd
