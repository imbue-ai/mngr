"""Workspace recovery: the passive backend verdict + the restart worker.

These are the engine behind the recovery flow (the recovery card's state and its
host restart action). They are extracted here -- away from :mod:`app` -- so the
versioned ``/api/v1`` surface (:mod:`api_v1`) can drive them without importing
:mod:`app` (which would form an import cycle, since ``app`` imports ``api_v1``).

``read_backend_unreachable_verdict`` answers "can minds reach the provider that
hosts this machine at all?" from evidence already in hand, so a polling surface
pays nothing for it. ``run_restart_sequence`` is the background worker body
(``mngr stop`` + ``mngr start``, then await recovery) that drives both the
:class:`SystemInterfaceHealthTracker` (so the recovery surfaces re-label) and a
:class:`WorkspaceOperationRegistryInterface` (so the v1
``/workspaces/operations/restart/<id>`` resource can report restart status + logs).

The environment signals meet discovery here too. :mod:`environment_signals`
supplies the raw facts -- has this process been running, can this device reach
anything -- and is deliberately a leaf that knows nothing of machines or
providers, so everything that answers one of its questions *from discovery*
lives on this side of the line: :class:`WorkspaceSshEndpointSource` (the
endpoints minds itself dials, which is what the SSH facet measures),
:class:`ProviderErrorConnectivityTrigger` (a provider discovery cannot reach is
the earliest evidence a cold start on a dead network produces),
:func:`is_network_dependent_workspace` / :func:`is_network_dependent_provider`
(the on-device rule that exempts local, docker and lima from all of it), and
:func:`read_environment_block` (what the recovery surfaces render). They sit
with the restart paths that consult them: the gate on
:class:`UnattendedRecoveryDispatcher` is the one place a signal withholds an
action rather than merely explaining one.
"""

import json
import os
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_creator import WORKSPACE_READY_TIMEOUT_SECONDS
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.environment_signals import SshEndpoint
from imbue.minds.desktop_client.mngr_command import format_output_tail
from imbue.minds.desktop_client.mngr_command import mngr_failure_verdict
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_lifecycle import HOST_START_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_lifecycle import HOST_STOP_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.errors import MindError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.api.discovery_events import DISCOVERY_STREAM_POLL_INTERVAL_SECONDS
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import parse_provider_unavailable_reason
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason

# Stand-in provider name for the "Can't connect to ..." headline, for a provider
# discovery has not named yet (or does not recognise).
_DEFAULT_PROVIDER_LABEL: Final[str] = "the machine backend"
# Reason shown for the UNAUTHENTICATED host state. Discovery carries only the
# host state (``DiscoveredHost`` has no failure_reason field), so there is no
# provider error to show verbatim; this covers the class of causes instead.
HOST_ACCESS_REJECTED_REASON: Final[str] = (
    "This machine's access to the machine host was rejected. You may need to recreate the machine or contact support."
)
# How long a single workspace probe through the plugin is allowed to hang.
# Short and snappy so a wedged workspace doesn't gate the recovery UI.
_WORKSPACE_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
# Default hard timeout for an ``mngr`` subprocess run via ``_run_mngr``. A
# "definitely wedged" ceiling, not an estimate -- generous for quick calls like
# a container bounce. The restart sequence's host stop/start steps do NOT use
# it: a host stop can mirror state for minutes (BYO cloud) and a host start can
# restore a workspace from object storage (imbue_cloud), so those steps pass
# the shared ``workspace_lifecycle`` budgets explicitly.
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
# How long we wait for the system interface to answer again after a restart.
# ``mngr start`` cold-boots the container, so this waits for exactly the event
# the create flow's readiness wait already measures -- a cold-booted workspace's
# system interface answering 200 through the plugin. Sized from that one
# calibrated number rather than a second, independent guess: a restart's boot
# does strictly less work than a first boot (the workspace is already
# provisioned; the worst case, a provider that can only recreate the host from a
# snapshot, is the first boot again), so the create budget is a sound ceiling
# and the two cannot drift into contradicting each other about a cold boot.
#
# The previous value was an independent 30s, well under the 90-180s a cold boot
# regularly takes, so an ordinary slow-but-successful restart tripped the
# failure branch below and error-logged. The workspace then came up anyway: the
# RESTART_FAILED that branch sets is itself a background-probe target (a
# RESTARTING one is not), so the health probe loop picked the workspace up and
# quietly flipped it to HEALTHY once the boot finished -- which is what made the
# report a false alarm rather than a symptom anyone could act on.
_HOST_RESTART_STARTUP_WAIT_SECONDS: Final[float] = WORKSPACE_READY_TIMEOUT_SECONDS
# Poll cadence while waiting for the system interface to come back post-restart.
_RESTART_PROBE_INTERVAL_SECONDS: Final[float] = 1.0
# How many consecutive missed snapshots mean the discovery pipeline has stalled,
# so the state it last reported can no longer be trusted. Multiplied by the
# cadence of whichever loop produced the snapshot being aged (see
# :func:`_workspace_provider_poll_interval_seconds`), it stays above the normal
# inter-snapshot interval to avoid a false "stale" during a single slow poll.
_DISCOVERY_FRESHNESS_MISSED_SNAPSHOT_COUNT: Final[int] = 3
# How recent a connectivity reading may be and still answer a gate that is about
# to withhold a restart. A dropped network wedges every remote workspace on the
# same probe tick, so their gates arrive together; without this each would queue
# behind the last and re-measure the same network, putting tens of seconds
# between the first machine's verdict and the last's. Short enough that the
# reading still describes the network the decision is being made on.
_GATE_READING_REUSE_SECONDS: Final[float] = 2.0
# Provider backends whose machines live on this device. Their workspaces are
# reachable with the network unplugged, so no connectivity reading ever gates or
# explains anything about them. Not ``mind_liveness``'s set of the same shape:
# that one is scoped to backends minds can also shut down, which excludes
# ``local`` -- and a local workspace is the last one a dead network should be
# allowed to withhold a restart from.
_ON_DEVICE_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset({"local", "docker", "lima"})


class WorkspaceSshEndpointSource(MutableModel):
    """Supplies the connectivity detector with the SSH endpoints minds actually dials.

    The detector needs to know whether *this device* can open the connections
    minds depends on, and those are not on port 22: each machine's host is
    reached on whatever port its provider forwarded, which for imbue_cloud is a
    box-forwarded port in the 22000-32000 range. Discovery already reports the
    real coordinate per agent -- it is the same one the recovery card renders as
    a copy-pasteable ``ssh`` command -- so this hands it over rather than
    guessing.

    Read fresh on every call: a probe taken minutes later must ask about the
    machines that exist then. Deduped, because the agents on one host share its
    endpoint and probing it three times measures nothing extra.

    Machines on a backend that runs on this device are left out. Discovery
    reports SSH info for them like any other host -- a docker container is
    reached at ``127.0.0.1`` on the port its daemon mapped, not through some
    local channel -- and an endpoint that answers with the wifi off cannot say
    anything about the network. Handing one over would settle the SSH facet as
    reachable on every probe, which is the facet reading the whole
    incompatible-network verdict rests on.

    Endpoints on hosts discovery reports as RUNNING come first, because the
    detector samples only the first few and one endpoint answering settles the
    facet. A host that is stopped, crashed, or in a state discovery has not
    reported cannot answer whatever the network is doing, so sampling it spends
    part of a bounded sample on a question it cannot resolve -- and every
    sampled endpoint failing is what sends the facet to the public quorum, where
    a network that blocks port 22 in particular then reads as blocking SSH
    outright and withholds a restart the machine may genuinely need. Ordering
    rather than filtering: on a dead network discovery goes stale too, so a
    reading taken when nothing is known to be running must still be able to ask
    about something.
    """

    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Discovery's view of the machines.")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __call__(self) -> tuple[SshEndpoint, ...]:
        endpoints_on_running_hosts: list[SshEndpoint] = []
        other_endpoints: list[SshEndpoint] = []
        for agent_id in self.backend_resolver.list_known_agent_ids():
            if not is_network_dependent_workspace(self.backend_resolver, agent_id):
                continue
            ssh_info = self.backend_resolver.get_ssh_info(agent_id)
            if ssh_info is None:
                continue
            endpoint = SshEndpoint(host=ssh_info.host, port=ssh_info.port)
            if endpoint in endpoints_on_running_hosts or endpoint in other_endpoints:
                continue
            if self._is_host_running(agent_id):
                endpoints_on_running_hosts.append(endpoint)
            else:
                other_endpoints.append(endpoint)
        return tuple(endpoints_on_running_hosts) + tuple(other_endpoints)

    def _is_host_running(self, agent_id: AgentId) -> bool:
        """Whether discovery reports this agent's host as RUNNING right now.

        False for every other answer, including the unknown ones: this only
        decides which endpoints are asked first, so a host discovery cannot
        currently describe simply loses its head start.
        """
        display_info = self.backend_resolver.get_agent_display_info(agent_id)
        if display_info is None:
            return False
        return read_host_state(self.backend_resolver, display_info) is HostState.RUNNING


class ProviderErrorConnectivityTrigger(MutableModel):
    """Probes the device when discovery reports a provider it cannot reach.

    The gate on the STUCK edge only ever asks about the network once a *machine*
    has been convicted, and a machine can only be convicted once something has
    tried to reach it. That leaves the case this exists for: minds opened on a
    dead network. Discovery's first poll of a remote provider fails immediately,
    the machines list renders rows nobody can reach, and until the user clicks
    into one there is nothing to convict and so nothing to say -- which is
    precisely when they most need telling that the problem is their wifi.

    A remote provider erroring is the earliest evidence available, and it costs
    nothing to act on: the probe answers the question the user is about to ask,
    and a healthy answer settles it. Local backends are ignored -- a stopped
    docker daemon errors the same way and says nothing about the network.

    Registered on the resolver's change callbacks, which fire on every discovery
    event, so the probe is taken on a worker (it blocks for seconds) and once
    per *episode* rather than once per event. The episode is the set of errored
    network-dependent providers: a provider can stay errored for the life of the
    app for reasons of its own -- a revoked token, a backend outage of its own --
    and re-measuring on every event would make this a background network poll,
    which is precisely what the detector does not do. One probe answers the
    question; if the answer is bad, the detector's own watching loop takes over
    the re-probing until it clears. Nothing here reads the result: the detector
    publishes it.
    """

    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Discovery's view of the providers.")
    connectivity_detector: ConnectivityDetector = Field(frozen=True, description="Detector to ask.")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for the probe worker.")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Whether a probe worker is already in flight. The resolver fires far faster
    # than a probe completes, and every one of those events reports the same
    # provider error; without this they would queue on the detector's probe lock.
    _is_probe_in_flight: bool = PrivateAttr(default=False)
    # The errored network-dependent providers the last probe was taken for, or
    # None while none are erroring. Keyed on the set rather than a bare flag so a
    # second provider going dark -- which is what a network dying underneath an
    # already-broken one looks like -- is new evidence and gets its own probe.
    _measured_provider_errors: frozenset[ProviderInstanceName] | None = PrivateAttr(default=None)

    def __call__(self) -> None:
        errored_providers = self._unreachable_remote_providers()
        if not errored_providers:
            with self._lock:
                self._measured_provider_errors = None
            return
        with self._lock:
            if self._is_probe_in_flight or errored_providers == self._measured_provider_errors:
                return
            self._is_probe_in_flight = True
            self._measured_provider_errors = errored_providers
        try:
            self.concurrency_group.start_new_thread(
                target=self._probe_once,
                name="connectivity-provider-error-probe",
                daemon=True,
                is_checked=False,
            )
        # An exited or shutting-down group raises one of these; this runs on the
        # resolver's callback thread, so an escape would take that with it.
        except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
            # The episode goes back to unmeasured with the flag: a probe that
            # never ran must not latch the errors it was going to ask about.
            with self._lock:
                self._is_probe_in_flight = False
                self._measured_provider_errors = None
            # Warning, like the gate's own spawn failure: this trigger is the only
            # thing that raises the app-level notice on a cold start over a dead
            # network, and it says nothing on its success path -- so at debug level
            # a bundle could not tell a failed probe from one that never fired.
            logger.warning("Could not start the provider-error connectivity probe: {}", exc)

    def _probe_once(self) -> None:
        """Worker body: measure the device once for this episode of provider errors.

        Fenced with the families ``probe_now`` can reach here with -- the same
        ones ``ConnectivityDetector.run_background_loop`` names for the same
        call. A probe that raised leaves the episode unmeasured, so it is
        un-latched with the in-flight flag rather than left recorded as asked:
        this trigger is the only thing that raises the app-level notice on a
        cold start over a dead network, and latching an episode nothing measured
        would leave the hub pages silent for the rest of it.
        """
        is_measured = False
        try:
            self.connectivity_detector.probe_now(max_reuse_age_seconds=_GATE_READING_REUSE_SECONDS)
            is_measured = True
        except (
            MindError,
            MngrError,
            OSError,
            RuntimeError,
            ValueError,
            ConcurrencyGroupError,
            ConcurrencyExceptionGroup,
        ) as exc:
            logger.opt(exception=exc).warning("The provider-error connectivity probe failed: {}", exc)
        finally:
            with self._lock:
                self._is_probe_in_flight = False
                if not is_measured:
                    self._measured_provider_errors = None

    def _unreachable_remote_providers(self) -> frozenset[ProviderInstanceName]:
        """The providers discovery is reporting an error for that need the network."""
        return frozenset(
            provider_name
            for provider_name in self.backend_resolver.get_provider_errors()
            if is_network_dependent_provider(self.backend_resolver, provider_name)
        )


def is_network_dependent_workspace(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    """Whether reaching this workspace requires the device to have a working network.

    Workspaces on a backend that runs on this device (``local``, ``docker``,
    ``lima``) are reachable over loopback with the wifi off, so a connectivity
    reading has nothing to say about them: it must never withhold their restart,
    and must never be offered as the explanation for their failure.

    Everything else answers True, including a workspace whose provider or
    backend cannot be identified. That is the conservative direction for this
    question and only for this question: a wrong True can at most delay an
    unattended restart until a probe confirms the network is fine (and that
    probe is what the caller is about to run anyway), whereas a wrong False
    would restore exactly the doomed-dispatch behaviour this exists to prevent.
    Nothing here suppresses anything on its own -- a confirmed-bad reading is
    still required for that.
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return True
    display_info = backend_resolver.get_agent_display_info(agent_id)
    if display_info is None or display_info.provider_name is None:
        return True
    return is_network_dependent_provider(backend_resolver, ProviderInstanceName(display_info.provider_name))


def is_network_dependent_provider(
    backend_resolver: BackendResolverInterface, provider_name: ProviderInstanceName
) -> bool:
    """Whether reaching this provider's machines requires the device to have a working network.

    The provider-level half of :func:`is_network_dependent_workspace`, split out
    because one caller starts from a provider rather than a machine: a provider
    discovery reports as unreachable is evidence about the network *before* any
    machine has been convicted, which is the only evidence a cold start on a
    dead network produces.

    Answers True for a provider it cannot identify, for the same reason and with
    the same safety as the per-workspace form: a wrong True costs one probe.
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return True
    for provider in backend_resolver.list_providers():
        if provider.provider_name == provider_name:
            return str(provider.config.backend) not in _ON_DEVICE_PROVIDER_BACKENDS
    return True


def _is_discovery_fresh(last_snapshot_at: datetime | None, poll_interval_seconds: float) -> bool:
    """Whether the most recent discovery snapshot is recent enough to trust.

    A snapshot older than ``_DISCOVERY_FRESHNESS_MISSED_SNAPSHOT_COUNT`` polls of
    ``poll_interval_seconds`` (or no snapshot at all) means discovery has stalled
    -- the resolver's host state may pre-date an outage -- so reachability cannot
    be positively established.

    The cadence is a parameter because the snapshot being aged is a *provider's*,
    and each provider re-polls on its own configurable interval: measuring one
    against the stream-wide baseline instead reads a provider that polls more
    slowly than the baseline as permanently stale.
    """
    if last_snapshot_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - last_snapshot_at).total_seconds()
    return age_seconds <= _DISCOVERY_FRESHNESS_MISSED_SNAPSHOT_COUNT * poll_interval_seconds


def _workspace_provider_poll_interval_seconds(
    backend_resolver: MngrCliBackendResolver, provider_name: str | None
) -> float:
    """``provider_name``'s own configured discovery cadence, or the stream baseline.

    Discovery reports each provider's ``discovery_poll_interval_seconds`` on its
    snapshots, and a provider that errored keeps its last reported config, so the
    real cadence is in hand for exactly the outages this is used to age. The
    baseline answers for a provider discovery has never described -- which is
    also when the caller is aging the *aggregate* snapshot time rather than one
    provider's (see :func:`_workspace_provider_snapshot_at`).
    """
    if provider_name is not None:
        for provider in backend_resolver.list_providers():
            if str(provider.provider_name) == provider_name:
                return float(provider.config.discovery_poll_interval_seconds)
    return DISCOVERY_STREAM_POLL_INTERVAL_SECONDS


def _workspace_provider_snapshot_at(
    backend_resolver: MngrCliBackendResolver, provider_name: str | None
) -> datetime | None:
    """Last per-provider snapshot time for ``provider_name``, or the aggregate fallback.

    A recovery verdict's trustworthiness turns on whether discovery has
    re-observed *this workspace's* host since the outage began. Because each
    provider is discovered on its own decoupled loop, a healthy provider keeps
    emitting fresh snapshots even while an unrelated provider is down -- so this
    uses the workspace's own provider's snapshot time, not a single global one.
    When the agent's provider is known, its snapshot time is returned even if
    ``None`` (no snapshot of that provider has completed yet, so freshness cannot
    be established and the caller treats it as stale). Only when the agent's
    provider is *unknown* (it has not appeared in discovery at all) do we fall
    back to the aggregate snapshot time across all providers.
    """
    if provider_name is not None:
        return backend_resolver.get_last_snapshot_at_for_provider(ProviderInstanceName(provider_name))
    _, aggregate_snapshot_at = backend_resolver.get_freshness_timestamps()
    return aggregate_snapshot_at


def is_recovery_classification_trustworthy(
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    agent_id: AgentId,
) -> bool:
    """Whether what the resolver reports is fresh enough to base a recovery verdict on.

    A negative recovery verdict leans on the host state and the provider error
    the passive discovery resolver reports. Both are properties of a single
    snapshot, so both are only trustworthy once a snapshot taken at/after the
    outage onset (``get_outage_started_wall_at``) has landed: a snapshot that
    predates the outage still carries the pre-outage host state (a just-stopped
    container still reads RUNNING) and the previous episode's provider error,
    either of which would misclassify the tier. Until then the verdict path
    treats the classification as untrustworthy and surfaces INDETERMINATE.
    Nothing destructive rides on this: the unattended start is dispatched on the
    tracker's stuck edge with no verdict at all, so this gate protects the
    verdict's copy, not an action. It is the *outage* onset rather than the
    current probe-failure run's, because that unattended start clears the run
    within a second of the machine wedging -- and a restart attempt does not make
    evidence from before the outage current.

    When no onset is recorded (only the force-``mark_stuck`` path, used in tests,
    lacks one) fall back to the absolute-age freshness gate. Only the
    passive-discovery resolver tracks snapshot freshness; for any other resolver
    (e.g. static test resolvers) the classification is treated as trustworthy so
    the verdict path is never gated. Freshness is scoped to the workspace's own
    provider (see ``_workspace_provider_snapshot_at``), and aged against that
    provider's own cadence.
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return True
    info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = info.provider_name if info is not None else None
    last_snapshot_at = _workspace_provider_snapshot_at(backend_resolver, provider_name)
    onset = tracker.get_outage_started_wall_at(agent_id) if tracker is not None else None
    if onset is None:
        return _is_discovery_fresh(
            last_snapshot_at, _workspace_provider_poll_interval_seconds(backend_resolver, provider_name)
        )
    return last_snapshot_at is not None and last_snapshot_at >= onset


def _in_band_provider_outage_reason(exc: MngrCommandError, provider_name: str | None) -> str | None:
    """``provider_name``'s own reason for a command ``mngr`` rejected at the provider, or None.

    A provider that reports itself unavailable while it is being *queried* fails
    the command at the provider -- before it ever reaches the host -- and
    ``mngr`` prints that ``ProviderUnavailableError`` on stderr, which is what
    there is to recover here. Only *this machine's* provider answers: the command
    aborts on whichever provider it queried is unavailable, which need not be the
    one being asked about.

    An unknown provider therefore answers nothing rather than taking whichever
    the message names. That is the mngr parser's ``None`` mode, and it is for a
    caller with genuinely no provider to compare against -- a caller that merely
    failed to resolve one has no grounds to adopt some other backend's outage as
    this machine's.

    A provider that instead fails while it is being *constructed* is dropped from
    the command's provider list before the command runs, and so is never queried
    at all. That is the docker backend's dead-daemon path (its state-container
    check runs at construction). It reaches stderr all the same: agent lookup
    keeps those construction skips, and reports an identifier it could not match
    while one of them is unreachable as that provider's outage rather than as a
    missing agent -- which is the same ``ProviderUnavailableError`` shape, and so
    the same reason, as the queried case.

    An outage that reaches stderr in neither shape answers nothing here, and is
    named only once the discovery poll surfaces it.
    """
    if provider_name is None:
        return None
    return parse_provider_unavailable_reason(str(exc), provider_name)


def _report_restart_step_failure(
    step_label: str,
    exc: MngrCommandError,
    *,
    workspace_agent_id: AgentId,
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    connectivity_detector: ConnectivityDetector | None,
) -> None:
    """End the restart on a failed step, naming the backend when that is the real cause.

    Reporting a stop/start that ``mngr`` rejected at the provider as a failed
    step frames a backend outage as a problem with the machine, so the
    provider's own reason is reported instead when there is one (see
    :func:`_in_band_provider_outage_reason` for when there is). The provider is
    resolved here so the lookup rides the failure path only.

    That reason is also *recorded* on the tracker, because this is the first
    observation of the outage anywhere: the rejection is a live one, while the
    discovery snapshot that carries the same outage is up to a provider poll
    interval behind it. Recorded before the RESTART_FAILED transition below, so
    the surfaces that re-derive on that edge already see it.

    A step that failed because this *device* is offline (or on a network that
    blocks SSH) reports at warning rather than error. The state it sets is
    unchanged -- RESTART_FAILED is truthful, and the user can retry it -- but
    error level is what reaches error reporting, and there is nothing there to
    report: the command was always going to fail, nobody can act on it, and the
    report's own log upload would be making the same doomed network call. The
    reading is read here rather than passed in, so it is the latest the detector
    has rather than whatever was known when the restart began -- but it is the
    *cached* reading, not a fresh probe, so a network that dropped since the
    last probe still reads clear and the failure is reported as any other.
    """
    display_info = backend_resolver.get_agent_display_info(workspace_agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    message = f"{step_label} step of host restart failed: {exc}"
    reason = None
    if provider_name is not None:
        reason = _in_band_provider_outage_reason(exc, provider_name)
        if reason is not None:
            tracker.record_backend_outage(workspace_agent_id, provider_name, reason)
            message = f"This machine's backend is unreachable, so the restart could not run: {reason}"
    device_block = read_environment_block(connectivity_detector, backend_resolver, workspace_agent_id)
    is_blocked_by_device = device_block is not EnvironmentBlock.NONE
    if is_blocked_by_device:
        verdict = f"this device is {device_block.value}"
    elif reason is not None:
        verdict = "reported as a backend outage"
    else:
        verdict = "reported as a failed step"
    # Which verdict the user was shown, alongside what the command actually
    # printed. The two now differ -- that difference is the whole verdict -- and
    # the raw output is still what a failure nobody anticipated is diagnosed from.
    # The subprocess's captured output tail is appended to this same (single)
    # log record -- reaching minds.log, and error reporting when the record is
    # an error -- rather than carried in the user-facing message above.
    logger.log(
        "WARNING" if is_blocked_by_device else "ERROR",
        "{} step of host restart for {} failed ({}): {}{}",
        step_label,
        workspace_agent_id,
        verdict,
        exc,
        "" if exc.output_tail is None else f"\nsubprocess output:\n{exc.output_tail}",
    )
    tracker.mark_restart_failed(workspace_agent_id, message)
    registry.fail(workspace_agent_id, message)


def read_environment_block(
    connectivity_detector: ConnectivityDetector | None,
    backend_resolver: BackendResolverInterface,
    workspace_agent_id: AgentId,
) -> EnvironmentBlock:
    """The device-level condition that explains this workspace being unreachable, if any.

    Both halves are required: a confirmed-bad reading, and a workspace that
    actually needs the network to be reached. A docker container on this laptop
    is reachable with the wifi off, so a dead network explains nothing about it
    and must not soften how its failures are reported.

    Reads the cached reading rather than probing. No caller is in a position to
    block for seconds -- they are reporting a failure, or answering a UI poll,
    or have just probed on the gate -- and an ``UNKNOWN`` cache answers NONE,
    which is the no-op.
    """
    if connectivity_detector is None:
        return EnvironmentBlock.NONE
    if not is_network_dependent_workspace(backend_resolver, workspace_agent_id):
        return EnvironmentBlock.NONE
    return connectivity_detector.get_reading().environment_block


def _build_restart_agent_address(agent_id: AgentId, workspace_display_info: AgentDisplayInfo | None) -> str:
    """Render ``agent_id`` as ``AGENT@HOST.PROVIDER`` when discovery can supply both components.

    A provider-qualified address is what restricts ``mngr``'s discovery to the
    one provider that can host this agent (see ``find_all_agents``, which queries
    every configured provider unless every address pins one). Unpinned, a restart
    pays for every provider the user has configured, and a provider that is
    merely unreachable -- a stopped Docker daemon, an account this device cannot
    currently reach -- is enough to fail it: when the agent goes unmatched,
    ``mngr`` reports the first unavailable provider as the reason, whether or not
    that provider could ever have hosted the agent.

    ``workspace_display_info`` describes the *workspace* agent rather than the
    system-services agent this address names. The two share a host by
    construction (they run in the same container), and the workspace agent is the
    one whose display info survives a lifecycle transition, so it is the more
    reliable source of the same coordinate.

    Falls back to the bare id when discovery supplies no ``host-`` coordinate or
    no provider name. Unpinned is what shipped, so the fallback costs the
    scoping and nothing else -- a restart must not fail for want of a qualifier.
    """
    if workspace_display_info is None or workspace_display_info.provider_name is None:
        return str(agent_id)
    host_id = str(workspace_display_info.host_id)
    # The resolver's placeholder host id ("localhost") is not a routable
    # coordinate, and only the real host-<hex> shape parses as a HostId.
    if not host_id.startswith("host-"):
        return str(agent_id)
    return f"{agent_id}@{host_id}.{workspace_display_info.provider_name}"


def _build_mngr_stop_argv(mngr_binary: str, agent_address: str) -> list[str]:
    """Build the argv for ``mngr stop`` on ``agent_address``, stopping its host with it.

    ``-v`` (DEBUG console logging to stderr) instead of ``--quiet``: the output
    goes to the capture pipe, not a terminal, and it is the per-step timeline
    that makes a killed-on-timeout command diagnosable (see ``output_tail`` on
    ``MngrCommandError``). ``--quiet`` here is what left every production start
    timeout a black box. The timeline stays on that tail and off the error
    message, which ``mngr_failure_verdict`` narrows to mngr's verdict alone.
    """
    return [mngr_binary, "stop", agent_address, "-v", "--stop-host"]


def _build_mngr_start_argv(mngr_binary: str, agent_address: str) -> list[str]:
    """Build the argv for ``mngr start`` on ``agent_address`` (also starts the host if it is stopped).

    ``-v`` instead of ``--quiet`` for the same reason as ``_build_mngr_stop_argv``.

    Structured output because the restart needs one thing the human output does
    not carry: whether a host was actually booted. The two flags act on separate
    streams and do not fight: ``--format json`` writes the one result line to
    stdout and suppresses the human lines entirely, while all of mngr's logging
    -- everything ``-v`` widens -- goes to stderr.
    """
    return [mngr_binary, "start", agent_address, "-v", "--format", "json"]


def _did_start_boot_a_host(stdout: str) -> bool | None:
    """Read ``was_host_started`` out of ``mngr start --format json``'s result line, or None.

    ``--format json`` writes exactly one result object to stdout (its logging
    goes to stderr), so the last non-empty line is the whole contract.

    None means the output did not answer, which is not the same as answering
    "nothing booted": an ``mngr`` on PATH too old to report the field leaves the
    question open, and a caller must keep its restart framing rather than
    reading silence as a no-op. Because that silently disables the no-op
    detection for every restart, it is logged rather than passed over.
    """
    last_line = next((line for line in reversed(stdout.splitlines()) if line.strip()), "")
    try:
        parsed = json.loads(last_line)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Could not read `mngr start`'s result line, so a start that booted nothing cannot be told "
            "from one that did ({}): {!r}",
            exc,
            last_line[:200],
        )
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("was_host_started"), bool):
        logger.warning(
            "`mngr start` reported no was_host_started, so a start that booted nothing cannot be told "
            "from one that did: {!r}",
            last_line[:200],
        )
        return None
    return bool(parsed["was_host_started"])


def _run_mngr(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run an ``mngr`` subprocess to completion and return its stdout on a clean exit.

    Raises ``MngrCommandError`` for every non-clean outcome (a timeout surfaces as
    the ``MngrCommandTimeoutError`` subclass, a nonzero exit and a launch failure
    as a bare ``MngrCommandError``), so callers catch a single domain error.
    """
    stdout, returncode, stderr = _run_mngr_capturing(concurrency_group, argv, env, timeout_seconds=timeout_seconds)
    if returncode != 0:
        # Only mngr's verdict rides the message (see ``mngr_failure_verdict``);
        # the timeline it printed getting there rides the tail, exactly as the
        # timeout path below does.
        raise MngrCommandError(
            f"exited {returncode}: {mngr_failure_verdict(stderr)}",
            output_tail=format_output_tail(stdout, stderr),
        )
    return stdout


def _run_mngr_capturing(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, int, str]:
    """Run an ``mngr`` subprocess, returning ``(stdout, returncode, stderr)`` without raising on nonzero exit.

    A nonzero exit is reported through the returned ``returncode`` rather than
    raised, so stdout is preserved for the caller to inspect. A failure to launch
    the process raises ``MngrCommandError``; a timeout raises the more specific
    ``MngrCommandTimeoutError``.
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=timeout_seconds,
            is_checked_after=False,
            env=env,
        )
    except (OSError, ConcurrencyGroupError) as exc:
        # The command never ran (a fork/exec failure, or a concurrency-group
        # setup/strand/shutdown failure). Callers handle failure locally, so we
        # wrap it as the single MngrCommandError they already catch.
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        # A killed subprocess never printed a verdict, so its captured output is
        # the only record of which step it died in; carry it on the error
        # (bounded, out of the message) instead of discarding it.
        raise MngrCommandTimeoutError(
            f"timed out after {int(timeout_seconds)}s",
            output_tail=format_output_tail(finished.stdout, finished.stderr),
        )
    # A finished, non-timed-out process always carries a returncode; the Optional
    # is for the not-yet-finished case, which this branch has ruled out.
    returncode = finished.returncode if finished.returncode is not None else 1
    return finished.stdout, returncode, finished.stderr


class RestartReadinessOutcome(UpperCaseStrEnum):
    """How the post-restart wait for the system interface ended."""

    # The interface answered 200: the restart converged.
    READY = auto()
    # The whole cold-boot budget elapsed with no answer: the restart did not converge.
    TIMED_OUT = auto()
    # The process is shutting down, so the wait was cut short. This says nothing
    # about whether the workspace recovered -- it is not a verdict either way.
    ABANDONED = auto()


def _await_system_interface_ready(
    workspace_host_id: str,
    mngr_forward_port: int,
    preauth_cookie: str,
    wait_seconds: float,
    concurrency_group: ConcurrencyGroup,
) -> RestartReadinessOutcome:
    """Poll the system interface through the plugin until it answers 200, the budget elapses, or shutdown.

    The wait spans a full cold boot, which is far longer than the group's ~10s
    exit budget, so it sleeps on the group's shutdown event rather than a bare
    timer: a quit during a restart must not leave this thread parked in an
    uninterruptible sleep that outlives the join and fails the group's exit.
    Shutdown is reported as its own outcome (never as a timeout), because a
    truncated wait is not evidence that the restart failed.
    """
    deadline = time.monotonic() + wait_seconds
    with make_workspace_probe_client(
        preauth_cookie=preauth_cookie,
        probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
    ) as probe_client:
        while time.monotonic() < deadline:
            if concurrency_group.is_shutting_down():
                return RestartReadinessOutcome.ABANDONED
            status = probe_workspace_through_plugin(
                mngr_forward_port=mngr_forward_port,
                preauth_cookie=preauth_cookie,
                workspace_host_id=workspace_host_id,
                probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                client=probe_client,
            )
            if status == 200:
                return RestartReadinessOutcome.READY
            if concurrency_group.shutdown_event.wait(timeout=_RESTART_PROBE_INTERVAL_SECONDS):
                return RestartReadinessOutcome.ABANDONED
    return RestartReadinessOutcome.TIMED_OUT


class RestartWorkerFailureHandler(MutableModel):
    """Callable ``on_failure`` hook for the restart worker thread.

    The recovery page only leaves its "Restarting..." state on a HEALTHY or
    RESTART_FAILED transition, and the tracker is already RESTARTING when the
    worker starts. If the worker thread crashes unexpectedly, the
    ``ConcurrencyGroup`` invokes this so the tracker still reaches RESTART_FAILED
    (and the v1 operation registry reaches FAILED) instead of the page / poller
    hanging. The crash itself is logged by the ``ObservableThread`` machinery, so
    this only records the recovery state.
    """

    tracker: SystemInterfaceHealthTracker = Field(frozen=True, description="Health tracker to transition.")
    workspace_agent_id: AgentId = Field(frozen=True, description="Workspace agent whose restart worker crashed.")
    registry: WorkspaceOperationRegistryInterface = Field(
        frozen=True, description="In-memory operation registry to mark FAILED."
    )

    def __call__(self, exc: BaseException) -> None:
        message = f"The restart worker failed unexpectedly: {exc}"
        self.tracker.mark_restart_failed(self.workspace_agent_id, message)
        self.registry.fail(self.workspace_agent_id, message)


class RestartDispatchOutcome(UpperCaseStrEnum):
    """What a call to :func:`dispatch_host_restart` did."""

    DISPATCHED = auto()
    ALREADY_RUNNING = auto()
    OPERATION_CONFLICT = auto()
    SPAWN_FAILED = auto()


def dispatch_host_restart(
    workspace_agent_id: AgentId,
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    mngr_host_dir: Path,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str | None,
    skip_stop: bool,
    connectivity_detector: ConnectivityDetector | None = None,
) -> RestartDispatchOutcome:
    """Claim the restart for ``workspace_agent_id`` and spawn its worker.

    The single dispatch path -- the ``/api/v1`` restart route and the unattended
    STUCK dispatch both come through here -- so there is exactly one place that
    claims RESTARTING, opens the operation record, and spawns the worker.

    ``registry.start_if_idle`` is the one claim, and winning it is what makes
    this caller the restart's owner. Workspace operations are serialized, so the
    workspace's single operation slot decides: a caller that loses it to another
    restart gets ``ALREADY_RUNNING`` and must not spawn a second worker racing
    the first's stop/start commands, and one that loses it to a backup update /
    configure / restore gets ``OPERATION_CONFLICT``. A spawn failure leaves the
    tracker in RESTART_FAILED and the operation FAILED, so nothing polls forever.

    One atomic claim rather than a read followed by an unconditional
    ``registry.start``, which would *replace* whatever record won the race --
    stranding that operation's poller and letting its terminal complete/fail
    land on the restart's record. The unattended dispatch is what makes the race
    real: it runs on the probe thread rather than a request thread, and a
    restore stops the workspace's services for minutes, which is exactly what
    drives the agent STUCK in the first place.

    The tracker is marked only after the slot is won, so RESTARTING is never
    claimed for a restart that turns out not to be this caller's to run.
    """
    if not registry.start_if_idle(
        workspace_agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc), None
    ):
        # Which operation blocked us decides what the caller is told. Re-read
        # rather than trusting a prior read: only the claim itself is ordered
        # against the other dispatches, and the record may already have moved on.
        blocking_operation = registry.get(workspace_agent_id)
        if blocking_operation is not None and blocking_operation.kind == WorkspaceOperationKind.RESTART:
            return RestartDispatchOutcome.ALREADY_RUNNING
        return RestartDispatchOutcome.OPERATION_CONFLICT

    tracker.mark_restarting(workspace_agent_id, start_only=skip_stop)

    # is_checked=False + on_failure: a crash of the one-shot worker transitions
    # the tracker to RESTART_FAILED and the registry to FAILED (so neither the
    # recovery surface nor the operation poller hangs). The spawn itself can
    # also raise when the group is shutting down; since RESTARTING is already
    # claimed, roll both into the failed state.
    try:
        concurrency_group.start_new_thread(
            target=run_restart_sequence,
            kwargs={
                "workspace_agent_id": workspace_agent_id,
                "tracker": tracker,
                "backend_resolver": backend_resolver,
                "mngr_binary": mngr_binary,
                "mngr_host_dir": mngr_host_dir,
                "concurrency_group": concurrency_group,
                "mngr_forward_port": mngr_forward_port,
                "mngr_forward_preauth_cookie": mngr_forward_preauth_cookie,
                "registry": registry,
                "skip_stop": skip_stop,
                "connectivity_detector": connectivity_detector,
            },
            name=f"workspace-restart-{workspace_agent_id}",
            daemon=True,
            is_checked=False,
            on_failure=RestartWorkerFailureHandler(
                tracker=tracker, workspace_agent_id=workspace_agent_id, registry=registry
            ),
        )
    # Both concurrency families are named on purpose: an exited group raises
    # InvalidConcurrencyGroupStateError (a ConcurrencyGroupError), but a group
    # that is shutting down -- or that already has a failed checked strand --
    # raises ConcurrencyExceptionGroup, which descends from ExceptionGroup
    # instead. An escape here would kill the health-probe thread this can be
    # called on, whose own dispatch catches only OSError/RuntimeError/ValueError.
    except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
        # Error level so the failure reaches Sentry: the recovery surface is
        # quiet, so a restart that never even spawned must report itself.
        logger.opt(exception=exc).error("Failed to spawn restart worker for {}: {}", workspace_agent_id, exc)
        message = f"Could not start the restart worker: {exc}"
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return RestartDispatchOutcome.SPAWN_FAILED
    return RestartDispatchOutcome.DISPATCHED


class UnattendedRecoveryDispatcher(MutableModel):
    """Stuck-edge callback that starts a machine back up when it wedges.

    A machine that stops answering gets one idempotent ``mngr start`` without
    the user asking, and the band reports it. Running in the backend rather than
    a renderer means it also covers a machine no window is displaying.

    Wired to ``add_on_stuck_edge_callback`` rather than the general on-change
    firehose: it must run once per outage episode, and the edge is the
    once-per-episode event -- the state is re-reported on every failing lap.

    ``start_only`` throughout: a pure ``mngr start`` checks ground truth at
    commit time and no-ops against a host that is already running, so an
    unattended dispatch can never bounce a live container out from under
    someone. Bouncing a running-but-wedged machine stays a decision the user
    makes, on the recovery card.

    A machine on the far side of a network this device cannot use is the one
    case where the dispatch is withheld rather than run. Every remote machine
    goes STUCK together when the wifi drops, and every ``mngr start`` aimed at
    them fails at DNS -- turning one local condition into a burst of
    RESTART_FAILED cards, each with an error report of its own. So a
    network-dependent workspace's dispatch first asks the device whether it can
    reach anything, and on a confirmed no it remembers the machine as owed (the
    card and band read the same condition off the detector's published state).
    The owed dispatches run when connectivity comes back, for whichever
    machines are still stuck by then.

    The gate deliberately runs off the stuck-edge thread. That edge is fired
    from the health probe loop, which every other workspace's probing is queued
    behind, and the connectivity probe costs seconds; the wait happens on a
    one-shot worker instead. Nothing is lost by the delay -- the machine is
    already stuck, and ``mngr start`` is idempotent.
    """

    tracker: SystemInterfaceHealthTracker = Field(frozen=True, description="Tracker whose edges drive this.")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Resolves the host to restart.")
    registry: WorkspaceOperationRegistryInterface = Field(frozen=True, description="Operation record for the restart.")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for the restart worker.")
    mngr_binary: str = Field(frozen=True, description="mngr executable the restart shells out to.")
    mngr_host_dir: Path = Field(frozen=True, description="MNGR_HOST_DIR for the restart's mngr calls.")
    mngr_forward_port: int = Field(frozen=True, description="Forward port used to probe for recovery.")
    mngr_forward_preauth_cookie: str | None = Field(frozen=True, description="Preauth cookie for that probe.")
    connectivity_detector: ConnectivityDetector | None = Field(
        default=None,
        frozen=True,
        description=(
            "Answers whether this device can reach anything, so a dispatch that would fail for "
            "local reasons is withheld and owed instead. None dispatches unconditionally, which "
            "is the behaviour without any environment signals at all."
        ),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Machines whose unattended start was withheld because this device could not
    # reach anything, held until it can. Per-process and deliberately small: it
    # is a list of restarts the app owes right now, not a history, and a quit
    # takes it with it (a machine still down at the next launch is picked up by
    # session restore, which starts it as a matter of course).
    _owed_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _owed_agent_ids: set[str] = PrivateAttr(default_factory=set)

    def __call__(self, agent_id: AgentId) -> None:
        # A machine the user stopped is unreachable on purpose. Starting it
        # again here would undo an action they just took, with no window open
        # to explain why it came back.
        if self.tracker.is_unattended_recovery_suppressed(agent_id):
            logger.info("Not auto-starting {}: it was stopped from inside the app", agent_id)
            return
        detector = self.connectivity_detector
        if detector is None or not is_network_dependent_workspace(self.backend_resolver, agent_id):
            self._dispatch(agent_id)
            return
        # The reading this needs takes seconds to establish, and this call is on
        # the probe loop's thread. Hand the whole decision to a worker, along
        # with the detector it is to consult -- which is what keeps "the gate
        # ran with nothing to ask" out of the worker's reach entirely.
        try:
            self.concurrency_group.start_new_thread(
                target=self._dispatch_once_connectivity_is_known,
                args=(agent_id, detector),
                name=f"unattended-recovery-gate-{agent_id}",
                daemon=True,
                is_checked=False,
            )
        # Same two families ``dispatch_host_restart`` names: an exited group
        # raises ConcurrencyGroupError, one that is shutting down (or already
        # has a failed checked strand) raises ConcurrencyExceptionGroup. An
        # escape here would kill the probe thread this runs on.
        except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
            # Nothing to recover into: the group can no longer run work, which
            # means the app is on its way down.
            logger.warning("Could not start the connectivity gate for {}: {}", agent_id, exc)

    def _dispatch_once_connectivity_is_known(self, agent_id: AgentId, detector: ConnectivityDetector) -> None:
        """Worker body: probe the device, then either dispatch or record the start as owed.

        The detector is handed in rather than read off the field, so the reading
        is always a real one: a caller with none dispatched inline and never
        spawned this.

        The reading costs seconds, and the machine can move out from under them,
        so both conditions are read here rather than trusted from before the
        probe -- the same two, in the same order, that
        :meth:`on_connectivity_recovered` re-reads before it dispatches an owed
        start. A stop or a destroy marks the machine *before* its own command
        runs, precisely so this dispatch cannot undo it, and a destroy takes no
        operation slot for the dispatch to lose; a machine a restart has already
        claimed is being handled by whatever claimed it.
        """
        try:
            block = detector.probe_now(max_reuse_age_seconds=_GATE_READING_REUSE_SECONDS).environment_block
        # The group refusing the SSH round is the spawn failure one frame later,
        # and gets the same answer for the same reason: it only refuses once the
        # app is going down, so there is nothing left to dispatch onto -- and a
        # dispatch that then loses its own spawn would report a RESTART_FAILED,
        # at error level, on the way out.
        except (ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
            logger.warning("Dropping the gated start of {}: the group can no longer probe: {}", agent_id, exc)
            return
        # Everything else the probe reaches is the walk behind its endpoints, on
        # an app that is otherwise fine. A reading that could not be taken is no
        # evidence, and no evidence suppresses nothing here -- the same answer a
        # wake-disqualified probe hands back. Withholding instead would strand
        # the machine: the owed set is drained by a connectivity recovery, and a
        # probe that never landed can produce no such edge.
        except (MindError, MngrError, OSError, RuntimeError, ValueError) as exc:
            logger.opt(exception=exc).warning(
                "The connectivity gate's probe for {} failed; dispatching as though the device were fine: {}",
                agent_id,
                exc,
            )
            block = EnvironmentBlock.NONE
        if self.tracker.get_health(agent_id) is not AgentHealth.STUCK:
            logger.info("Dropping the gated start of {}: it is no longer stuck", agent_id)
            return
        if self.tracker.is_unattended_recovery_suppressed(agent_id):
            logger.info("Dropping the gated start of {}: it was stopped from inside the app", agent_id)
            return
        if block is EnvironmentBlock.NONE:
            self._dispatch(agent_id)
            return
        with self._owed_lock:
            self._owed_agent_ids.add(str(agent_id))
        logger.info(
            "Withholding the unattended start of {}: this device is {}. It is owed until connectivity returns",
            agent_id,
            block.value,
        )
        # A recovery that landed while the lines above ran drained an owed set
        # this machine had not joined yet, and no second one is coming: the
        # detector fires on the bad -> good edge alone and stops probing at it,
        # so the machine would sit STUCK behind a device condition that no
        # longer holds, with nothing to clear it. Draining again here is the
        # remedy. The question is whether an edge is still to come rather than
        # whether the reading currently reads clear, because a wake blanks the
        # reading to UNKNOWN while leaving the watch on -- and a machine whose
        # gate happened to be interrupted by one must stay owed rather than be
        # released onto a network nothing has looked at. Reaching here at all
        # means this gate's own probe stored a bad reading (a disqualified one
        # answers UNKNOWN, whose block is NONE, and takes the dispatch path), so
        # the watch is off only if a recovery took it off.
        if not detector.is_watching_for_recovery():
            self.on_connectivity_recovered()

    def on_connectivity_recovered(self) -> None:
        """Run the starts withheld while this device could not reach anything.

        Registered as the detector's recovery callback. Only machines that are
        *still* stuck are started: one that answered again in the meantime (the
        outage was the network the whole time, so most of them will have) needs
        nothing, and one that has moved on to RESTARTING or RESTART_FAILED is
        already being handled by whatever moved it.
        """
        with self._owed_lock:
            owed_agent_ids = sorted(self._owed_agent_ids)
            self._owed_agent_ids.clear()
        for aid_str in owed_agent_ids:
            # Per machine, because the set was cleared before any of it ran and
            # the detector has already stopped watching: one release raising
            # would otherwise leave every machine after it stuck behind a
            # waiting-for-network card, on a working network, with nothing left
            # to come back for them. The two families are the ones a restart's
            # registry and tracker calls raise -- the same pair the detector's
            # own callback fence names.
            try:
                self._release_owed_start(AgentId(aid_str))
            except (MindError, MngrError) as exc:
                logger.opt(exception=exc).warning("The owed start of {} could not be released: {}", aid_str, exc)

    def _release_owed_start(self, agent_id: AgentId) -> None:
        """Start one owed machine, if it still needs and may take a start."""
        if self.tracker.get_health(agent_id) is not AgentHealth.STUCK:
            logger.info("Dropping the owed start of {}: it is no longer stuck", agent_id)
            return
        if self.tracker.is_unattended_recovery_suppressed(agent_id):
            logger.info("Dropping the owed start of {}: it was stopped from inside the app", agent_id)
            return
        self._dispatch(agent_id)

    def _dispatch(self, agent_id: AgentId) -> None:
        """Run the start-only restart and report which of the dispatch outcomes it hit."""
        outcome = dispatch_host_restart(
            workspace_agent_id=agent_id,
            tracker=self.tracker,
            backend_resolver=self.backend_resolver,
            registry=self.registry,
            concurrency_group=self.concurrency_group,
            mngr_binary=self.mngr_binary,
            mngr_host_dir=self.mngr_host_dir,
            mngr_forward_port=self.mngr_forward_port,
            mngr_forward_preauth_cookie=self.mngr_forward_preauth_cookie,
            skip_stop=True,
            connectivity_detector=self.connectivity_detector,
        )
        logger.info("Unattended recovery for {}: {}", agent_id, outcome.value)


def run_restart_sequence(
    workspace_agent_id: AgentId,
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str | None,
    registry: WorkspaceOperationRegistryInterface,
    skip_stop: bool = False,
    startup_wait_seconds: float = _HOST_RESTART_STARTUP_WAIT_SECONDS,
    connectivity_detector: ConnectivityDetector | None = None,
) -> None:
    """Background worker: stop + start the workspace's host, then await recovery.

    Drives the health tracker to HEALTHY on recovery or RESTART_FAILED (with a
    reason) when a step errors or the system interface does not return within
    ``startup_wait_seconds`` (sized for a container cold boot). In lockstep it appends
    progress lines to, and completes / fails, the v1 ``registry`` operation so the
    ``/workspaces/operations/restart/<id>`` resource can report the same restart. A crash
    of this worker is turned into RESTART_FAILED by ``RestartWorkerFailureHandler``,
    wired as the thread's ``on_failure`` callback.

    Every RESTART_FAILED transition also logs at error level: the recovery
    surface is quiet (Principle 3), so a failed restart must reach error
    reporting even though the card renders it for the user. The exception is the
    two endings that route over the network -- a step that errored, and a
    readiness wait that timed out -- while this device is confirmed offline or on
    a network that blocks SSH: those log at warning instead, since the state is
    still RESTART_FAILED but the failure was doomed by something no error report
    can act on (see :func:`_report_restart_step_failure`). The endings that
    describe discovery losing a coordinate keep their error level, network or
    no: nothing about the device explains them. That is also why the
    readiness wait is given a full cold-boot budget: below it, a restart that was
    merely slow reports itself as a failure, to the user and to error reporting
    alike. A shutdown that truncates the wait is the one ending that yields no
    verdict at all -- it observed nothing, so it claims nothing.

    ``skip_stop`` is set by the dispatches that fire with no knowledge of the
    host's state (the API's ``start_only``): the unattended one on the tracker's
    STUCK edge, and the stopped-machine click-through. The sequence must then
    never bounce a live container, and ``mngr start`` alone guarantees that --
    it checks ground truth at commit time, no-ops on a running host, and
    cold-boots a stopped one. The "Restart machine" click on the recovery card
    keeps the stop step, since it may target a running-but-wedged container
    that only a bounce fixes.
    """
    registry.append_log(workspace_agent_id, "Starting host restart.")
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        message = "Could not locate the system-services agent for this machine."
        logger.error("Host restart of {} failed: {}", workspace_agent_id, message)
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return

    # Read before the stop step, so both commands address the same machine. The
    # post-restart read further down is a separate question (where the machine
    # ended up) and deliberately takes its own, later snapshot.
    services_agent_address = _build_restart_agent_address(
        services_agent_id, backend_resolver.get_agent_display_info(workspace_agent_id)
    )

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)

    if skip_stop:
        logger.info("Start-only restart for {}: skipping the stop step", workspace_agent_id)
        registry.append_log(workspace_agent_id, "Start-only restart; skipping the stop step.")
    else:
        registry.append_log(workspace_agent_id, "Stopping the system-services agent.")
        try:
            _run_mngr(
                concurrency_group,
                _build_mngr_stop_argv(mngr_binary, services_agent_address),
                env,
                timeout_seconds=HOST_STOP_TIMEOUT_SECONDS,
            )
        except MngrCommandError as exc:
            # ``mngr stop --stop-host`` raises HostShutdownNotSupportedError when a provider's
            # ``supports_shutdown_hosts`` is False (e.g. Modal). minds runs mngr as a subprocess,
            # so it can only match the error's message text in stderr -- keyed off mngr's exported
            # HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE constant (one shared source of truth) rather than
            # a duplicated literal.
            if HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE in str(exc):
                # Provider can't stop a host in place (e.g. Modal). Expected, not a
                # failure: the start step below restarts it on its own (reconnect-if-alive,
                # else recreate-from-snapshot), so skip the stop and proceed.
                logger.info(
                    "Stop step of host restart for {} skipped: provider does not support host shutdown; "
                    "restart proceeds via start alone",
                    workspace_agent_id,
                )
                registry.append_log(
                    workspace_agent_id, "Provider does not support stopping the host; skipping stop step."
                )
            else:
                _report_restart_step_failure(
                    "Stop",
                    exc,
                    workspace_agent_id=workspace_agent_id,
                    tracker=tracker,
                    backend_resolver=backend_resolver,
                    registry=registry,
                    connectivity_detector=connectivity_detector,
                )
                return

    registry.append_log(workspace_agent_id, "Starting the system-services agent.")
    try:
        start_stdout = _run_mngr(
            concurrency_group,
            _build_mngr_start_argv(mngr_binary, services_agent_address),
            env,
            timeout_seconds=HOST_START_TIMEOUT_SECONDS,
        )
    except MngrCommandError as exc:
        _report_restart_step_failure(
            "Start",
            exc,
            workspace_agent_id=workspace_agent_id,
            tracker=tracker,
            backend_resolver=backend_resolver,
            registry=registry,
            connectivity_detector=connectivity_detector,
        )
        return

    # A start that booted no host is the whole reason this reads its output. It
    # is not a failure -- the start is idempotent by design, and a host that was
    # already running needed nothing -- and it means the machine never went down
    # and came back, so the surfaces must stop describing this episode as a
    # restart of it. (It does not mean the start did nothing: an agent whose
    # session had died is relaunched either way. Only the host is in question.)
    if _did_start_boot_a_host(start_stdout) is False:
        logger.info("Start step of host restart for {} booted nothing; the host was already up", workspace_agent_id)
        registry.append_log(workspace_agent_id, "The machine was already running; it was not restarted.")
        tracker.record_restart_started_nothing(workspace_agent_id)

    # Without a plugin route there is no way to probe for recovery, so treat a
    # clean dispatch as success (mirrors the background probe loop being a no-op).
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie:
        tracker.record_probe_success(workspace_agent_id)
        registry.append_log(workspace_agent_id, "Restart dispatched.")
        registry.complete(workspace_agent_id)
        return

    # Workspace origins are keyed by host id; resolve it from discovery. A
    # missing coordinate (discovery lost the host across the restart) means
    # the probe could never route, so fail the restart rather than spin. The
    # real host-<hex> shape is required: the resolver interface's placeholder
    # ("localhost") would probe the unroutable vhost localhost.localhost.
    display_info = backend_resolver.get_agent_display_info(workspace_agent_id)
    if display_info is None or not str(display_info.host_id).startswith("host-"):
        message = "The workspace's host coordinate is unknown after the restart, so its recovery cannot be confirmed."
        logger.error("Host restart of {} failed: {}", workspace_agent_id, message)
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return

    registry.append_log(workspace_agent_id, "Waiting for the system interface to respond.")
    outcome = _await_system_interface_ready(
        str(display_info.host_id),
        mngr_forward_port,
        mngr_forward_preauth_cookie,
        startup_wait_seconds,
        concurrency_group,
    )
    if outcome is RestartReadinessOutcome.READY:
        tracker.record_probe_success(workspace_agent_id)
        registry.append_log(workspace_agent_id, "The system interface is responding again.")
        registry.complete(workspace_agent_id)
    elif outcome is RestartReadinessOutcome.ABANDONED:
        # The app is quitting mid-restart. Nothing is left to report to: both the
        # tracker and the operation registry are per-process and die with it, and
        # no window is up to render a verdict. Reporting a failure here would be a
        # claim about the workspace we never actually observed, so this stays an
        # info line -- there is no persistent condition for it to hide.
        logger.info("Host restart of {} was cut short by shutdown before the interface answered", workspace_agent_id)
    else:
        message = f"The system interface did not respond within {int(startup_wait_seconds)}s of the host restart."
        # Warning while this device is confirmed offline or on a network that
        # blocks SSH, for the reason :func:`_report_restart_step_failure` gives
        # for the steps: every poll of the wait was routed over the same network
        # the commands were, so there is nothing here an error report could act
        # on. This is the longer of the two windows the network can die in --
        # the wait is given a full cold-boot budget -- and so the likelier
        # place a restart already in flight when the wifi dropped ends up.
        is_blocked_by_device = (
            read_environment_block(connectivity_detector, backend_resolver, workspace_agent_id)
            is not EnvironmentBlock.NONE
        )
        logger.log(
            "WARNING" if is_blocked_by_device else "ERROR",
            "Host restart of {} failed: {}",
            workspace_agent_id,
            message,
        )
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)


def _provider_error_message_for_workspace(
    provider_errors: Mapping[ProviderInstanceName, DiscoveryError],
    provider_name: str | None,
    is_classification_trustworthy: bool,
) -> str | None:
    """Map this workspace's provider error message (if any) from the discovery snapshot.

    ``get_provider_errors()`` keys per-provider discovery errors by provider
    name, so attribution to *this* workspace's provider is exact. Returns None in
    the brief pre-discovery window where the provider is unknown
    (``provider_name is None``), and None when this workspace's provider has no
    surfaced error. Otherwise returns the provider's own error message.

    The error is freshness-gated like every other verdict read off the resolver.
    It is a property of one snapshot -- ``get_provider_errors()`` holds whatever
    that provider's *last* poll reported until its next one lands -- so an error
    from a poll that predates the outage onset describes a previous episode, not
    this one, and would explain a new outage with a backend that has since
    recovered. ``is_classification_trustworthy`` is
    :func:`is_recovery_classification_trustworthy` for this workspace, so the
    provider error and the host state answer to the same rule. This costs up to
    one provider poll interval before an outage that discovery saw *before* the
    machine's own health failed can be named; the in-band reason
    (:func:`_in_band_provider_outage_reason`) is not gated, because a command
    mngr rejected at the provider is a live observation with no snapshot behind
    it.

    That message is read by a user -- the recovery card shows it under its own
    "Can't connect to <provider>" heading -- and which shape it arrives in turns
    on something the provider decided: discovery records the error's ``__cause__``
    when there is one, so a provider that raised ``ProviderUnavailableError``
    without a ``from`` clause surfaces the whole generic message instead of the
    bare reason its neighbours surface. The reason is recovered from that shape
    here so the two read alike, rather than repeating the provider's name back at
    the heading and trailing mngr's internal marker sentence.
    """
    if provider_name is None or not is_classification_trustworthy:
        return None
    for name, error in provider_errors.items():
        if str(name) == provider_name:
            return parse_provider_unavailable_reason(error.message, provider_name) or error.message
    return None


def _recorded_backend_outage_reason(
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    agent_id: AgentId,
    provider_name: str | None,
) -> str | None:
    """The backend outage a command hit, until discovery has re-polled that backend.

    A stop/start ``mngr`` rejected at the provider is recorded on the tracker
    (see :func:`_report_restart_step_failure`), and this is what reads it back.
    It is the only account of an outage available before the provider's next
    poll, which is exactly the window in which the recovery surfaces are raised
    -- minds restarts a machine the moment it wedges, so the rejection lands
    within seconds of the outage while the poll can be half a minute away.

    Its authority ends at that poll. Whatever the poll reports supersedes it:
    still down, and the resolver's own error takes over (saying the same thing);
    back up, and there is nothing left to say, so a machine that stays wedged
    for reasons of its own stops being blamed on a backend that is now
    answering. This is the same lifetime a surfaced provider error has -- until
    its provider is next polled -- reached from the other side.

    The record is not freshness-gated the way the resolver's error is: it was
    observed rather than remembered from a snapshot, and it is dropped with the
    tracker's record when the machine answers, so it can only ever describe the
    episode in progress. It answers only for the provider it was observed at,
    which is what keeps the reason and the "Can't connect to ..." heading above
    it talking about the same backend.
    """
    if tracker is None or provider_name is None:
        return None
    outage = tracker.get_backend_outage(agent_id)
    if outage is None or outage.provider_name != provider_name:
        return None
    if isinstance(backend_resolver, MngrCliBackendResolver):
        polled_at = _workspace_provider_snapshot_at(backend_resolver, provider_name)
        if polled_at is not None and polled_at > outage.observed_at:
            return None
    return outage.reason


def _passive_provider_error_message(
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    agent_id: AgentId,
    provider_name: str | None,
    is_classification_trustworthy: bool,
) -> str | None:
    """This workspace's backend outage, from the evidence already in hand, or None.

    The one place the two passive accounts of the same backend are ranked, so the
    card, the band and the probe cannot rank them differently -- which is what
    :func:`read_backend_unreachable_verdict` exists to guarantee. A backend a
    command has already been rejected at (:func:`_recorded_backend_outage_reason`)
    outranks the resolver's surfaced error
    (:func:`_provider_error_message_for_workspace`): it is the more recent
    observation of the two, and it is readable at all only while it stays that
    way.

    The probe's own exec reason is not ranked here. It is fresher than both --
    observed after this poll's snapshot -- so its single caller prefers it over
    whatever this returns.
    """
    return _recorded_backend_outage_reason(
        backend_resolver, tracker, agent_id, provider_name
    ) or _provider_error_message_for_workspace(
        backend_resolver.get_provider_errors(), provider_name, is_classification_trustworthy
    )


class BackendUnreachableVerdict(FrozenModel):
    """The machine's backend is unreachable, with the provider's own account of why."""

    provider_label: str = Field(description="Friendly provider name for the 'Can't connect to ...' headline")
    reason: str = Field(description="The provider's verbatim error, or the canned access-rejected reason")


def read_backend_unreachable_verdict(
    agent_id: AgentId,
    *,
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
) -> BackendUnreachableVerdict | None:
    """Return the backend-unreachable verdict reachable without running a command, or None.

    The recovery card polls, so it needs this verdict at a poll's cost: no
    ``mngr`` round trip is made for it. Two sources answer, and both are already
    in hand -- a surfaced provider error and an UNAUTHENTICATED host state, both
    freshness-gated because both are properties of one snapshot, plus the backend
    outage a restart already ran into, which the tracker holds.

    That last one is why this does not trail an outage by a provider poll. The
    machine minds is asked to recover is one it has just tried to restart, and a
    restart mngr rejected at the provider names the backend on the spot; only an
    outage no command has run into yet waits for discovery.

    This answers only "is the backend unreachable?". A None means "not by this
    evidence", not "the machine is fine": a wedged but reachable container looks
    identical here, and it is the probe loop, not this read, that settles it.
    """
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    host_state_enum = read_host_state(backend_resolver, display_info) if display_info is not None else None
    # One trust reading for both passive sources, so the provider error and the
    # host state cannot disagree about whether this workspace's provider has been
    # re-observed since the outage began.
    classification_is_trustworthy = is_recovery_classification_trustworthy(backend_resolver, tracker, agent_id)
    provider_error_message = _passive_provider_error_message(
        backend_resolver, tracker, agent_id, provider_name, classification_is_trustworthy
    )
    if provider_error_message is not None:
        reason = provider_error_message
    elif classification_is_trustworthy and host_state_enum is HostState.UNAUTHENTICATED:
        # Discovery carries only the host state (``DiscoveredHost`` has no
        # failure_reason), so there is no verbatim error to show here -- the
        # canned text covers the class of causes instead.
        reason = HOST_ACCESS_REJECTED_REASON
    else:
        return None
    return BackendUnreachableVerdict(
        provider_label=friendly_provider_label(provider_name) or _DEFAULT_PROVIDER_LABEL,
        reason=reason,
    )


# The classified causes that mean the failure is on this device, not the
# workspace: a tunnel this machine could not build, and the forward's own
# connection pool running out. Both are raised without the backend ever being
# dialed, so neither says anything about whether the workspace is answering --
# and both are fixed by restarting the app, never by restarting the machine.
_DEVICE_SIDE_FAILURE_REASONS: Final[frozenset[SystemInterfaceBackendFailureReason]] = frozenset(
    {
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        SystemInterfaceBackendFailureReason.POOL_EXHAUSTED,
    }
)


class DeviceCannotConnectVerdict(FrozenModel):
    """This device cannot reach the workspace, whatever the workspace is doing."""

    detail: str = Field(description="The forward's verbatim error text, empty when it quoted none")


def read_device_cannot_connect_verdict(
    agent_id: AgentId,
    *,
    tracker: SystemInterfaceHealthTracker | None,
) -> DeviceCannotConnectVerdict | None:
    """Return the this-device-cannot-connect verdict, or None when the evidence does not say so.

    Read off the cause the forward classified for this episode (see
    ``SystemInterfaceHealthTracker.record_connection_failure``). Only the two
    causes raised before the backend is ever dialed qualify; a failure that
    reached the network leaves the workspace implicated and is not this.

    Like the backend-unreachable verdict, this outranks whatever the restart
    episode concluded, because it explains it: a machine this device cannot
    reach goes STUCK and gets restarted whether or not anything is wrong with
    it, and the restart is what produces the RESTART_FAILED the surfaces would
    otherwise report. It clears the moment a probe succeeds, which drops the
    tracker's record for the episode along with it.

    Pool exhaustion and a broken tunnel are not separated for the user: both are
    fixed by restarting the app and by nothing else the user can do, so a second
    card would be a distinction without an action. The recorded cause keeps them
    apart in the log and in Sentry, where the difference is measurable.
    """
    if tracker is None:
        return None
    observation = tracker.get_connection_failure(agent_id)
    if observation is None or observation.reason not in _DEVICE_SIDE_FAILURE_REASONS:
        return None
    return DeviceCannotConnectVerdict(detail=observation.detail or "")


def read_host_state(backend_resolver: BackendResolverInterface, display_info: AgentDisplayInfo) -> HostState | None:
    """The lifecycle state of the host an agent's display info names, or None if unknown.

    Resolvers without discovery report a "localhost" placeholder for
    ``host_id`` that is not a parseable ``HostId``; they carry no host state
    anyway, so that reads as unknown rather than raising.
    """
    try:
        host_id = HostId(display_info.host_id)
    except ValueError:
        return None
    return backend_resolver.get_host_state(host_id)
