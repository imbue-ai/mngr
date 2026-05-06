"""Unit tests for agent_creator.

IMBUE_CLOUD-mode lease/rename/env-injection no longer happens in this
module: it runs inside ``ImbueCloudProvider.create_host``, reached
through the standard ``mngr create`` invocation. The plugin's own test
suite (``libs/mngr_imbue_cloud``) covers the lease + adopt path; this
file covers minds' command-building and helpers.
"""

import queue
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import _build_mngr_create_command
from imbue.minds.desktop_client.agent_creator import _is_git_worktree
from imbue.minds.desktop_client.agent_creator import _is_local_path
from imbue.minds.desktop_client.agent_creator import _make_host_name
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials
from imbue.minds.desktop_client.agent_creator import _redact_url_credentials_in_text
from imbue.minds.desktop_client.agent_creator import extract_repo_name
from imbue.minds.desktop_client.latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.primitives import AgentName
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId


def test_extract_repo_name_strips_dot_git_and_trailing_slash() -> None:
    assert extract_repo_name("https://github.com/user/repo.git") == "repo"
    assert extract_repo_name("https://github.com/user/repo/") == "repo"
    assert extract_repo_name("https://github.com/user/Some-Repo_Name") == "Some-Repo_Name"


def test_extract_repo_name_falls_back_to_workspace() -> None:
    assert extract_repo_name("/") == "workspace"
    assert extract_repo_name("///") == "workspace"


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


def test_make_host_name_appends_host_suffix() -> None:
    assert _make_host_name(AgentName("alpha")) == "alpha-host"


def test_build_mngr_create_command_injects_latchkey_for_non_dev_modes() -> None:
    """Container/VM/VPS/leased modes get the constant agent-side LATCHKEY_GATEWAY URL.

    The reverse tunnel that ``LatchkeyDiscoveryHandler`` sets up post-discovery bridges
    the agent's loopback at ``AGENT_SIDE_LATCHKEY_PORT`` to whichever host-side gateway
    port the discovery handler picked, so the URL is the same constant every time.
    """
    expected = f"LATCHKEY_GATEWAY=http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    for mode in (LaunchMode.LOCAL, LaunchMode.LIMA, LaunchMode.CLOUD):
        command, _ = _build_mngr_create_command(launch_mode=mode, agent_name=AgentName("hello"))
        assert expected in command, f"{mode} command missing latchkey env: {command}"
    # IMBUE_CLOUD requires an account to build the address; pass one and check the env var.
    command, _ = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        agent_name=AgentName("hello"),
        imbue_cloud_account="alice@imbue.com",
    )
    assert expected in command


def test_build_mngr_create_command_omits_latchkey_for_dev_mode() -> None:
    """DEV runs the agent on the bare host with no reverse tunnel, so no
    ``LATCHKEY_GATEWAY`` is injected. Tests that need one set it themselves."""
    command, _ = _build_mngr_create_command(launch_mode=LaunchMode.DEV, agent_name=AgentName("hello"))
    joined = " ".join(command)
    assert "LATCHKEY_GATEWAY" not in joined


def test_build_mngr_create_command_uses_main_template_and_omits_message_arg() -> None:
    command, api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.LOCAL,
        agent_name=AgentName("hello"),
    )
    assert "--template" in command
    assert "main" in command
    # The /welcome message now lives in forever-claude-template's
    # [create_templates.main] section, so the explicit --message arg is gone.
    assert "--message" not in command
    assert api_key
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
    command, api_key = _build_mngr_create_command(
        launch_mode=LaunchMode.IMBUE_CLOUD,
        agent_name=AgentName("hello"),
        imbue_cloud_account="alice@imbue.com",
        imbue_cloud_repo_url="https://github.com/imbue-ai/forever-claude-template",
        imbue_cloud_branch_or_tag="v1.2.3",
        imbue_cloud_anthropic_api_key="sk-test",
        imbue_cloud_anthropic_base_url="https://litellm.example.com",
    )
    joined = " ".join(command)
    # Address points at the imbue_cloud_<slug> provider so mngr routes
    # create_host to ImbueCloudProvider.
    assert "@hello-host.imbue_cloud_alice-imbue-com" in joined
    # IMBUE_CLOUD does not pass --reuse / --update (each lease is one-shot)
    # nor --id (the canonical id is parsed from the JSONL ``created`` event).
    assert "--id" not in command
    assert "--reuse" not in command
    assert "--update" not in command
    assert api_key
    # Lease attributes flow through --build-arg.
    assert "-b" in command
    assert "repo_url=https://github.com/imbue-ai/forever-claude-template" in command
    assert "repo_branch_or_tag=v1.2.3" in command
    # The actual ANTHROPIC_API_KEY / BASE_URL values are never on the
    # command line -- they land in the subprocess env that
    # ``run_mngr_create`` builds, and the template's ``pass_host_env``
    # forwards them onto the host. ``--pass-host-env`` itself isn't
    # inlined here either; the ``imbue_cloud`` template owns it.
    assert "ANTHROPIC_API_KEY=sk-test" not in command
    assert "ANTHROPIC_BASE_URL=https://litellm.example.com" not in command
    assert "--pass-host-env" not in command
    # IMBUE_CLOUD now uses the symmetric ``--template main --template imbue_cloud``
    # shape (mirroring how DEV/LOCAL/LIMA/CLOUD use ``--template main --template <provider>``).
    # The provider-specific knobs (idle_mode, pass_host_env) live in the
    # ``imbue_cloud`` template instead of being inlined here.
    assert "--template" in command
    template_args = [command[i + 1] for i, arg in enumerate(command) if arg == "--template" and i + 1 < len(command)]
    assert "main" in template_args
    assert "imbue_cloud" in template_args
    # ``--idle-mode disabled`` also moved into the template.
    assert "--idle-mode" not in command
    assert api_key


def test_is_git_worktree_returns_false_for_nonexistent_path(tmp_path) -> None:
    assert not _is_git_worktree(tmp_path / "no-such-dir")


def _make_test_creator(
    tmp_path,
    *,
    mngr_forward_port: int = 0,
    preauth_cookie: str = "",
    timeout_seconds: float = 1.0,
    poll_interval_seconds: float = 0.05,
    probe_timeout_seconds: float = 0.5,
) -> AgentCreator:
    paths = WorkspacePaths(data_dir=tmp_path)
    cg = ConcurrencyGroup(name="agent-creator-test")
    cg.__enter__()
    return AgentCreator(
        paths=paths,
        root_concurrency_group=cg,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        workspace_ready_timeout_seconds=timeout_seconds,
        workspace_ready_poll_interval_seconds=poll_interval_seconds,
        workspace_ready_probe_timeout_seconds=probe_timeout_seconds,
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


def test_wait_for_workspace_ready_short_circuits_when_disabled(tmp_path) -> None:
    """Default construction (``mngr_forward_port=0``) skips the probe entirely."""
    creator = _make_test_creator(tmp_path, mngr_forward_port=0, preauth_cookie="anything")
    log_q: queue.Queue[str] = queue.Queue()
    aid = AgentId.generate()
    started = time.monotonic()
    creator._wait_for_workspace_ready(aid, log_q)
    # Returns immediately -- no network calls, no log lines.
    assert time.monotonic() - started < 0.1
    assert log_q.empty()


def test_wait_for_workspace_ready_short_circuits_when_no_preauth(tmp_path) -> None:
    """Empty preauth cookie also disables the probe (the plugin requires auth)."""
    creator = _make_test_creator(tmp_path, mngr_forward_port=8421, preauth_cookie="")
    log_q: queue.Queue[str] = queue.Queue()
    aid = AgentId.generate()
    started = time.monotonic()
    creator._wait_for_workspace_ready(aid, log_q)
    assert time.monotonic() - started < 0.1
    assert log_q.empty()


def test_wait_for_workspace_ready_returns_when_probe_succeeds(tmp_path) -> None:
    """The probe stops as soon as the (subdomain) endpoint returns 200."""
    server, _thread, port = _start_scripted_server(not_ready_count=2)
    try:
        creator = _make_test_creator(
            tmp_path,
            mngr_forward_port=port,
            preauth_cookie="any-preauth",
            timeout_seconds=2.0,
            poll_interval_seconds=0.02,
            probe_timeout_seconds=0.5,
        )
        log_q: queue.Queue[str] = queue.Queue()
        # Use a localhost URL that resolves to the same server. Subdomains
        # of localhost all resolve to 127.0.0.1, so an http.server bound to
        # 127.0.0.1 answers regardless of the Host header. Construct a
        # plausible-looking AgentId so the probe URL is well-formed.
        aid = AgentId.generate()
        creator._wait_for_workspace_ready(aid, log_q)
    finally:
        server.shutdown()
    drained: list[str] = []
    while not log_q.empty():
        drained.append(log_q.get_nowait())
    assert any("Waiting for workspace" in line for line in drained)
    assert any("ready" in line.lower() for line in drained)


def test_wait_for_workspace_ready_publishes_anyway_on_timeout(tmp_path) -> None:
    """If the probe times out, we still return so the caller can publish the redirect."""
    server, _thread, port = _start_scripted_server(not_ready_count=10**6)
    try:
        creator = _make_test_creator(
            tmp_path,
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
