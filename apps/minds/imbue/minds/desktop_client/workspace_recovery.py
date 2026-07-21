"""Workspace recovery: host-health probe + restart worker.

These are the engine behind the recovery flow (the recovery page's diagnostics
list and its host restart action). They are extracted here -- away
from :mod:`app` -- so the versioned ``/api/v1`` surface (:mod:`api_v1`) can
drive them without importing :mod:`app` (which would form an import cycle, since
``app`` imports ``api_v1``).

``probe_workspace_health`` composes a :class:`HostHealthResponse` from the
passive discovery resolver plus a batched in-container ``mngr exec`` probe.
``run_restart_sequence`` is the background worker body (``mngr stop`` + ``mngr
start``, then await recovery) that drives both the
:class:`SystemInterfaceHealthTracker` (so the existing recovery page keeps
working) and a :class:`WorkspaceOperationRegistryInterface` (so the v1
``/workspaces/operations/restart/<id>`` resource can report restart status + logs).
"""

import os
import shlex
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.api.discovery_events import DISCOVERY_STREAM_POLL_INTERVAL_SECONDS
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName

# How long a single workspace probe through the plugin is allowed to hang.
# Short and snappy so a wedged workspace doesn't gate the recovery UI.
_WORKSPACE_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
# Default hard timeout for an ``mngr`` subprocess run via ``_run_mngr``. Generous
# because it is sized for the slowest legitimate case -- a host stop/start, which
# bounces a container and can take tens of seconds -- so it is a "definitely
# wedged" ceiling, not an estimate.
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
# Hard timeout for the recovery host-health probe's in-container ``mngr exec``.
# Far shorter than the default ceiling: this is a *diagnostic* that gates the
# recovery UI. The exec touches the provider (``get_host`` -> the connector's
# ~30s httpx) before reaching the container, so it must carry its own 30s-class
# cap rather than inheriting the 120s default.
_HOST_HEALTH_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
# How long we wait for the system interface to answer again after a restart.
# The host restart cold-boots the container, so this is sized for a full boot.
_HOST_RESTART_STARTUP_WAIT_SECONDS: Final[float] = 30.0
# Poll cadence while waiting for the system interface to come back post-restart.
_RESTART_PROBE_INTERVAL_SECONDS: Final[float] = 1.0
# How recent the last discovery snapshot must be to trust the host state it
# reports when deriving a recovery verdict. A healthy discovery poll emits a
# snapshot every ``DISCOVERY_STREAM_POLL_INTERVAL_SECONDS``; three missed
# snapshots means the pipeline has stalled, so the state it last reported can no
# longer be trusted. The 3x multiple stays comfortably above the normal
# inter-snapshot interval to avoid a false "stale" during a single slow poll.
_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS: Final[float] = 3 * DISCOVERY_STREAM_POLL_INTERVAL_SECONDS


def _is_discovery_fresh(last_snapshot_at: datetime | None) -> bool:
    """Whether the most recent discovery snapshot is recent enough to trust.

    A snapshot older than ``_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS`` (or no
    snapshot at all) means discovery has stalled -- the resolver's host state may
    pre-date an outage -- so reachability cannot be positively established.
    """
    if last_snapshot_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - last_snapshot_at).total_seconds()
    return age_seconds <= _DISCOVERY_FRESHNESS_THRESHOLD_SECONDS


def _workspace_provider_snapshot_at(backend_resolver: MngrCliBackendResolver, agent_id: AgentId) -> datetime | None:
    """Last per-provider snapshot time for ``agent_id``'s provider, or the aggregate fallback.

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
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is not None and info.provider_name is not None:
        return backend_resolver.get_last_snapshot_at_for_provider(ProviderInstanceName(info.provider_name))
    _, aggregate_snapshot_at = backend_resolver.get_freshness_timestamps()
    return aggregate_snapshot_at


def is_recovery_classification_trustworthy(
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    agent_id: AgentId,
) -> bool:
    """Whether the resolver's host state is fresh enough to base a recovery verdict on.

    A negative recovery verdict (or an auto-dispatched restart) leans on the host
    state the passive discovery resolver reports. That state is only trustworthy
    once a full snapshot taken at/after the outage onset
    (``get_failure_run_started_wall_at``) has landed: a snapshot that predates the
    outage still carries the pre-outage host state (a just-stopped container still
    reads RUNNING), which would misclassify the tier. Until then the verdict path
    treats the classification as untrustworthy and surfaces INDETERMINATE.

    When no onset is recorded (only the force-``mark_stuck`` path, used in tests,
    lacks one) fall back to the absolute-age freshness gate. Only the
    passive-discovery resolver tracks snapshot freshness; for any other resolver
    (e.g. static test resolvers) the classification is treated as trustworthy so
    the verdict path is never gated. Freshness is scoped to the workspace's own
    provider (see ``_workspace_provider_snapshot_at``).
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return True
    last_snapshot_at = _workspace_provider_snapshot_at(backend_resolver, agent_id)
    onset = tracker.get_failure_run_started_wall_at(agent_id) if tracker is not None else None
    if onset is None:
        return _is_discovery_fresh(last_snapshot_at)
    return last_snapshot_at is not None and last_snapshot_at >= onset


# How long without any discovery snapshot for a workspace's provider before we
# consider discovery itself stalled (a dead/wedged producer, not just a snapshot
# that has not landed yet). Providers poll every 30s by default
# (``discovery_poll_interval_seconds``), so this is three missed polls -- well
# past the freshness-gate wait, which resolves within one poll when discovery is
# healthy. A stalled stream means the snapshot-freshness gate can never open, so
# the probe path stops waiting for it and gathers direct evidence instead.
_DISCOVERY_STALL_THRESHOLD_SECONDS: Final[float] = 90.0


def is_workspace_discovery_stalled(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    """Whether discovery has stopped producing snapshots for ``agent_id``'s provider.

    True when the workspace's provider has no snapshot at all or its latest one
    is older than ``_DISCOVERY_STALL_THRESHOLD_SECONDS``. Distinct from the
    onset-based trustworthiness gate: that gate answers "has discovery
    re-observed the host since the outage began?" and is expected to open within
    one poll interval; this answers "is discovery producing observations at
    all?". Only the passive-discovery resolver tracks snapshot times; any other
    resolver (e.g. static test resolvers) is never considered stalled.
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return False
    last_snapshot_at = _workspace_provider_snapshot_at(backend_resolver, agent_id)
    if last_snapshot_at is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - last_snapshot_at).total_seconds()
    return age_seconds > _DISCOVERY_STALL_THRESHOLD_SECONDS


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


def _await_system_interface_ready(
    agent_id: AgentId, mngr_forward_port: int, preauth_cookie: str, wait_seconds: float
) -> bool:
    """Poll the system interface through the plugin until it answers 200, or ``wait_seconds`` elapses."""
    deadline = time.monotonic() + wait_seconds
    with make_workspace_probe_client(
        preauth_cookie=preauth_cookie,
        probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
    ) as probe_client:
        while time.monotonic() < deadline:
            status = probe_workspace_through_plugin(
                mngr_forward_port=mngr_forward_port,
                preauth_cookie=preauth_cookie,
                agent_id=agent_id,
                probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                client=probe_client,
            )
            if status == 200:
                return True
            threading.Event().wait(timeout=_RESTART_PROBE_INTERVAL_SECONDS)
    return False


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
    reporting even though the page renders it for the user.

    ``skip_stop`` is set only for the auto-dispatched restart, which is chosen
    exclusively when the host-health probe found the container fully stopped --
    there is nothing to stop, so the (idempotent but not free) ``mngr stop
    --stop-host`` subprocess is skipped to shave a full mngr invocation off the
    cold boot's critical path.
    """
    registry.append_log(workspace_agent_id, "Starting host restart.")
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        message = "Could not locate the system-services agent for this workspace."
        logger.error("Host restart of {} failed: {}", workspace_agent_id, message)
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)

    if skip_stop:
        logger.info("Skipping stop step for {}: container already fully stopped", workspace_agent_id)
        registry.append_log(workspace_agent_id, "Container already fully stopped; skipping stop step.")
    else:
        registry.append_log(workspace_agent_id, "Stopping the system-services agent.")
        try:
            _run_mngr(concurrency_group, _build_mngr_stop_argv(mngr_binary, services_agent_id), env)
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
                logger.error("Stop step of host restart for {} failed: {}", workspace_agent_id, exc)
                message = f"Stop step of host restart failed: {exc}"
                tracker.mark_restart_failed(workspace_agent_id, message)
                registry.fail(workspace_agent_id, message)
                return

    registry.append_log(workspace_agent_id, "Starting the system-services agent.")
    try:
        _run_mngr(concurrency_group, _build_mngr_start_argv(mngr_binary, services_agent_id), env)
    except MngrCommandError as exc:
        logger.error("Start step of host restart for {} failed: {}", workspace_agent_id, exc)
        message = f"Start step of host restart failed: {exc}"
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return

    # Without a plugin route there is no way to probe for recovery, so treat a
    # clean dispatch as success (mirrors the background probe loop being a no-op).
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie:
        tracker.record_probe_success(workspace_agent_id)
        registry.append_log(workspace_agent_id, "Restart dispatched.")
        registry.complete(workspace_agent_id)
        return

    registry.append_log(workspace_agent_id, "Waiting for the system interface to respond.")
    if _await_system_interface_ready(
        workspace_agent_id, mngr_forward_port, mngr_forward_preauth_cookie, startup_wait_seconds
    ):
        tracker.record_probe_success(workspace_agent_id)
        registry.append_log(workspace_agent_id, "The system interface is responding again.")
        registry.complete(workspace_agent_id)
    else:
        message = f"The system interface did not respond within {int(startup_wait_seconds)}s of the host restart."
        logger.error("Host restart of {} failed: {}", workspace_agent_id, message)
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)


def _provider_error_message_for_workspace(
    provider_errors: Mapping[ProviderInstanceName, DiscoveryError], provider_name: str | None
) -> str | None:
    """Map this workspace's provider error message (if any) from the discovery snapshot.

    ``get_provider_errors()`` keys per-provider discovery errors by provider
    name, so attribution to *this* workspace's provider is exact. Returns None in
    the brief pre-discovery window where the provider is unknown
    (``provider_name is None``), and None when this workspace's provider has no
    surfaced error. Otherwise returns the provider's own error message.
    """
    if provider_name is None:
        return None
    for name, error in provider_errors.items():
        if str(name) == provider_name:
            return error.message
    return None


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
    exec`` probe, which is fired when the provider is reachable and the host is
    RUNNING (so an outage never pays a doomed provider round-trip), and also --
    regardless of the recorded host state -- when discovery itself has stalled
    (``is_workspace_discovery_stalled``): with no snapshots flowing, the exec's
    own outcome is the only direct evidence available. The plugin's
    resolver-snapshot mirror supplies the last probe.

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

    The host state that feeds the classifier is re-read after the exec, at the
    same instant the trustworthiness check runs, so the freshness gate always
    certifies the state that is actually classified (a snapshot landing during
    the slow exec would otherwise open the gate for a pre-snapshot reading).
    """
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    services_agent_id = backend_resolver.get_system_services_agent_id(agent_id)
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    # Friendly provider name for the "Can't connect to ..." page title.
    provider_label = friendly_provider_label(provider_name) or "the workspace backend"

    # Read host/provider state from the passive discovery resolver.
    host_state_enum = (
        backend_resolver.get_host_state(HostId(display_info.host_id)) if display_info is not None else None
    )
    host_state = host_state_enum.value if host_state_enum is not None else ""
    provider_error_message = _provider_error_message_for_workspace(
        backend_resolver.get_provider_errors(), provider_name
    )

    # In-container exec probe, only when the provider is reachable and either the
    # host is RUNNING or discovery itself has stalled. The exec SSHes to the
    # container via ``get_host`` (the connector's ~30s httpx), so it carries an
    # explicit 30s-class cap and is skipped when the provider has a surfaced
    # error or the (still-flowing) discovery stream already shows the host not
    # running -- an outage never pays a doomed provider round-trip. When
    # discovery has stalled (a dead producer), the host state can never be
    # re-certified and the freshness gate can never open, so the exec is
    # attempted regardless of the stale state: its outcome is the only direct
    # evidence available, and it resolves the page to HEALTHY or a consent-gated
    # HOST_UNRESPONSIVE instead of an indefinite INDETERMINATE. A non-clean
    # outcome leaves ``in_container_stdout`` None.
    in_container_stdout: str | None = None
    probe_timed_out = False
    probe_exec_attempted = False
    if (
        services_agent_id is not None
        and provider_error_message is None
        and (host_state_enum == HostState.RUNNING or is_workspace_discovery_stalled(backend_resolver, agent_id))
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
        host_state_enum = backend_resolver.get_host_state(HostId(display_info.host_id))
        host_state = host_state_enum.value if host_state_enum is not None else ""
    classification_is_trustworthy = is_recovery_classification_trustworthy(backend_resolver, tracker, agent_id)
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
