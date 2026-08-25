"""Shared non-fixture test helpers for desktop_client tests."""

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as loguru_logger
from pydantic import PrivateAttr

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.restic_cli import _get_restic_binary
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.testing import make_in_memory_test_ca
from imbue.mngr_forward.tls import build_server_ssl_context
from imbue.mngr_forward.tls import generate_server_credentials


def device_id_for_test(name: str) -> HostId:
    """Deterministic ``HostId``-shaped device id for a named fake device in tests."""
    return HostId(f"host-{hashlib.sha256(name.encode()).hexdigest()[:32]}")


class WriteCountingMindsConfig(MindsConfig):
    """MindsConfig double that counts config-file writes (each is one atomic replace)."""

    _write_count: int = PrivateAttr(default=0)

    def _write_raw(self, data: dict[str, object]) -> None:
        self._write_count += 1
        super()._write_raw(data)

    @property
    def write_count(self) -> int:
        return self._write_count


class ReadCountingMindsConfig(MindsConfig):
    """MindsConfig double that counts config-file reads: each is one lock-guarded load, so counting
    them proves whether a multi-field getter reads its fields under one lock acquisition or several."""

    _read_count: int = PrivateAttr(default=0)

    def _read_raw(self) -> dict[str, object]:
        self._read_count += 1
        return super()._read_raw()

    @property
    def read_count(self) -> int:
        return self._read_count


def is_workspace_options_pane_hidden(html: str, pane: str) -> bool:
    """Whether the workspace options panel ships ``pane`` hidden (it must ship both).

    Reads the ``hidden`` class off the pane rather than matching its whole class
    attribute, which also carries the layout that lets the pane pin its title
    and nav and scroll its right side. Explodes if the pane is not in the HTML
    at all, so a test cannot pass by asserting a missing pane is not shown.
    """
    match = re.search(rf'data-wsopt-panel="{re.escape(pane)}" class="([^"]*)"', html)
    assert match is not None, f"no {pane!r} pane in the rendered options panel"
    return "hidden" in match.group(1).split()


def workspace_options_pane_html(html: str, pane: str) -> str:
    """The markup of one pane of the workspace options panel, for asserting on its layout.

    The panel ships both panes, so a naive substring search cannot tell which
    one it matched. This slices from the pane's own element to the start of the
    next pane (or the end), which is enough because the two are siblings.
    """
    start = html.find(f'data-wsopt-panel="{pane}"')
    assert start != -1, f"no {pane!r} pane in the rendered options panel"
    next_pane = html.find("data-wsopt-panel=", start + 1)
    return html[start:] if next_pane == -1 else html[start:next_pane]


def tamper_session_cookie_signed_content(cookie_value: str) -> str:
    """Return a copy of a session cookie altered so it can never re-verify.

    A session cookie is an itsdangerous ``signed-content.signature`` token whose
    signature is an HMAC over the signed-content string; the signature is the
    only segment a verifier base64-decodes, so a flip in its base64 tail can be
    absorbed by the tail's spare bits and still verify. Altering the signed
    content instead -- anything left of the final "." -- always changes the HMAC
    input, so it is rejected whatever the payload.
    """
    signed_content, separator, signature = cookie_value.rpartition(".")
    assert separator, f"not a signed token: {cookie_value!r}"
    flipped_head = ("A" if signed_content[0] != "A" else "B") + signed_content[1:]
    return flipped_head + separator + signature


@contextmanager
def capture_error_logs() -> Iterator[list[str]]:
    """Capture loguru ERROR-level records (a loguru sink; caplog can't hook loguru).

    Every RESTART_FAILED transition must reach error reporting (Principle 3:
    the recovery surface is quiet), so the restart-failure tests assert exactly
    one error record per attempt through this capture.
    """
    records: list[str] = []
    sink_id = loguru_logger.add(lambda msg: records.append(str(msg)), level="ERROR")
    try:
        yield records
    finally:
        loguru_logger.remove(sink_id)


def drain_ui_channel_frames(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every frame waiting on one ``/ui/ws`` connection's queue, parsed, in order.

    Takes the queue ``UiChannelBroadcaster.register`` hands a connection, which
    is how a test stands in for a window without a socket. ``None`` on it is the
    eviction/shutdown sentinel rather than a frame, so it is skipped.
    """
    frames: list[dict[str, Any]] = []
    is_drained = False
    while not is_drained:
        try:
            raw = client_queue.get_nowait()
        except queue.Empty:
            is_drained = True
            continue
        if raw is None:
            continue
        frames.append(json.loads(raw))
    return frames


# -- Backend resolvers, for the host lifecycle helpers that resolve agents --

_DEFAULT_WORKSPACE_AGENT_NAME: Final[AgentName] = AgentName("my-claude-agent")


def build_resolver_with_system_services(
    workspace_agent: AgentId,
    services_agent: AgentId,
    *,
    host_id: HostId | None = None,
    host_state: HostState | None = None,
    workspace_agent_name: AgentName = _DEFAULT_WORKSPACE_AGENT_NAME,
    workspace_certified_data: Mapping[str, Any] | None = None,
) -> MngrCliBackendResolver:
    """Build a resolver where the machine agent and system-services agent share a host.

    The shape every host lifecycle helper resolves against: it is the
    system-services agent beside the workspace that stop / start / restart
    actually target.

    ``host_state`` records an observed lifecycle state for that shared host in
    the snapshot; None leaves the host state undiscovered.
    ``workspace_certified_data`` carries the workspace's ``data.json`` fields --
    the ``workspace`` / ``is_primary`` labels a caller needs when it reads
    liveness rather than just resolving agents.
    """
    resolved_host_id = host_id if host_id is not None else HostId.generate()
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=resolved_host_id,
                    agent_id=workspace_agent,
                    agent_name=workspace_agent_name,
                    provider_name=ProviderInstanceName("docker"),
                    certified_data=workspace_certified_data if workspace_certified_data is not None else {},
                ),
                DiscoveredAgent(
                    host_id=resolved_host_id,
                    agent_id=services_agent,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName("docker"),
                ),
            ),
            host_state_by_host_id=({str(resolved_host_id): host_state} if host_state is not None else {}),
        )
    )
    return resolver


def record_provider_discovery_error(
    resolver: MngrCliBackendResolver, provider_name: str, message: str, last_snapshot_at: datetime | None = None
) -> None:
    """Surface a discovery error for ``provider_name``, as an errored poll would.

    The snapshot time defaults to now, so the reading is fresh enough for the
    freshness-gated recovery verdicts. Pass ``last_snapshot_at`` to place the
    errored poll at a particular moment relative to an outage onset -- it must be
    set here rather than afterwards, because a later clean snapshot is what
    *clears* the error.
    """
    resolver.update_providers(
        ProviderInstanceName(provider_name),
        provider=None,
        error=DiscoveryError(
            type_name="ProviderUnavailableError",
            message=message,
            provider_name=ProviderInstanceName(provider_name),
        ),
        last_snapshot_at=last_snapshot_at if last_snapshot_at is not None else datetime.now(timezone.utc),
    )


# -- Stub mngr binaries, for the host lifecycle helpers that shell out --


def write_stub_mngr(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable stub standing in for ``mngr`` with ``body`` as its script."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


# Iterations of the blocking stub's 0.05s poll before it gives up on its release
# file. Bounded because a pytest run killed mid-test orphans this detached shell
# with nothing left to write the file it waits for. Well clear of
# SUPPRESSION_WAIT_SECONDS, so only an abandoned run reaches the ceiling.
_BLOCKING_STUB_MAX_POLLS: Final[int] = 1200


def write_blocking_stub_mngr(tmp_path: Path, name: str, release_path: Path) -> str:
    """A stub ``mngr`` that does not return until ``release_path`` appears.

    Stands in for a real stop, which runs for tens of seconds to minutes while
    the machine's system interface is already gone -- the window in which the
    intentional-stop mark has to hold.
    """
    body = (
        "polls=0\n"
        f'while [ ! -f "{release_path}" ]; do\n'
        "  polls=$((polls + 1))\n"
        f"  [ $polls -ge {_BLOCKING_STUB_MAX_POLLS} ] && exit 1\n"
        "  sleep 0.05\n"
        "done\n"
        "exit 0"
    )
    return write_stub_mngr(tmp_path, name, body)


# Ceiling on "the blocking command has reached the point where it marks the
# tracker": the wait ends the instant the mark lands, so this only bounds a
# failing run. Kept inside the suite's own ``--timeout=10`` per-test budget, so
# a regression fails on the assertion that says what went wrong rather than on
# pytest's opaque timeout.
SUPPRESSION_WAIT_SECONDS: Final[float] = 5.0


class SuppressionAnnouncingTracker(SystemInterfaceHealthTracker):
    """A tracker that signals when an intentional-stop mark is set.

    Lets a test observe the mark *while* the command that set it is still
    running, rather than polling for a state it might miss: the window under
    test opens the moment the stop marks and closes when its ``mngr`` returns.
    """

    _suppression_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def suppress_unattended_recovery(self, agent_id: AgentId, *, is_stop_in_flight: bool = False) -> None:
        super().suppress_unattended_recovery(agent_id, is_stop_in_flight=is_stop_in_flight)
        self._suppression_event.set()

    def wait_for_suppression(self, timeout_seconds: float) -> bool:
        """Block until the mark is set, reporting whether it arrived in time."""
        return self._suppression_event.wait(timeout=timeout_seconds)


def restic_backup_a_file(repository: str, password: str, source: Path) -> None:
    """Create one snapshot in ``repository`` from ``source`` using plain restic."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        [_get_restic_binary(), "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr


class _ScriptedWorkspaceProbeHandler(BaseHTTPRequestHandler):
    """Stands in for the ``mngr forward`` plugin: 503 for the first N probes, then 200.

    Models a container that is still booting: the plugin itself answers, but the
    inner system interface is not listening yet, which is the 503 the real
    plugin's auto-refresh page returns.
    """

    not_ready_count: int = 0
    request_count: int = 0
    lock: threading.Lock = threading.Lock()
    # Fired on the first probe. Lets a test act at the exact moment a readiness
    # wait is known to have started, instead of racing it with a sleep.
    on_first_request: Callable[[], None] | None = None

    def do_GET(self) -> None:
        with type(self).lock:
            type(self).request_count += 1
            attempt = type(self).request_count
        on_first_request = type(self).on_first_request
        if attempt == 1 and on_first_request is not None:
            on_first_request()
        is_booting = attempt <= type(self).not_ready_count
        self.send_response(503 if is_booting else 200)
        self.end_headers()
        self.wfile.write(b"booting" if is_booting else b"ok")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def scripted_workspace_probe_server(
    not_ready_count: int, on_first_request: Callable[[], None] | None = None
) -> Iterator[int]:
    """Serve a plugin stand-in on loopback, yielding its port.

    Answers 503 for the first ``not_ready_count`` probes and 200 thereafter, so a
    readiness wait sees a workspace that becomes reachable partway through
    (``10**6`` stands in for "never ready"). Shared by every test that drives a
    readiness poll -- the create attempt's wait and the restart worker's -- so
    both exercise the same stand-in.

    Speaks TLS with the proxy's own CA-backed cert helpers: minds always runs
    ``mngr forward`` with HTTP/2, so a readiness probe dials https and would fail
    the handshake against a plain-HTTP socket.
    """
    handler_cls = type(
        "_ScopedWorkspaceProbeHandler",
        (_ScriptedWorkspaceProbeHandler,),
        {
            "not_ready_count": not_ready_count,
            "request_count": 0,
            "lock": threading.Lock(),
            # Wrapped in staticmethod so the class attribute stays a plain
            # callable rather than binding as a method on each handler instance.
            "on_first_request": staticmethod(on_first_request) if on_first_request is not None else None,
        },
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    ca = make_in_memory_test_ca()
    chain_pem, key_pem = generate_server_credentials(ca)
    server.socket = build_server_ssl_context(chain_pem, key_pem, ca).wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
