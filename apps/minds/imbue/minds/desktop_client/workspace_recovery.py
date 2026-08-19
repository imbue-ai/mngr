"""Workspace recovery: host-health probe + restart worker.

These are the engine behind the recovery flow (the recovery card's state and its
host restart action). They are extracted here -- away from :mod:`app` -- so the
versioned ``/api/v1`` surface (:mod:`api_v1`) can drive them without importing
:mod:`app` (which would form an import cycle, since ``app`` imports ``api_v1``).

``probe_workspace_health`` composes a :class:`HostHealthResponse` from the
passive discovery resolver plus a batched in-container ``mngr exec`` probe.
``run_restart_sequence`` is the background worker body (``mngr stop`` + ``mngr
start``, then await recovery) that drives both the
:class:`SystemInterfaceHealthTracker` (so the recovery surfaces re-label) and a
:class:`WorkspaceOperationRegistryInterface` (so the v1
``/workspaces/operations/restart/<id>`` resource can report restart status + logs).
"""

import os
import shlex
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
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.recovery_probe import DispatchTier
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_lifecycle import HOST_START_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_lifecycle import HOST_STOP_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.api.discovery_events import DISCOVERY_STREAM_POLL_INTERVAL_SECONDS
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.errors import parse_provider_unavailable_reason
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName

# Stand-in provider name for the "Can't connect to ..." headline, for a provider
# discovery has not named yet (or does not recognise).
_DEFAULT_PROVIDER_LABEL: Final[str] = "the machine backend"
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
# Hard timeout for the recovery host-health probe's in-container ``mngr exec``.
# Far shorter than the default ceiling: this is a *diagnostic* that gates the
# recovery UI. The exec touches the provider (``get_host`` -> the connector's
# ~30s httpx) before reaching the container, so it must carry its own 30s-class
# cap rather than inheriting the 120s default.
_HOST_HEALTH_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
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
    # Which verdict the user was shown, alongside what the command actually
    # printed. The two now differ -- that difference is the whole verdict -- and
    # the raw output is still what a failure nobody anticipated is diagnosed from.
    logger.error(
        "{} step of host restart for {} failed ({}): {}",
        step_label,
        workspace_agent_id,
        "reported as a backend outage" if reason is not None else "reported as a failed step",
        exc,
    )
    tracker.mark_restart_failed(workspace_agent_id, message)
    registry.fail(workspace_agent_id, message)


def _build_mngr_stop_argv(mngr_binary: str, agent_id: AgentId) -> list[str]:
    """Build the argv for ``mngr stop`` on ``agent_id``, stopping its host with it."""
    return [mngr_binary, "stop", str(agent_id), "--quiet", "--stop-host"]


def _build_mngr_start_argv(mngr_binary: str, agent_id: AgentId) -> list[str]:
    """Build the argv for ``mngr start`` on ``agent_id`` (also starts the host if it is stopped)."""
    return [mngr_binary, "start", str(agent_id), "--quiet"]


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
        raise MngrCommandError(f"exited {returncode}: {stderr.strip()}")
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
        raise MngrCommandTimeoutError(f"timed out after {int(timeout_seconds)}s")
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
    """

    tracker: SystemInterfaceHealthTracker = Field(frozen=True, description="Tracker whose edges drive this.")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Resolves the host to restart.")
    registry: WorkspaceOperationRegistryInterface = Field(frozen=True, description="Operation record for the restart.")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for the restart worker.")
    mngr_binary: str = Field(frozen=True, description="mngr executable the restart shells out to.")
    mngr_host_dir: Path = Field(frozen=True, description="MNGR_HOST_DIR for the restart's mngr calls.")
    mngr_forward_port: int = Field(frozen=True, description="Forward port used to probe for recovery.")
    mngr_forward_preauth_cookie: str | None = Field(frozen=True, description="Preauth cookie for that probe.")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __call__(self, agent_id: AgentId) -> None:
        # A machine the user stopped is unreachable on purpose. Starting it
        # again here would undo an action they just took, with no window open
        # to explain why it came back.
        if self.tracker.is_unattended_recovery_suppressed(agent_id):
            logger.info("Not auto-starting {}: it was stopped from inside the app", agent_id)
            return
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
    reporting even though the card renders it for the user. That is also why the
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
                _build_mngr_stop_argv(mngr_binary, services_agent_id),
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
                )
                return

    registry.append_log(workspace_agent_id, "Starting the system-services agent.")
    try:
        _run_mngr(
            concurrency_group,
            _build_mngr_start_argv(mngr_binary, services_agent_id),
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
        )
        return

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
        logger.error("Host restart of {} failed: {}", workspace_agent_id, message)
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
    ``mngr exec`` round trip is made for it. It is the same classifier
    ``probe_workspace_health`` runs, handed only the evidence already in hand --
    so the two agree on the sources of BACKEND_UNREACHABLE (a surfaced provider
    error and an UNAUTHENTICATED host state, both freshness-gated because both
    are properties of one snapshot; and the backend outage a restart already ran
    into, held by the tracker) by construction rather than by restating the
    rules here.

    That last one is why this does not trail an outage by a provider poll. The
    machine minds is asked to recover is one it has just tried to restart, and a
    restart mngr rejected at the provider names the backend on the spot; only an
    outage no command has run into yet waits for discovery. The one source out
    of reach is the exec the probe fires itself, which is bought with the round
    trip this read exists to avoid.

    The reverse does not hold either: the tiers that need the in-container probe,
    such as a wedged but reachable container, are invisible here -- with no exec
    attempted the classifier renders them INDETERMINATE. So this answers only "is
    the backend unreachable?", and a None means "not by this evidence", not "the
    machine is fine".
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
    # No exec was attempted and none is going to be, so the probe rows the
    # response also carries are unused here (they are pure string assembly over
    # values already in hand, which is what keeps this a poll-cheap read).
    response = build_host_health_response(
        host_state=host_state_enum.value if host_state_enum is not None else "",
        services_agent_id=None,
        in_container_stdout=None,
        plugin_resolver_services={},
        provider_error_message=provider_error_message,
        provider_label=friendly_provider_label(provider_name) or _DEFAULT_PROVIDER_LABEL,
        classification_is_trustworthy=classification_is_trustworthy,
    )
    if response.dispatch_tier is not DispatchTier.BACKEND_UNREACHABLE:
        return None
    return BackendUnreachableVerdict(provider_label=response.provider_label, reason=response.unreachable_reason)


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


def probe_workspace_health(
    agent_id: AgentId,
    *,
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    envelope_stream_consumer: EnvelopeStreamConsumer | None,
) -> HostHealthResponse:
    """Compose the host-health response from the passive resolver + an in-container probe.

    Provider reachability and host lifecycle are read from the
    ``backend_resolver`` -- the single passive-discovery sampler shared with the
    rest of minds -- not re-sampled with a synchronous ``mngr list``. The reason
    the inner interface isn't answering comes from the batched in-container ``mngr
    exec`` probe, which is fired when the provider is reachable and the passive
    layer cannot already answer: either the host is observed RUNNING (so the
    container should be reachable and the exec tests the inner interface), or the
    resolver's host state is not trustworthy yet (a stale pre-onset snapshot, or a
    dead/stalled discovery producer whose freshness gate can never open). When
    the passive layer cannot
    answer, the exec's own outcome is the only direct evidence available; a fresh,
    trustworthy, actionable state is left to the classifier so an outage never
    pays a doomed provider round-trip. The plugin's resolver-snapshot mirror
    supplies the last probe.

    The recovery page can be reached before discovery has re-observed the host
    after an outage (the STUCK redirect is no longer gated on freshness -- that
    gate moved here), so this checks freshness itself: ``tracker`` supplies the
    outage onset and ``is_recovery_classification_trustworthy`` decides whether a
    negative verdict off the resolver's host state can be trusted yet. When it
    cannot (a pre-outage snapshot), or the in-container probe timed out (observed
    nothing), the classifier yields INDETERMINATE rather than a verdict -- unless
    the probe returned direct evidence: a live GET / 200 is trusted regardless of
    freshness, and an exec that completed without reaching the container is
    likewise direct (fresh) evidence for the consent-gated HOST_UNRESPONSIVE.

    Both passive reads that feed the classifier -- the host state and the
    provider error -- are re-read after the exec, at the same instant the
    trustworthiness check runs, so the freshness gate always certifies the values
    that are actually classified (a snapshot landing during the slow exec would
    otherwise open the gate for a pre-snapshot reading). The exec's own in-band
    provider reason is not one of them: it is a live observation, so it is held
    apart and preferred over the resolver's.
    """
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    services_agent_id = backend_resolver.get_system_services_agent_id(agent_id)
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    # Friendly provider name for the "Can't connect to ..." page title.
    provider_label = friendly_provider_label(provider_name) or _DEFAULT_PROVIDER_LABEL

    # Read host/provider state from the passive discovery resolver.
    host_state_enum = read_host_state(backend_resolver, display_info) if display_info is not None else None
    host_state = host_state_enum.value if host_state_enum is not None else ""

    # Whether the resolver's host state can be trusted for a verdict yet (a
    # snapshot taken at/after the outage onset has landed). Computed here to gate
    # the exec; re-read after the exec for the actual classification (see below).
    state_is_trustworthy = is_recovery_classification_trustworthy(backend_resolver, tracker, agent_id)
    provider_error_message = _passive_provider_error_message(
        backend_resolver, tracker, agent_id, provider_name, state_is_trustworthy
    )

    # In-container exec probe, fired only when the provider is reachable (no
    # surfaced error that the freshness gate lets speak -- a gated-off one is
    # left to the exec to settle in-band, sooner and from a live observation --
    # and no outage a restart has already been rejected with, which would send
    # this exec through the same backend to be rejected the same way)
    # and the passive layer will not already answer with a
    # verdict: the host is observed RUNNING (verify the interface), or its state
    # is UNKNOWN (observed up but unreadable from inside, or otherwise
    # indeterminate -- the passive layer carries no verdict, so the exec is the
    # only direct evidence), or its state is not trustworthy yet (a stale
    # pre-onset snapshot, or a dead/stalled discovery producer whose freshness
    # gate can never open). A fresh, trusted state that *does* carry a verdict
    # (STOPPED/CRASHED -> offline, FAILED/UNAUTHENTICATED, or a transitional
    # STOPPING) is left to the classifier rather than execced, so an outage never
    # pays a doomed provider round-trip. The exec SSHes to the container via
    # ``get_host`` (the connector's ~30s httpx), so it carries an explicit
    # 30s-class cap; its outcome is the only direct evidence available when the
    # passive layer is silent, resolving the page to HEALTHY or a consent-gated
    # HOST_UNRESPONSIVE instead of an indefinite INDETERMINATE. A non-clean
    # outcome leaves ``in_container_stdout`` None.
    in_container_stdout: str | None = None
    probe_timed_out = False
    probe_exec_attempted = False
    in_band_provider_error: str | None = None
    if (
        services_agent_id is not None
        and provider_error_message is None
        and (host_state_enum in (HostState.RUNNING, HostState.UNKNOWN) or not state_is_trustworthy)
    ):
        probe_exec_attempted = True
        try:
            in_container_stdout = _run_mngr(
                concurrency_group,
                build_probe_argv(mngr_binary, services_agent_id),
                env,
                timeout_seconds=_HOST_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
        except MngrCommandTimeoutError as exc:
            # A timeout observed nothing -- distinct from a clean exit with no
            # sentinel (ssh dead, a real HOST_UNRESPONSIVE signal). Flag it so the
            # classifier surfaces INDETERMINATE (keep checking) rather than
            # rendering a verdict off non-evidence. Ordered before MngrCommandError
            # because the timeout error is a subclass of it.
            probe_timed_out = True
            logger.debug("in-container probe for host-health of {} timed out: {}", agent_id, exc)
        except MngrCommandError as exc:
            # The exec can fail *at the provider*, before it ever reaches the
            # container, and a provider that reports itself unavailable while
            # being queried says so in-band as a ProviderUnavailableError.
            # Trusting only the resolver's surfaced error would misclassify that
            # for up to a full provider poll interval (30s for imbue_cloud): the
            # classifier reads a completed-but-empty exec as direct evidence the
            # container is unreachable, so an outage the exec never got past
            # would be reported as a wedged machine. The in-band reason is the
            # same class of evidence, only sooner. An outage that carries no
            # such reason reads as that completed-but-empty exec until the
            # discovery poll names it. It is kept apart from the resolver's own
            # error rather than overwriting it because only the resolver's is
            # freshness-gated: this one is a live observation, so it must
            # survive a gate that is closed (and it is preferred below when both
            # are in hand, being the more recent of the two). Only this
            # workspace's own provider answers, and only when it is known: an
            # unrelated backend's outage says nothing about whether this
            # container can be reached.
            in_band_provider_error = _in_band_provider_outage_reason(exc, provider_name)
            logger.debug("in-container probe for host-health of {} did not exit cleanly: {}", agent_id, exc)
    plugin_resolver_services: dict[str, str] = (
        envelope_stream_consumer.get_resolver_snapshot_for_agent(agent_id)
        if envelope_stream_consumer is not None
        else {}
    )
    if services_agent_id is not None:
        exec_command = shlex.join(build_probe_argv(mngr_binary, services_agent_id))
    else:
        exec_command = "(mngr exec <system-services-agent>) -- no services agent id known"
    # Re-read the host state here, paired with the trustworthiness check below, so
    # the freshness gate certifies the state that is actually classified. The exec
    # above can take tens of seconds; a discovery snapshot landing mid-exec bumps
    # the per-provider snapshot time past the outage onset (making the
    # classification trustworthy) while the pre-exec read still holds the
    # pre-snapshot state -- classifying e.g. HOST_UNRESPONSIVE off a stale RUNNING
    # when the snapshot that opened the gate already reads STOPPED. The pre-exec
    # read above only decides whether to attempt the exec.
    if display_info is not None:
        host_state_enum = read_host_state(backend_resolver, display_info)
        host_state = host_state_enum.value if host_state_enum is not None else ""
    classification_is_trustworthy = is_recovery_classification_trustworthy(backend_resolver, tracker, agent_id)
    # The provider error is re-read for the same reason the host state is: a
    # snapshot landing mid-exec can open the freshness gate, and the read that is
    # classified must be the one this check certifies. The recorded outage is
    # re-read too, since such a snapshot is also what ends its authority. The
    # exec's own in-band reason wins over both when it produced one -- it was
    # observed after this poll's snapshot, so it is the latest account of the
    # same backend.
    provider_error_message = in_band_provider_error or _passive_provider_error_message(
        backend_resolver, tracker, agent_id, provider_name, classification_is_trustworthy
    )
    response = build_host_health_response(
        host_state=host_state,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services,
        mngr_exec_command=exec_command,
        mngr_binary=mngr_binary,
        provider_error_message=provider_error_message,
        provider_label=provider_label,
        probe_timed_out=probe_timed_out,
        probe_exec_attempted=probe_exec_attempted,
        classification_is_trustworthy=classification_is_trustworthy,
    )
    # One line per probe with the classifier's inputs: the tier alone (logged at
    # the route) cannot explain WHY a verdict fired -- reconstructing a
    # multi-probe sequence (e.g. unresponsive -> indeterminate -> offline at app
    # startup) needs the host state, trust, and exec outcome that produced each.
    logger.info(
        "Host-health probe inputs for {}: host_state={!r} trusted={} exec_attempted={} timed_out={} provider_error={} -> {}",
        agent_id,
        host_state,
        classification_is_trustworthy,
        probe_exec_attempted,
        probe_timed_out,
        provider_error_message is not None,
        response.dispatch_tier.value,
    )
    return response
