"""Workspace recovery: host-health probe + restart worker.

These are the engine behind the recovery flow (the recovery page's diagnostics
list and its surgical / host restart actions). They are extracted here -- away
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
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
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
# How long we wait for the system interface to answer again after a restart,
# split by tier. A surgical (in-place) restart leaves the container running, so
# the interface should answer again quickly. A host restart cold-boots the
# container, which legitimately takes longer.
_SURGICAL_STARTUP_WAIT_SECONDS: Final[float] = 15.0
_HOST_RESTART_STARTUP_WAIT_SECONDS: Final[float] = 30.0
# Poll cadence while waiting for the system interface to come back post-restart.
_RESTART_PROBE_INTERVAL_SECONDS: Final[float] = 1.0


def _build_mngr_stop_argv(mngr_binary: str, agent_id: AgentId, is_host_restart: bool) -> list[str]:
    """Build the argv for ``mngr stop`` on ``agent_id`` -- with ``--stop-host`` for the host tier."""
    argv = [mngr_binary, "stop", str(agent_id), "--quiet"]
    if is_host_restart:
        argv.append("--stop-host")
    return argv


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
    is_host_restart: bool,
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str | None,
    registry: WorkspaceOperationRegistryInterface,
    skip_stop: bool = False,
) -> None:
    """Background worker: stop + start the system-services agent, then await recovery.

    Drives the health tracker to HEALTHY on recovery or RESTART_FAILED (with a
    reason) when a step errors or the system interface does not return within the
    tier's startup-wait budget (the host tier cold-boots a container, so it waits
    longer than the in-place surgical tier). In lockstep it appends progress lines
    to, and completes / fails, the v1 ``registry`` operation so the
    ``/workspaces/operations/restart/<id>`` resource can report the same restart. A crash
    of this worker is turned into RESTART_FAILED by ``RestartWorkerFailureHandler``,
    wired as the thread's ``on_failure`` callback.

    ``skip_stop`` is set only for the auto-dispatched host tier, which is chosen
    exclusively when the host-health probe found the container fully stopped --
    there is nothing to stop, so the (idempotent but not free) ``mngr stop
    --stop-host`` subprocess is skipped to shave a full mngr invocation off the
    cold boot's critical path.
    """
    tier_label = "host restart" if is_host_restart else "system-interface restart"
    startup_wait_seconds = _HOST_RESTART_STARTUP_WAIT_SECONDS if is_host_restart else _SURGICAL_STARTUP_WAIT_SECONDS
    registry.append_log(workspace_agent_id, f"Starting {tier_label}.")
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        message = "Could not locate the system-services agent for this workspace."
        tracker.mark_restart_failed(workspace_agent_id, message)
        registry.fail(workspace_agent_id, message)
        return

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)

    if skip_stop:
        logger.info("Skipping stop step for {} ({}): container already fully stopped", workspace_agent_id, tier_label)
        registry.append_log(workspace_agent_id, "Container already fully stopped; skipping stop step.")
    else:
        registry.append_log(workspace_agent_id, "Stopping the system-services agent.")
        try:
            _run_mngr(concurrency_group, _build_mngr_stop_argv(mngr_binary, services_agent_id, is_host_restart), env)
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
                    "Stop step of {} for {} skipped: provider does not support host shutdown; "
                    "restart proceeds via start alone",
                    tier_label,
                    workspace_agent_id,
                )
                registry.append_log(
                    workspace_agent_id, "Provider does not support stopping the host; skipping stop step."
                )
            else:
                logger.warning("Stop step of {} for {} failed: {}", tier_label, workspace_agent_id, exc)
                message = f"Stop step of {tier_label} failed: {exc}"
                tracker.mark_restart_failed(workspace_agent_id, message)
                registry.fail(workspace_agent_id, message)
                return

    registry.append_log(workspace_agent_id, "Starting the system-services agent.")
    try:
        _run_mngr(concurrency_group, _build_mngr_start_argv(mngr_binary, services_agent_id), env)
    except MngrCommandError as exc:
        logger.warning("Start step of {} for {} failed: {}", tier_label, workspace_agent_id, exc)
        message = f"Start step of {tier_label} failed: {exc}"
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
        message = f"The system interface did not respond within {int(startup_wait_seconds)}s of the {tier_label}."
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
    exec`` probe, which is fired only when the provider is reachable and the host
    is RUNNING so an outage never pays a doomed provider round-trip. The plugin's
    resolver-snapshot mirror supplies the last probe.

    Callers reach this only once discovery is fresh (the recovery redirect is
    gated on freshness in the chrome-events stream), so the host/provider state
    read here is trustworthy without a per-call freshness gate.
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

    # In-container exec probe, only when the provider is reachable and the host is
    # RUNNING. The exec SSHes to the container via ``get_host`` (the connector's
    # ~30s httpx), so it carries an explicit 30s-class cap and is skipped entirely
    # unless the provider has no surfaced error and the host is RUNNING. A
    # non-clean outcome leaves ``in_container_stdout`` None.
    in_container_stdout: str | None = None
    if services_agent_id is not None and provider_error_message is None and host_state_enum == HostState.RUNNING:
        try:
            in_container_stdout = _run_mngr(
                concurrency_group,
                build_probe_argv(mngr_binary, services_agent_id),
                env,
                timeout_seconds=_HOST_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
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
    return build_host_health_response(
        host_state=host_state,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services,
        mngr_exec_command=exec_command,
        mngr_binary=mngr_binary,
        provider_error_message=provider_error_message,
        provider_label=provider_label,
    )
