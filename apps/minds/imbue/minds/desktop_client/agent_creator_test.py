"""Unit tests for agent_creator.

IMBUE_CLOUD-mode lease/rename/env-injection no longer happens in this
module: it runs inside ``ImbueCloudProvider.create_host``, reached
through the standard ``mngr create`` invocation. The plugin's own test
suite (``libs/mngr_imbue_cloud``) covers the lease + adopt path; this
file covers minds' command-building and helpers.
"""

import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path

import httpx
import pytest
from pydantic import AnyUrl
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import _CreateEventCapture
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _is_git_worktree
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials_in_text
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.errors import GitCloneError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import GitBranch
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostName
from imbue.mngr.utils.git_utils import GIT_MIRROR_PUSH_REFSPECS


def test_extract_repo_name_strips_dot_git_and_trailing_slash() -> None:
    assert extract_repo_name("https://github.com/user/repo.git") == "repo"
    assert extract_repo_name("https://github.com/user/repo/") == "repo"
    assert extract_repo_name("https://github.com/user/Some-Repo_Name") == "Some-Repo_Name"


def test_extract_repo_name_falls_back_to_workspace() -> None:
    assert extract_repo_name("/") == "workspace"
    assert extract_repo_name("///") == "workspace"


def test_create_event_capture_records_error_class_from_jsonl_error_event() -> None:
    """A structured ``{"event":"error","error_class":...}`` line populates ``error_class``.

    This is what lets the fast->slow fallback branch on the error *type* rather
    than substring-matching human text.
    """
    capture = _CreateEventCapture()
    capture(
        '{"event": "error", "error_class": "FastPathUnavailableError", "message": "no match"}',
        is_stdout=True,
    )
    assert capture.error_class == "FastPathUnavailableError"
    assert capture.canonical_agent_id is None


def test_create_event_capture_still_records_created_event() -> None:
    """The error-event handling must not regress the existing ``created`` parsing."""
    capture = _CreateEventCapture()
    capture(
        '{"event": "created", "agent_id": "agent-b40593cc326a41cd832e3dc5c3d951de", "host_id": "host-xyz"}',
        is_stdout=True,
    )
    assert str(capture.canonical_agent_id) == "agent-b40593cc326a41cd832e3dc5c3d951de"
    assert capture.canonical_host_id == "host-xyz"
    assert capture.error_class is None


def test_create_event_capture_ignores_error_event_without_error_class() -> None:
    """An error event lacking ``error_class`` leaves the field unset (no crash)."""
    capture = _CreateEventCapture()
    capture('{"event": "error", "message": "something failed"}', is_stdout=True)
    assert capture.error_class is None


def test_mngr_command_error_carries_error_class() -> None:
    """MngrCommandError exposes the parsed error class for fallback decisions."""
    err = MngrCommandError("mngr create failed", error_class="FastPathUnavailableError")
    assert err.error_class == "FastPathUnavailableError"
    assert MngrCommandError("plain failure").error_class is None


def test_is_local_path_recognises_relative_and_absolute_paths() -> None:
    assert _is_local_path("/tmp/foo")
    assert _is_local_path("./foo")
    assert _is_local_path("../foo")
    assert _is_local_path("~/foo")
    assert not _is_local_path("https://example.com/foo")
    assert not _is_local_path("git@github.com:user/repo.git")


def test_redact_url_credentials_strips_userinfo_for_schemed_urls() -> None:
    assert _redact_url_credentials("https://x-access-token:tok@github.com/user/repo") == "https://github.com/user/repo"
    assert _redact_url_credentials("https://github.com/user/repo") == "https://github.com/user/repo"


def test_redact_url_credentials_in_text_strips_embedded_userinfo() -> None:
    msg = "fatal: unable to access 'https://user:secret@github.com/x/y': bad"
    assert _redact_url_credentials_in_text(msg) == "fatal: unable to access 'https://github.com/x/y': bad"


def test_build_mngr_create_command_lifts_latchkey_env_to_host_env_flags() -> None:
    """``_build_mngr_create_command`` lifts each entry of ``latchkey_env`` into a ``--host-env`` flag.

    The shape of the env (which keys are set, which URL is used, etc.) is decided
    upstream by ``prepare_agent_latchkey``; this command-builder just plumbs
    whatever it gets through to ``mngr create``. The plugin's
    ``agent_setup_test.py`` covers all the per-mode permutations.

    ``--host-env`` (not ``--env``) is used so the wiring is written to the
    new host's env file once and every agent that ever runs on the host
    inherits the same gateway URL / password / JWT.
    """
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.DOCKER,
        host_name=HostName("hello"),
        latchkey_env={
            "LATCHKEY_GATEWAY": "http://127.0.0.1:1989",
            "LATCHKEY_GATEWAY_PASSWORD": "sup3rs3cret",
            "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "eyJhbGc.fake.jwt",
            "LATCHKEY_DISABLE_COUNTING": "1",
        },
    )
    assert "LATCHKEY_GATEWAY=http://127.0.0.1:1989" in command
    assert "LATCHKEY_GATEWAY_PASSWORD=sup3rs3cret" in command
    assert "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE=eyJhbGc.fake.jwt" in command
    assert "LATCHKEY_DISABLE_COUNTING=1" in command

    # Each latchkey entry must be preceded by ``--host-env`` (not ``--env``)
    # so every agent on the host shares the same gateway wiring.
    latchkey_keys = {
        "LATCHKEY_GATEWAY",
        "LATCHKEY_GATEWAY_PASSWORD",
        "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE",
        "LATCHKEY_DISABLE_COUNTING",
    }
    for index, arg in enumerate(command):
        if any(arg.startswith(f"{key}=") for key in latchkey_keys):
            assert index > 0
            assert command[index - 1] == "--host-env", (
                f"Latchkey arg {arg!r} should be passed via --host-env, got {command[index - 1]!r}"
            )


def test_build_mngr_create_command_does_not_inject_minds_api_key() -> None:
    """The per-agent ``MINDS_API_KEY`` is gone.

    There is now exactly one ``MINDS_API_KEY`` per minds installation;
    the latchkey gateway's ``minds-api-proxy`` extension adds it as
    ``Authorization: Bearer <key>`` on every forwarded request, and the
    agent itself never sees the value. ``_build_mngr_create_command``
    must therefore neither generate nor reference it -- whether via
    ``--env`` or ``--host-env``.
    """
    for mode, account in (
        (LaunchMode.DOCKER, None),
        (LaunchMode.LIMA, None),
        (LaunchMode.CLOUD, None),
        (LaunchMode.IMBUE_CLOUD, "alice@imbue.com"),
    ):
        command = _build_mngr_create_command(
            launch_mode=mode,
            host_name=HostName("hello"),
            imbue_cloud_account=account,
        )
        joined = " ".join(command)
        assert "MINDS_API_KEY" not in joined, f"{mode}: command must not mention MINDS_API_KEY"


def test_build_mngr_create_command_forwards_fast_mode_for_imbue_cloud() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        host_name=HostName("hello"),
        imbue_cloud_account="alice@imbue.com",
        imbue_cloud_fast_mode="require",
    )
    # The fast_mode knob must reach mngr as a -b build arg.
    assert "-b" in command
    assert "fast_mode=require" in command


def test_build_mngr_create_command_omits_fast_mode_when_unset() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        host_name=HostName("hello"),
        imbue_cloud_account="alice@imbue.com",
    )
    joined = " ".join(command)
    assert "fast_mode" not in joined


def test_build_mngr_create_command_forwards_region_for_imbue_cloud() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        host_name=HostName("hello"),
        imbue_cloud_account="alice@imbue.com",
        region="US-WEST-OR",
    )
    # The explicit region must reach mngr as a hard -b region= build arg.
    assert "region=US-WEST-OR" in command


def test_build_mngr_create_command_forwards_region_for_vultr() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.CLOUD,
        host_name=HostName("hello"),
        region="lhr",
    )
    # Vultr takes the region as the --vps-region build arg.
    assert "--vps-region=lhr" in command


def test_build_mngr_create_command_omits_region_when_unset() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        host_name=HostName("hello"),
        imbue_cloud_account="alice@imbue.com",
    )
    joined = " ".join(command)
    assert "region=" not in joined


def test_build_mngr_create_command_ignores_region_for_docker() -> None:
    # Region is meaningful only for region-bearing providers; DOCKER drops it.
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.DOCKER,
        host_name=HostName("hello"),
        region="US-WEST-OR",
    )
    joined = " ".join(command)
    assert "region=" not in joined and "vps-region" not in joined


def test_build_mngr_create_command_omits_latchkey_when_env_is_empty() -> None:
    """Empty / ``None`` ``latchkey_env`` opts the host out of latchkey wiring entirely."""
    for latchkey_env in (None, {}):
        command = _build_mngr_create_command(
            launch_mode=LaunchMode.DOCKER,
            host_name=HostName("hello"),
            latchkey_env=latchkey_env,
        )
        joined = " ".join(command)
        assert "LATCHKEY_GATEWAY" not in joined
        assert "LATCHKEY_DISABLE_COUNTING" not in joined


def test_build_mngr_create_command_uses_main_template_and_omits_message_arg() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.DOCKER,
        host_name=HostName("hello"),
    )
    assert "--template" in command
    assert "main" in command
    # The /welcome message now lives in forever-claude-template's
    # [create_templates.main] section, so the explicit --message arg is gone.
    assert "--message" not in command
    # minds no longer pre-generates an agent id; mngr generates one and we
    # parse it out of the JSONL ``created`` event in run_mngr_create.
    assert "--id" not in command
    # ``--reuse --update`` keeps re-deploys of the same workspace name
    # idempotent on local-host modes.
    assert "--reuse" in command
    assert "--update" in command
    # We always emit JSONL so the canonical agent id can be parsed from the
    # trailing ``"event": "created"`` line.
    assert "--format" in command
    assert "jsonl" in command


def test_build_mngr_create_command_imbue_cloud_targets_account_provider() -> None:
    command = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        host_name=HostName("hello"),
        imbue_cloud_account="alice@imbue.com",
        imbue_cloud_repo_url="https://github.com/imbue-ai/forever-claude-template",
        imbue_cloud_branch_or_tag="v1.2.3",
    )
    joined = " ".join(command)
    # Address points at the imbue_cloud_<slug> provider so mngr routes
    # create_host to ImbueCloudProvider. The agent name is now the constant
    # ``system-services``; the user's input drives the host name.
    assert "system-services@hello.imbue_cloud_alice-imbue-com" in joined
    # IMBUE_CLOUD passes ``--reuse`` because the bake's services agent
    # is named ``system-services`` too, which mngr's pre-flight "agent
    # already exists on this host" check would otherwise reject. It
    # does NOT pass ``--update`` (the adopt path in
    # ``ImbueCloudHost.create_agent_state`` already patches the agent
    # in place; ``--update`` would re-run the bake's file-transfer
    # provisioning unnecessarily). No ``--id`` either: the canonical
    # id is parsed from the JSONL ``created`` event.
    assert "--id" not in command
    assert "--reuse" in command
    assert "--update" not in command
    # Lease attributes flow through --build-arg.
    assert "-b" in command
    assert "repo_url=https://github.com/imbue-ai/forever-claude-template" in command
    assert "repo_branch_or_tag=v1.2.3" in command
    # No secret env vars in argv: forwarding is declared by the FCT
    # ``imbue_cloud`` template's own ``pass_host_env`` and the values live
    # in the subprocess env ``run_mngr_create`` populates.
    assert "ANTHROPIC_API_KEY" not in joined
    assert "ANTHROPIC_BASE_URL" not in joined
    assert "GH_TOKEN" not in joined
    assert "--pass-host-env" not in command
    # IMBUE_CLOUD now uses the symmetric ``--template main --template imbue_cloud``
    # shape (mirroring how DOCKER/LIMA/CLOUD use ``--template main --template <provider>``).
    # The provider-specific knobs (idle_mode, pass_host_env) live in the
    # ``imbue_cloud`` template instead of being inlined here.
    assert "--template" in command
    template_args = [command[i + 1] for i, arg in enumerate(command) if arg == "--template" and i + 1 < len(command)]
    assert "main" in template_args
    assert "imbue_cloud" in template_args
    # ``--idle-mode disabled`` also moved into the template.
    assert "--idle-mode" not in command


def test_build_mngr_create_command_never_inlines_secret_env_flags() -> None:
    """Secret forwarding lives in FCT, not minds. The command line never carries
    ``--pass-(host-)env`` flags or secret values for any compute mode."""
    for mode, account in (
        (LaunchMode.DOCKER, None),
        (LaunchMode.LIMA, None),
        (LaunchMode.CLOUD, None),
        (LaunchMode.IMBUE_CLOUD, "alice@imbue.com"),
    ):
        command = _build_mngr_create_command(
            launch_mode=mode,
            host_name=HostName("hello"),
            imbue_cloud_account=account,
        )
        joined = " ".join(command)
        assert "--pass-env" not in command, f"{mode} should not inline --pass-env"
        # IMBUE_CLOUD compute *does* still get _remote_host_env_flags() which
        # uses --pass-host-env MNGR_PREFIX -- that one is unrelated to the
        # secrets we moved into FCT, so we only forbid the secret names here.
        assert "ANTHROPIC_API_KEY" not in joined, f"{mode} leaked ANTHROPIC_API_KEY"
        assert "ANTHROPIC_BASE_URL" not in joined, f"{mode} leaked ANTHROPIC_BASE_URL"
        assert "GH_TOKEN" not in joined, f"{mode} leaked GH_TOKEN"


def test_is_git_worktree_returns_false_for_nonexistent_path(tmp_path) -> None:
    assert not _is_git_worktree(tmp_path / "no-such-dir")


def _git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd`` and return its stripped stdout."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_origin_repo_with_branch(origin: Path, branch: str) -> None:
    """Create a repo on ``main`` with a second branch ``branch`` that has its own tip.

    The branch tip has a parent commit, which is exactly the case a ``--depth 1``
    clone would turn into a shallow boundary (and thus an unpushable mirror).
    """
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "f").write_text("base\n")
    _git(origin, "add", "f")
    _git(origin, "commit", "-qm", "base commit")
    _git(origin, "checkout", "-q", "-b", branch)
    (origin / "f").write_text("on branch\n")
    _git(origin, "commit", "-qam", "branch commit")
    _git(origin, "checkout", "-q", "main")


def test_clone_git_repo_branch_is_single_branch_non_shallow_and_mirror_pushable(tmp_path: Path) -> None:
    """A branch clone fetches only that branch, keeps full ancestry (non-shallow),
    and remains mirror-pushable.

    This is the regression for the deep-clone fix: the previous ``--depth 1``
    clone could not check out a non-default branch ("pathspec did not match") and,
    even once it could, a shallow clone is rejected by mngr create's mirror-push
    into the agent container ("shallow update not allowed"). A single-branch
    non-shallow clone fixes both.
    """
    origin = tmp_path / "origin"
    _make_origin_repo_with_branch(origin, "testing")

    dest = tmp_path / "clone"
    clone_git_repo(GitUrl("file://{}".format(origin)), dest, branch=GitBranch("testing"))

    # Checked out on the requested branch, with that branch's content.
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD") == "testing"
    assert (dest / "f").read_text() == "on branch\n"
    # Only the requested branch was fetched, and the clone is NOT shallow.
    assert _git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "testing"
    assert not (dest / ".git" / "shallow").exists()

    # The mirror-push mngr create performs into the agent container's bare repo
    # must succeed -- this is what fails on a shallow clone.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    push = subprocess.run(
        ["git", "-C", str(dest), "push", "--force", "--prune", str(bare), *GIT_MIRROR_PUSH_REFSPECS],
        capture_output=True,
        text=True,
    )
    assert push.returncode == 0, push.stderr
    assert _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "testing"


def test_clone_git_repo_raises_on_missing_branch(tmp_path: Path) -> None:
    """Requesting a branch that does not exist fails at clone time (cleanly)."""
    origin = tmp_path / "origin"
    _make_origin_repo_with_branch(origin, "testing")

    dest = tmp_path / "clone"
    with pytest.raises(GitCloneError):
        clone_git_repo(GitUrl("file://{}".format(origin)), dest, branch=GitBranch("nonexistent"))


class _RecordingNotificationDispatcher(NotificationDispatcher):
    """Test-only NotificationDispatcher that records dispatch calls instead of dispatching."""

    _recorded: list[tuple[NotificationRequest, str]] = PrivateAttr(default_factory=list)

    def dispatch(self, request: NotificationRequest, agent_display_name: str) -> None:
        self._recorded.append((request, agent_display_name))

    @property
    def recorded(self) -> list[tuple[NotificationRequest, str]]:
        return self._recorded


def _make_test_creator(
    tmp_path,
    root_concurrency_group: ConcurrencyGroup,
    *,
    mngr_forward_port: int = 0,
    preauth_cookie: str = "",
    timeout_seconds: float = 1.0,
    poll_interval_seconds: float = 0.05,
    probe_timeout_seconds: float = 0.5,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = None,
    notification_dispatcher: NotificationDispatcher | None = None,
    backup_setup_retry_budget_seconds: float = 0.0,
    backup_setup_retry_wait_seconds: float = 0.0,
) -> AgentCreator:
    paths = WorkspacePaths(data_dir=tmp_path)
    return AgentCreator(
        paths=paths,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher
        or NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        workspace_ready_timeout_seconds=timeout_seconds,
        workspace_ready_poll_interval_seconds=poll_interval_seconds,
        workspace_ready_probe_timeout_seconds=probe_timeout_seconds,
        system_interface_health_tracker=system_interface_health_tracker or SystemInterfaceHealthTracker(),
        backup_setup_retry_budget_seconds=backup_setup_retry_budget_seconds,
        backup_setup_retry_wait_seconds=backup_setup_retry_wait_seconds,
    )


class _ScriptedRequestHandler(BaseHTTPRequestHandler):
    """Returns 503 for the first ``not_ready_count`` requests, then 200."""

    not_ready_count: int = 0
    request_count: int = 0
    lock: threading.Lock = threading.Lock()

    def do_GET(self) -> None:
        with type(self).lock:
            type(self).request_count += 1
            attempt = type(self).request_count
        if attempt <= type(self).not_ready_count:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"not yet")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _start_scripted_server(not_ready_count: int) -> tuple[HTTPServer, threading.Thread, int]:
    handler_cls = type(
        "_ScopedHandler",
        (_ScriptedRequestHandler,),
        {"not_ready_count": not_ready_count, "request_count": 0, "lock": threading.Lock()},
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, thread, port


def test_provision_backups_notifies_user_after_retry_budget_exhausted(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A backup setup that keeps failing notifies the user once the retry budget is spent.

    Uses an API_KEY request with no RESTIC_REPOSITORY, which fails deterministically
    (no network) on every attempt. With a zero-second budget the loop makes a single
    attempt, then gives up and dispatches exactly one notification -- and must not let
    the exception escape the detached-thread entry point.
    """
    dispatcher = _RecordingNotificationDispatcher(is_electron=False, is_macos=False)
    creator = _make_test_creator(
        tmp_path,
        root_concurrency_group,
        notification_dispatcher=dispatcher,
        backup_setup_retry_budget_seconds=0.0,
        backup_setup_retry_wait_seconds=0.0,
    )
    request = BackupSetupRequest(backup_provider=BackupProvider.API_KEY, api_key_env_text="")

    creator._provision_backups(
        agent_id=AgentId.generate(),
        host_id="host-00000000000000000000000000000000",
        backup_request=request,
    )

    assert len(dispatcher.recorded) == 1
    notification, _agent_display_name = dispatcher.recorded[0]
    assert notification.title == "Backup setup failed"


def test_wait_for_workspace_ready_short_circuits_when_disabled(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Default construction (``mngr_forward_port=0``) skips the probe entirely."""
    creator = _make_test_creator(tmp_path, root_concurrency_group, mngr_forward_port=0, preauth_cookie="anything")
    log_q: queue.Queue[str] = queue.Queue()
    aid = AgentId.generate()
    started = time.monotonic()
    creator._wait_for_workspace_ready(aid, log_q)
    # Returns immediately -- no network calls, no log lines.
    assert time.monotonic() - started < 0.1
    assert log_q.empty()


def test_wait_for_workspace_ready_short_circuits_when_no_preauth(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Empty preauth cookie also disables the probe (the plugin requires auth)."""
    creator = _make_test_creator(tmp_path, root_concurrency_group, mngr_forward_port=8421, preauth_cookie="")
    log_q: queue.Queue[str] = queue.Queue()
    aid = AgentId.generate()
    started = time.monotonic()
    creator._wait_for_workspace_ready(aid, log_q)
    assert time.monotonic() - started < 0.1
    assert log_q.empty()


def test_wait_for_workspace_ready_returns_when_probe_succeeds(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The probe stops as soon as the (subdomain) endpoint returns 200."""
    server, _thread, port = _start_scripted_server(not_ready_count=2)
    try:
        creator = _make_test_creator(
            tmp_path,
            root_concurrency_group,
            mngr_forward_port=port,
            preauth_cookie="any-preauth",
            timeout_seconds=2.0,
            poll_interval_seconds=0.02,
            probe_timeout_seconds=0.5,
        )
        log_q: queue.Queue[str] = queue.Queue()
        # The probe connects to the plugin on loopback and carries the agent
        # vhost only in the Host header, so the http.server bound to 127.0.0.1
        # answers it without any ``*.localhost`` name resolution. Construct a
        # plausible-looking AgentId so the Host header is well-formed.
        aid = AgentId.generate()
        creator._wait_for_workspace_ready(aid, log_q)
    finally:
        server.shutdown()
    drained: list[str] = []
    while not log_q.empty():
        drained.append(log_q.get_nowait())
    assert any("Waiting for system interface" in line for line in drained)
    # Assert the *success* line specifically -- the timeout-warning line also
    # contains the word "ready", so a substring check would pass on a timeout.
    assert any("System interface is ready" in line for line in drained)


def test_wait_for_workspace_ready_calls_record_probe_success_on_ready(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Regression: a successful readiness probe must propagate to the health tracker.

    Without the ``record_probe_success`` call, the agent stays enrolled as a
    suspect probe target after an earlier ``system_interface_backend_failure``
    envelope, the background probe loop keeps accumulating a probe-failure run
    while the container warms up, and the agent would be driven to STUCK --
    landing the user on the recovery page seconds after their freshly created
    agent appeared healthy. See ``system_interface_health.py`` for the
    suspect / probe-failure-run lifecycle.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId.generate()
    # Enroll the agent as a suspect the way an in-flight warmup failure would.
    # The agent stays HEALTHY; we want to verify ``record_probe_success``
    # de-enrolls it so the background probe loop stops polling it.
    tracker.record_failure(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    server, _thread, port = _start_scripted_server(not_ready_count=0)
    try:
        creator = _make_test_creator(
            tmp_path,
            root_concurrency_group,
            mngr_forward_port=port,
            preauth_cookie="any-preauth",
            timeout_seconds=2.0,
            poll_interval_seconds=0.02,
            probe_timeout_seconds=0.5,
            system_interface_health_tracker=tracker,
        )
        creator._wait_for_workspace_ready(aid, queue.Queue())
    finally:
        server.shutdown()
    # ``record_probe_success`` de-enrolled the agent, so it is no longer a
    # probe target and the background loop will stop polling it.
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert aid not in tracker.snapshot_all()
    assert aid not in tracker.snapshot_probe_targets()


def test_probe_workspace_through_plugin_targets_root_path() -> None:
    """The probe hits ``/``, carrying the agent vhost in the Host header.

    Probing ``/`` deliberately decouples readiness from any particular app
    running inside the workspace: a 200 only confirms that some web server is
    answering on the inner port, with no assumption about which routes it
    implements.
    """
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    aid = AgentId.generate()
    with httpx.Client(transport=httpx.MockTransport(_capture)) as client:
        status = probe_workspace_through_plugin(
            mngr_forward_port=18999,
            preauth_cookie="any-preauth",
            agent_id=aid,
            probe_timeout_seconds=0.5,
            client=client,
        )

    assert status == 200
    assert len(captured) == 1
    assert captured[0].url.path == "/"
    # The agent vhost rides the Host header, not the URL host, so the probe
    # does not depend on ``*.localhost`` resolution.
    assert captured[0].headers["host"] == f"{aid}.localhost"


def test_probe_workspace_through_plugin_surfaces_non_200_status() -> None:
    """A non-200 from the probed route surfaces as that status (not None / not 200).

    When the inner port answers but not with a 200 (e.g. a 503 while the server
    is still warming up), the probe returns that status so the caller's
    ``== 200`` check treats the workspace as unready and the background loop
    records a probe failure, driving the agent toward STUCK.
    """

    def _capture(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text="Service Unavailable")

    with httpx.Client(transport=httpx.MockTransport(_capture)) as client:
        status = probe_workspace_through_plugin(
            mngr_forward_port=18999,
            preauth_cookie="any-preauth",
            agent_id=AgentId.generate(),
            probe_timeout_seconds=0.5,
            client=client,
        )

    assert status == 503


def test_wait_for_workspace_ready_publishes_anyway_on_timeout(
    tmp_path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """If the probe times out, we still return so the caller can publish the redirect."""
    server, _thread, port = _start_scripted_server(not_ready_count=10**6)
    try:
        creator = _make_test_creator(
            tmp_path,
            root_concurrency_group,
            mngr_forward_port=port,
            preauth_cookie="any-preauth",
            timeout_seconds=0.3,
            poll_interval_seconds=0.05,
            probe_timeout_seconds=0.2,
        )
        log_q: queue.Queue[str] = queue.Queue()
        aid = AgentId.generate()
        started = time.monotonic()
        creator._wait_for_workspace_ready(aid, log_q)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
    # The probe should give up around the timeout; allow a generous margin
    # so we don't flake under load.
    assert 0.2 <= elapsed <= 1.5
    drained: list[str] = []
    while not log_q.empty():
        drained.append(log_q.get_nowait())
    assert any("did not become ready" in line for line in drained)


# ---------------------------------------------------------------------------
# AI provider dispatch tests
#
# These exercise the new ``ai_provider`` match in ``_create_agent_background``
# end-to-end via ``start_creation`` -- the ``mngr create`` subprocess fails
# (we point at a nonexistent local path) but by then we've already gone
# through the AI-provider dispatch, so the recorded calls on the fake CLI
# tell us whether the right branch ran. The branch goal explicitly created
# the new combination "AIProvider.IMBUE_CLOUD with launch_mode != IMBUE_CLOUD",
# which we cover here.
# ---------------------------------------------------------------------------


class _RecordingImbueCloudCli(FakeImbueCloudCli):
    """``FakeImbueCloudCli`` that records ``create_litellm_key`` calls.

    Returns a stub :class:`LiteLLMKeyMaterial` instead of spawning the real
    ``mngr imbue_cloud keys litellm create`` subprocess so the test can run
    fully offline.
    """

    create_calls: list[dict[str, object]] = Field(default_factory=list)

    def create_litellm_key(
        self,
        *,
        account: str,
        alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> LiteLLMKeyMaterial:
        self.create_calls.append(
            {
                "account": account,
                "alias": alias,
                "max_budget": max_budget,
                "budget_duration": budget_duration,
                "metadata": dict(metadata) if metadata is not None else None,
            }
        )
        return LiteLLMKeyMaterial(
            key=SecretStr("sk-fake-litellm-key"),
            base_url=AnyUrl("https://litellm.example.com"),
        )


def _make_fake_repo(tmp_path: Path) -> Path:
    """Create a directory that ``_create_agent_background`` will accept as a local
    repo (it just needs to exist and not look like a git worktree)."""
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    return repo_dir


def _make_creator_with_cli(
    tmp_path: Path, cli: _RecordingImbueCloudCli, root_concurrency_group: ConcurrencyGroup
) -> AgentCreator:
    return AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        imbue_cloud_cli=cli,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )


def _wait_until_finished(creator: AgentCreator, creation_id: CreationId, deadline_seconds: float = 10.0) -> None:
    """Poll ``get_creation_info`` until status is DONE or FAILED, then return."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        info = creator.get_creation_info(creation_id)
        if info is not None and info.status in (AgentCreationStatus.DONE, AgentCreationStatus.FAILED):
            return
        threading.Event().wait(0.05)
    raise AssertionError(f"creation {creation_id} did not finish within {deadline_seconds}s")


def test_start_creation_imbue_cloud_ai_with_local_compute_mints_litellm_key(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The AIProvider.IMBUE_CLOUD branch must mint a LiteLLM key even when the compute
    provider is not IMBUE_CLOUD. The actual ``mngr create`` invocation will fail (no
    real binary / no real repo) but the key-mint must happen first, so the creation
    ends up FAILED only *after* getting past the mint."""
    cli = _RecordingImbueCloudCli(
        parent_concurrency_group=ConcurrencyGroup(name="recording-cli"),
        connector_url=FAKE_CONNECTOR_URL,
    )
    creator = _make_creator_with_cli(tmp_path, cli, root_concurrency_group)

    creation_id = creator.start_creation(
        repo_source=str(_make_fake_repo(tmp_path)),
        host_name="my-workspace",
        launch_mode=LaunchMode.DOCKER,
        ai_provider=AIProvider.IMBUE_CLOUD,
        account_email="alice@imbue.com",
    )
    _wait_until_finished(creator, creation_id)

    # The mint happened, then ``mngr create`` failed (no real binary / repo),
    # so the creation must have progressed past the mint to FAILED rather than
    # erroring out before minting.
    info = creator.get_creation_info(creation_id)
    assert info is not None
    assert info.status is AgentCreationStatus.FAILED

    assert len(cli.create_calls) == 1
    assert cli.create_calls[0]["account"] == "alice@imbue.com"
    assert cli.create_calls[0]["metadata"] == {"host_name": "my-workspace"}
    # NOTE: that the minted key/base_url are injected into the ``mngr create``
    # subprocess env (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``) is NOT
    # asserted here. The minted material flows from ``effective_anthropic_*``
    # in ``_create_agent_background`` into ``run_mngr_create``, which only
    # writes it into the real ``subprocess.run`` env (see
    # ``run_mngr_create`` in agent_creator.py, ~lines 826-831). The
    # ``_RecordingImbueCloudCli`` double records the *mint* call but offers no
    # seam to observe the subprocess env, and that env-injection is not yet
    # covered by any test. Adding a seam would require invasive production
    # plumbing, so it is left as a known coverage gap.


def test_start_creation_api_key_ai_does_not_mint_litellm_key(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The API_KEY branch uses the user-supplied key directly and must never call
    ``create_litellm_key``."""
    cli = _RecordingImbueCloudCli(
        parent_concurrency_group=ConcurrencyGroup(name="recording-cli"),
        connector_url=FAKE_CONNECTOR_URL,
    )
    creator = _make_creator_with_cli(tmp_path, cli, root_concurrency_group)

    creation_id = creator.start_creation(
        repo_source=str(_make_fake_repo(tmp_path)),
        host_name="my-workspace",
        launch_mode=LaunchMode.DOCKER,
        ai_provider=AIProvider.API_KEY,
        anthropic_api_key="sk-ant-user-supplied",
    )
    _wait_until_finished(creator, creation_id)

    assert cli.create_calls == []


def test_start_creation_subscription_ai_does_not_mint_litellm_key(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The SUBSCRIPTION branch injects no Anthropic creds and must never call
    ``create_litellm_key``."""
    cli = _RecordingImbueCloudCli(
        parent_concurrency_group=ConcurrencyGroup(name="recording-cli"),
        connector_url=FAKE_CONNECTOR_URL,
    )
    creator = _make_creator_with_cli(tmp_path, cli, root_concurrency_group)

    creation_id = creator.start_creation(
        repo_source=str(_make_fake_repo(tmp_path)),
        host_name="my-workspace",
        launch_mode=LaunchMode.DOCKER,
        ai_provider=AIProvider.SUBSCRIPTION,
    )
    _wait_until_finished(creator, creation_id)

    assert cli.create_calls == []


def test_start_creation_api_key_ai_without_key_fails_with_clear_message(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The API_KEY branch must reject an empty key with a specific error rather than
    silently falling through to mngr create with no key set."""
    cli = _RecordingImbueCloudCli(
        parent_concurrency_group=ConcurrencyGroup(name="recording-cli"),
        connector_url=FAKE_CONNECTOR_URL,
    )
    creator = _make_creator_with_cli(tmp_path, cli, root_concurrency_group)

    creation_id = creator.start_creation(
        repo_source=str(_make_fake_repo(tmp_path)),
        host_name="my-workspace",
        launch_mode=LaunchMode.DOCKER,
        ai_provider=AIProvider.API_KEY,
        anthropic_api_key="",
    )
    _wait_until_finished(creator, creation_id)

    info = creator.get_creation_info(creation_id)
    assert info is not None
    assert info.status is AgentCreationStatus.FAILED
    assert info.error is not None and "API_KEY" in info.error
