"""Shared non-fixture test helpers for desktop_client tests."""

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from flask import Flask
from loguru import logger as loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import ProducerRemediator
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import EnvironmentCondition
from imbue.minds.desktop_client.environment_signals import NetworkProber
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.desktop_client.environment_signals import SshEndpoint
from imbue.minds.desktop_client.latchkey.gateway_client import AccountsRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingAccess
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import PermissionEffect
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_ACCOUNTS
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_FILE_SHARING
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_PREDEFINED
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_WORKSPACE
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.gateway_client import WorkspaceRequestPayload
from imbue.minds.desktop_client.latchkey.pending_requests import PendingRequestsInterface
from imbue.minds.desktop_client.latchkey.response_events import RequestResponseEvent
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.restic_cli import _get_restic_binary
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import set_state
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiEnvironmentMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher
from imbue.minds.primitives import DeviceId
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import PersistedProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.testing import make_in_memory_test_ca
from imbue.mngr_forward.tls import build_server_ssl_context
from imbue.mngr_forward.tls import generate_server_credentials
from imbue.mngr_latchkey.core import LatchkeyError


def device_id_for_test(name: str) -> DeviceId:
    """Deterministic device id for a named fake device in tests (legacy host-id-shaped values)."""
    return DeviceId(f"host-{hashlib.sha256(name.encode()).hexdigest()[:32]}")


# -- Connectivity, without a network --

# Stand-in probe hosts. Deliberately unresolvable names, so a stub that somehow
# reached the real prober would fail rather than quietly measure the machine
# running the tests.
STUB_CONNECTIVITY_HOSTS: Final[tuple[str, ...]] = ("alpha.example", "beta.example", "gamma.example")

# The same hosts as the public quorum reaches them: port 22. A test that wants a
# network where public SSH works spells it with these.
PUBLIC_SSH_ENDPOINTS: Final[tuple[SshEndpoint, ...]] = tuple(
    SshEndpoint(host=host, port=22) for host in STUB_CONNECTIVITY_HOSTS
)


class StubNetworkProber(NetworkProber):
    """Answers the detector's two endpoint questions from settable sets of hosts.

    Injected in place of the socket-backed prober, so the detector under test
    runs its real quorum logic, caching, and callbacks against a network the
    test describes. Mutate the sets mid-test to bring the network up or down.
    """

    reachable_hosts: set[str] = Field(default_factory=set, description="Hosts that answer on the HTTPS port")
    ssh_endpoints: set[SshEndpoint] = Field(
        default_factory=set, description="host:port pairs that serve an SSH banner"
    )
    probed_endpoints: list[str] = Field(
        default_factory=list,
        description=(
            "Every endpoint asked about, in the order the rounds ran. Within one round the order is "
            "whichever of its threads was scheduled first, so an assertion about it has to sort"
        ),
    )

    def is_reachable(self, host: str, port: int) -> bool:
        self.probed_endpoints.append(f"{host}:{port}")
        return host in self.reachable_hosts

    def is_ssh_server(self, host: str, port: int) -> bool:
        self.probed_endpoints.append(f"ssh://{host}:{port}")
        return SshEndpoint(host=host, port=port) in self.ssh_endpoints


class SideEffectingStubNetworkProber(StubNetworkProber):
    """A stub prober that runs a callback as it is asked a round's first question.

    For the tests where something has to land *inside* a probe rather than
    around it. The production probe is seconds long, so whatever a caller read
    before it can move underneath it: a wake that disqualifies the measurement
    in flight, a stop that claims the machine the gate is deciding about, an
    error out of the discovery walk the endpoints come from. Here the callback
    is what moves it, and one that raises interrupts the probe, since it runs
    before the answer -- though not with the exception it raised: the round asks
    its hosts on the group's threads, which hand a raise back wrapped in a
    ``ConcurrencyExceptionGroup``. A test that turns on *which* family the probe
    failed with has to fail the walk itself.

    Disarms itself after firing. Set ``is_armed`` again for a test that needs a
    later probe interrupted too, or construct it disarmed to arm it per case.
    """

    on_first_question: Callable[[], None] = Field(description="Run as the round's first endpoint is asked")
    is_armed: bool = Field(default=True, description="Whether the next round's first question fires the callback")

    # The round asks its hosts on threads of its own, so the disarm has to
    # exclude the others: read-then-clear on its own lets two of them both see
    # an armed prober and fire a callback that is meant to happen once.
    _arming_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def is_reachable(self, host: str, port: int) -> bool:
        with self._arming_lock:
            is_firing = self.is_armed
            self.is_armed = False
        if is_firing:
            self.on_first_question()
        return super().is_reachable(host, port)


def _utc_now_for_test() -> datetime:
    return datetime.now(timezone.utc)


def build_connectivity_detector_over(
    prober: NetworkProber,
    concurrency_group: ConcurrencyGroup,
    *,
    poll_interval_seconds: float = 0.02,
    workspace_ssh_endpoints: tuple[SshEndpoint, ...] = (),
    now_fn: Callable[[], datetime] = _utc_now_for_test,
    shutdown_event: ReadOnlyEvent | None = None,
) -> ConnectivityDetector:
    """A real detector over ``prober``, on hosts that cannot resolve to a real one.

    What a test that brings its own prober wants: the detector's own quorum
    logic, caching, reading generations and callbacks, measuring the network the
    prober describes. Several of these files' probers subclass
    :class:`StubNetworkProber` to make something happen *inside* a round -- a
    wake, a stop, a raise -- which is why they cannot go through
    :func:`build_stub_connectivity_detector`, which builds its own.

    ``probe_hosts`` is not a parameter: every test wants
    :data:`STUB_CONNECTIVITY_HOSTS`, and a site that spelled its own could dial
    the real quorum and measure the machine running the suite instead.

    ``concurrency_group`` is the one both of the probe's rounds fan out under,
    the same way production hands it the app's root group -- so a test measures
    the rounds the app actually runs, including a group that refuses them. The
    ``root_concurrency_group`` fixture is one.
    """
    return ConnectivityDetector(
        prober=prober,
        probe_hosts=STUB_CONNECTIVITY_HOSTS,
        poll_interval_seconds=poll_interval_seconds,
        workspace_ssh_endpoints_fn=lambda: workspace_ssh_endpoints,
        now_fn=now_fn,
        shutdown_event=shutdown_event,
        concurrency_group=concurrency_group,
    )


def build_stub_connectivity_detector(
    concurrency_group: ConcurrencyGroup,
    *,
    is_internet_up: bool = True,
    is_ssh_up: bool = True,
    poll_interval_seconds: float = 0.02,
    workspace_ssh_endpoints: tuple[SshEndpoint, ...] = (),
    shutdown_event: ReadOnlyEvent | None = None,
) -> tuple[ConnectivityDetector, StubNetworkProber]:
    """A real detector over a stub prober, plus the prober so a test can change the network.

    ``concurrency_group`` is the one both of the probe's rounds fan out under,
    the same way production hands it the app's root group -- so a test measures
    the rounds the app actually runs. The ``root_concurrency_group`` fixture is
    one.

    ``workspace_ssh_endpoints`` are the endpoints minds itself would dial -- the
    ones the SSH facet asks about first. Empty (the default) leaves that facet on
    the public quorum alone, which is what an app with no remote machines has.

    ``is_ssh_up`` only has effect while ``is_internet_up``. A host that answers
    nothing on 443 cannot serve a banner on 22 either -- both facets dial the
    same three hosts -- so the two spelled independently would describe a network
    ``SocketNetworkProber`` cannot produce, and a test that reached the SSH facet
    over it would be measuring nothing real.

    ``shutdown_event`` is the app going down, for the tests about what a probe
    is allowed to do on the way out. Passed to the constructor rather than
    assigned afterwards, because the constructor is the only way production
    supplies it.

    Returns the detector unprobed: its reading is UNKNOWN until the test calls
    ``probe_now`` (or its background loop does), which is the same state a
    freshly-started or just-woken app is in.
    """
    prober = StubNetworkProber(
        reachable_hosts=set(STUB_CONNECTIVITY_HOSTS) if is_internet_up else set(),
        ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS) if is_internet_up and is_ssh_up else set(),
    )
    detector = build_connectivity_detector_over(
        prober,
        concurrency_group,
        poll_interval_seconds=poll_interval_seconds,
        workspace_ssh_endpoints=workspace_ssh_endpoints,
        shutdown_event=shutdown_event,
    )
    return detector, prober


def bring_stub_network_up(prober: StubNetworkProber) -> None:
    """Put the stub network back up: every probe host answers, on HTTPS and on SSH.

    The counterpart to ``build_stub_connectivity_detector``'s ``is_internet_up`` /
    ``is_ssh_up``, for the tests that take the network down and then restore it
    mid-test. Leaves the detector's cached reading alone -- a caller that needs
    the detector to *notice* wants :func:`bring_stub_network_back`, or its own
    ``probe_now`` where the number of probes is what it is measuring.
    """
    prober.reachable_hosts = set(STUB_CONNECTIVITY_HOSTS)
    prober.ssh_endpoints = set(PUBLIC_SSH_ENDPOINTS)


def bring_stub_network_back(detector: ConnectivityDetector, prober: StubNetworkProber) -> None:
    """Bring the stub network up and take the probe that observes it, as the poll loop would."""
    bring_stub_network_up(prober)
    detector.probe_now()


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

    Every RECOVERY_FAILED transition must reach error reporting (Principle 3:
    the recovery surface is quiet), so the recovery-failure tests assert exactly
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


def _empty_workspaces_message() -> UiWorkspacesMessage:
    return UiWorkspacesMessage(
        workspaces=(), destroying_agent_ids=(), restorable_workspace_ids=(), remote_workspace_states={}
    )


def build_ui_state_publisher_for_test(
    derive_workspaces: Callable[[], UiWorkspacesMessage] = _empty_workspaces_message,
    derive_health_states: Callable[[], tuple[UiHealthMessage, ...]] = tuple,
) -> tuple[UiStatePublisher, "queue.Queue[str | None]"]:
    """A publisher over a fresh broadcaster, plus the queue one registered window reads.

    Every derive answers an empty frame, because the tests that build one of
    these are about what the publisher *does* with the frames rather than what is
    in them. The two exceptions are the two a test can want to say something
    about: the workspace list a caller mutates between passes, and the health
    states a snapshot replays.

    Shared rather than rebuilt per file: the derive list is the publisher's
    constructor signature, so every frame added to the wire would otherwise have
    to be added to each copy, and a copy that was missed fails on a required
    field rather than on anything the test is about.
    """
    broadcaster = UiChannelBroadcaster()
    publisher = UiStatePublisher(
        broadcaster=broadcaster,
        derive_workspaces=derive_workspaces,
        derive_accounts=lambda: UiAccountsMessage(has_accounts=False, account_email="", extra_account_count=0),
        derive_providers=lambda: UiProvidersMessage(providers=(), last_event_at=None, last_full_snapshot_at=None),
        derive_requests=lambda: UiRequestsMessage(count=0, request_ids=()),
        derive_notifications=lambda: UiNotificationsMessage(entries=(), unresolved_count=0),
        derive_discovery_health=lambda: UiDiscoveryHealthMessage(state=DiscoveryHealth.HEALTHY),
        derive_environment=lambda: UiEnvironmentMessage(state=EnvironmentCondition.NONE),
        derive_health_states=derive_health_states,
    )
    return publisher, broadcaster.register()


# -- Backend resolvers, for the host lifecycle helpers that resolve agents --

_DEFAULT_WORKSPACE_AGENT_NAME: Final[AgentName] = AgentName("my-claude-agent")
# The provider every agent from :func:`build_resolver_with_system_services` sits
# on. Named so a test that has to seed a snapshot for that provider cannot drift
# from the builder it is describing.
SYSTEM_SERVICES_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("docker")


def build_resolver_with_system_services(
    workspace_agent: AgentId,
    services_agent: AgentId,
    *,
    host_id: HostId | None = None,
    host_state: HostState | None = None,
    workspace_agent_name: AgentName = _DEFAULT_WORKSPACE_AGENT_NAME,
    workspace_certified_data: Mapping[str, Any] | None = None,
    provider_name: ProviderInstanceName = SYSTEM_SERVICES_PROVIDER_NAME,
    provider_backend: str | None = None,
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

    ``provider_backend`` seeds a clean poll of ``provider_name`` naming that
    backend, which is what makes the machine's *locality* answerable here: this
    builder seeds no SSH coordinate, so the backend name is all its machines
    have to be judged by. Without one the resolver has agents on a provider it
    has never been told about, and ``is_network_dependent_provider`` answers from
    its "cannot identify it" fallback rather than from the backend. None (the
    default) leaves it that way, which is the right shape for a test that is not
    about locality at all.
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
                    provider_name=provider_name,
                    certified_data=workspace_certified_data if workspace_certified_data is not None else {},
                ),
                DiscoveredAgent(
                    host_id=resolved_host_id,
                    agent_id=services_agent,
                    agent_name=AgentName("system-services"),
                    provider_name=provider_name,
                ),
            ),
            host_state_by_host_id=({str(resolved_host_id): host_state} if host_state is not None else {}),
        )
    )
    if provider_backend is not None:
        seed_provider_backend(resolver, provider_name=str(provider_name), backend=provider_backend)
    return resolver


class SeededAgent(FrozenModel):
    """One machine for :func:`build_resolver_with_provider_backends` to report.

    Named fields rather than a positional tuple because two of them are provider
    strings that mean opposite things -- the provider *instance* a machine sits
    on, and the *backend* that instance runs -- and neither of them is what
    decides whether the machine is on this device. ``ssh_info`` is: a machine
    with a loopback coordinate is dialled here and one with any other coordinate
    is not, whatever backend it names. The backend answers only for a machine
    seeded without a coordinate. A machine with neither -- no coordinate and no
    backend, because no poll has ever described its provider -- is the third
    case, and the one every caller has to answer conservatively.
    """

    agent_id: AgentId = Field(description="The workspace agent discovery reports")
    provider_name: str = Field(description="Provider instance the agent's host runs on")
    backend: str | None = Field(
        default=None,
        description=(
            "Backend that provider instance runs (local / docker / imbue_cloud / ...), or None to leave "
            "the provider undescribed, as one no discovery poll has reported is"
        ),
    )
    ssh_info: RemoteSSHInfo | None = Field(default=None, description="SSH coordinate, or None for a host without one")
    host_state: HostState | None = Field(
        default=None, description="Host state, or None for a host discovery has not reported one for"
    )
    host_id: HostId | None = Field(
        default=None,
        description=(
            "Host to place this agent on. None gives it one of its own; share one to build the "
            "shape a real machine has, where the workspace agent and its system-services agent "
            "sit on the same host and so report the same SSH coordinate."
        ),
    )


def build_resolver_with_provider_backends(agents: tuple[SeededAgent, ...]) -> MngrCliBackendResolver:
    """A resolver reporting each :class:`SeededAgent` it is given, on its own host unless it names one."""
    hosted_agents = tuple(
        (agent.host_id if agent.host_id is not None else HostId.generate(), agent) for agent in agents
    )
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=tuple(agent.agent_id for agent in agents),
            discovered_agents=tuple(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=agent.agent_id,
                    agent_name=AgentName("machine"),
                    provider_name=ProviderInstanceName(agent.provider_name),
                )
                for host_id, agent in hosted_agents
            ),
            ssh_info_by_agent_id={
                str(agent.agent_id): agent.ssh_info for agent in agents if agent.ssh_info is not None
            },
            host_state_by_host_id={
                str(host_id): agent.host_state for host_id, agent in hosted_agents if agent.host_state is not None
            },
        )
    )
    for agent in agents:
        if agent.backend is not None:
            seed_provider_backend(resolver, provider_name=agent.provider_name, backend=agent.backend)
    return resolver


def seed_provider_backend(resolver: MngrCliBackendResolver, provider_name: str, backend: str) -> None:
    """Report a clean poll of ``provider_name``, naming the backend it runs.

    The backend is where ``is_network_dependent_provider`` starts -- a remote one
    settles it outright, an on-device one sends it on to its machines'
    coordinates -- and a resolver that has never been told about a provider
    answers "cannot identify it", which is a different code path from either. A
    test about any of them has to say which.
    """
    resolver.update_providers(
        ProviderInstanceName(provider_name),
        provider=DiscoveredProvider(
            provider_name=ProviderInstanceName(provider_name),
            config=PersistedProviderInstanceConfig(backend=ProviderBackendName(backend)),
        ),
        error=None,
        last_snapshot_at=datetime.now(timezone.utc),
    )


def build_resolver_with_provider_backend(
    agent_id: AgentId, provider_name: str, backend: str
) -> MngrCliBackendResolver:
    """A resolver reporting ``agent_id`` on a provider running ``backend``."""
    return build_resolver_with_provider_backends(
        (SeededAgent(agent_id=agent_id, provider_name=provider_name, backend=backend),)
    )


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


def install_stub_mngr_on_path(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    """Install an executable ``mngr`` stub in ``bin_dir``, first on ``PATH``, and return its path.

    For the desktop-client paths that resolve ``mngr`` the way production does
    -- via ``PATH`` -- so a test can shape what the real subprocess invocation
    sees without threading a binary path through the code under test.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = write_stub_mngr(bin_dir, MNGR_BINARY, body)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


# Iterations of the blocking stub's 0.05s poll before it gives up on its release
# file. Bounded because a pytest run killed mid-test orphans this detached shell
# with nothing left to write the file it waits for. Well clear of
# SUPPRESSION_WAIT_SECONDS, so only an abandoned run reaches the ceiling.
_BLOCKING_STUB_MAX_POLLS: Final[int] = 1200


def blocking_release_wait_body(release_path: Path) -> str:
    """Shell lines that poll for ``release_path``, exiting 1 at the bounded ceiling.

    The one release-wait fragment every blocking stub shares: the bound is what
    keeps a pytest run killed mid-test from leaving the orphaned, detached stub
    shell polling forever (see ``_BLOCKING_STUB_MAX_POLLS``).
    """
    return (
        "polls=0\n"
        f'while [ ! -f "{release_path}" ]; do\n'
        "  polls=$((polls + 1))\n"
        f"  [ $polls -ge {_BLOCKING_STUB_MAX_POLLS} ] && exit 1\n"
        "  sleep 0.05\n"
        "done"
    )


def write_blocking_stub_mngr(tmp_path: Path, name: str, release_path: Path) -> str:
    """A stub ``mngr`` that does not return until ``release_path`` appears.

    Stands in for a real stop, which runs for tens of seconds to minutes while
    the machine's system interface is already gone -- the window in which the
    intentional-stop mark has to hold.
    """
    return write_stub_mngr(tmp_path, name, blocking_release_wait_body(release_path) + "\nexit 0")


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
    readiness poll -- the create attempt's wait and the recovery worker's -- so
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


def exec_json_envelope(
    remote_stdout: str, *, success: bool = True, stderr: str = "", results_key: str = "results"
) -> str:
    """The ``mngr exec --format json`` envelope wrapping one remote result.

    ``results_key`` is ``"results"`` for in-container execs and
    ``"outer_results"`` for ``--outer`` ones, mirroring mngr's own output.
    """
    return json.dumps({results_key: [{"stdout": remote_stdout, "stderr": stderr, "success": success}]})


# -- Discovery-health watchdog, for its state machine and its loop --


class ManualClock:
    """A UTC clock that only moves when a test advances it.

    For the watchdog, whose backoff waits and stall threshold are durations
    between two of its own readings: a real clock would make those races. Also
    handed to a sleep tracker where a test needs the two to agree on now.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingProducerRemediator(ProducerRemediator):
    """Records remediation calls instead of touching a real supervisor.

    ``fail_restart``, when True, makes ``restart`` raise after recording the
    call -- mirroring a real supervisor restart that fails, which the watchdog
    must treat as "did not help" (retry), not give up.
    """

    calls: list[str] = Field(default_factory=list, description="Remediations requested, in order")
    fail_restart: bool = Field(default=False, description="Whether restart raises after recording the call")

    def bounce(self) -> None:
        self.calls.append("bounce")

    def restart(self) -> None:
        self.calls.append("restart")
        if self.fail_restart:
            raise LatchkeyError("simulated supervisor restart failure")


# -- Sleep signal, for the tracker and the loops that drive it --


class CatchUpClock:
    """A wall clock reported behind real time by a settable lag.

    Dropping the lag to zero produces exactly the heartbeat gap a real sleep
    produces: an interval whose end is *now*, the same now the tracker stamps
    its probe-failure runs with. A freely-advancing fake clock cannot stand in
    for it -- its intervals would end in the tracker's future, and every
    subsequent probe would keep re-reading the same sleep.
    """

    def __init__(self, lag_seconds: float) -> None:
        self.lag_seconds = lag_seconds

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=self.lag_seconds)


def make_sleep_tracker() -> tuple[SleepTracker, CatchUpClock]:
    """A tracker on a :class:`CatchUpClock` currently telling real time."""
    clock = CatchUpClock(lag_seconds=0.0)
    return SleepTracker(now_fn=clock), clock


def record_sleep_of(sleep_tracker: SleepTracker, clock: CatchUpClock, seconds: float) -> None:
    """Record one sleep interval of ``seconds`` ending now, through the real entry point."""
    clock.lag_seconds = seconds
    sleep_tracker.record_heartbeat()
    clock.lag_seconds = 0.0
    sleep_tracker.record_heartbeat()


def _streamed_request(
    agent_id: str,
    rationale: str,
    request_type: str,
    payload: PredefinedRequestPayload | FileSharingRequestPayload | WorkspaceRequestPayload | AccountsRequestPayload,
    target: str,
) -> StreamedPermissionRequest:
    """Assemble one gateway permission request with a fresh request id."""
    return StreamedPermissionRequest(
        request_id=f"req-{uuid.uuid4().hex}",
        agent_id=agent_id,
        rationale=rationale,
        request_type=request_type,
        payload=payload,
        target=target,
        effect=PermissionEffect(),
    )


def create_predefined_permission_request(
    agent_id: str,
    scope: str,
    rationale: str,
    permissions: tuple[str, ...] = (),
    account: str | None = None,
    target: str = "/tmp/permissions.json",
) -> StreamedPermissionRequest:
    """Build a predefined permission request as the gateway would stream it."""
    return _streamed_request(
        agent_id=agent_id,
        rationale=rationale,
        request_type=REQUEST_TYPE_PREDEFINED,
        payload=PredefinedRequestPayload(scope=scope, permissions=permissions, account=account),
        target=target,
    )


def create_file_sharing_permission_request(
    agent_id: str,
    path: str,
    access: str,
    rationale: str,
    target: str = "/tmp/permissions.json",
) -> StreamedPermissionRequest:
    """Build a file-sharing permission request as the gateway would stream it."""
    return _streamed_request(
        agent_id=agent_id,
        rationale=rationale,
        request_type=REQUEST_TYPE_FILE_SHARING,
        payload=FileSharingRequestPayload(path=path, access=FileSharingAccess(access)),
        target=target,
    )


def create_workspace_permission_request(
    agent_id: str,
    rationale: str,
    permissions: tuple[str, ...] = (),
    target_workspace_id: str | None = None,
) -> StreamedPermissionRequest:
    """Build a workspace permission request as the gateway would stream it."""
    return _streamed_request(
        agent_id=agent_id,
        rationale=rationale,
        request_type=REQUEST_TYPE_WORKSPACE,
        payload=WorkspaceRequestPayload(permissions=permissions, target_workspace_id=target_workspace_id),
        target="/tmp/permissions.json",
    )


def create_accounts_permission_request(
    agent_id: str,
    rationale: str,
) -> StreamedPermissionRequest:
    """Build an accounts permission request as the gateway would stream it."""
    return _streamed_request(
        agent_id=agent_id,
        rationale=rationale,
        request_type=REQUEST_TYPE_ACCOUNTS,
        payload=AccountsRequestPayload(),
        target="/tmp/permissions.json",
    )


class StaticPendingRequests(MutableModel, PendingRequestsInterface):
    """In-memory :class:`PendingRequestsInterface` for tests: fixed pending set, recorded verdicts."""

    pending: tuple[StreamedPermissionRequest, ...] = Field(default=(), description="The fixed pending set")
    answered: tuple[RequestResponseEvent, ...] = Field(
        default=(), description="Verdicts already recorded when the view is built"
    )
    recorded: list[RequestResponseEvent] = Field(
        default_factory=list, description="Verdicts recorded through the view, for assertions"
    )

    _responses_by_request_id: dict[str, RequestResponseEvent] = PrivateAttr(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def model_post_init(self, context: object) -> None:
        for event in self.answered:
            self._responses_by_request_id[event.request_event_id] = event

    def list_pending(self) -> tuple[StreamedPermissionRequest, ...]:
        return tuple(req for req in self.pending if req.request_id not in self._responses_by_request_id)

    def get_pending(self, request_id: str) -> StreamedPermissionRequest | None:
        return next((req for req in self.list_pending() if req.request_id == request_id), None)

    def is_resolved(self, request_id: str) -> bool:
        return request_id in self._responses_by_request_id

    def record_response(self, event: RequestResponseEvent) -> None:
        self._responses_by_request_id[event.request_event_id] = event
        self.recorded.append(event)

    def responses(self) -> tuple[RequestResponseEvent, ...]:
        return tuple(self._responses_by_request_id.values())


@contextmanager
def desktop_state_app_context(
    tmp_path: Path,
    pending_requests: StaticPendingRequests | None = None,
) -> Iterator[StaticPendingRequests]:
    """A minimal Flask app context carrying a DesktopClientState, for direct handler calls.

    Handler unit tests invoke grant/deny methods without the full desktop
    client; the shared resolve epilogue still reads ``get_state()`` for the
    pending-requests view and the backend resolver. Yields the view so tests
    can assert on what was recorded.
    """
    view = pending_requests if pending_requests is not None else StaticPendingRequests()
    app = Flask("minds-test-state")
    set_state(
        app,
        DesktopClientState(
            auth_store=FileAuthStore(data_directory=tmp_path / "auth-state"),
            backend_resolver=MngrCliBackendResolver(),
            pending_requests=view,
        ),
    )
    with app.app_context():
        yield view
