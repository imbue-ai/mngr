"""Shared workspace host lifecycle (start / stop) for the minds desktop client.

Extracted from ``app.py`` so both the browser-facing landing controls (in
``app.py``) and the agent-facing ``/api/v1/workspaces/<id>/start|stop`` routes
(in ``api_v1.py``) run the same host stop/start with the same system-services
resolution and the same optimistic host-state override. ``api_v1`` cannot import
``app.py`` (cycle), so this lower-level module is the single home both import.
"""

import os
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.ui_models import UiWorkspaceStoppedMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# A host stop/start shells out to ``mngr`` and blocks until the host transition
# resolves before returning the outcome. A timeout here is reported to the UI as
# a *failure* even though the underlying mngr keeps running -- so a too-small cap
# manufactures false failures for flows that are actually working.
#
# Only STOP is slow: a cloud VM's FIRST stop mirrors the entire host_dir to the
# provider's state store before deallocating (observed ~10 minutes on Azure after
# a fresh workspace build; later stops sync deltas and take ~1-2 min). START of a
# local/BYO-cloud host resumes a disk-intact VM quickly, but an imbue_cloud
# start may restore the workspace from object storage onto a fresh box
# (download + boot + container relaunch, and the mngr client polls the
# connector for up to 20 minutes), so START now shares the generous cap --
# mngr reports genuine failures well before it.
#
# Public: the recovery worker runs the same host stop and start commands
# through its own mngr subprocesses and must share these budgets, or a click
# on a stopped cloud machine manufactures a spurious timeout failure while
# the underlying start keeps running.
HOST_STOP_TIMEOUT_SECONDS: Final[float] = 1200.0
HOST_START_TIMEOUT_SECONDS: Final[float] = 1260.0


class MindHostAction(UpperCaseStrEnum):
    """Which lifecycle action a Start/Stop runs on a mind's host."""

    STOP = auto()
    START = auto()


class MindHostActionOutcome(FrozenModel):
    """Whether a host stop/start succeeded, and why it did not.

    Carries ``failure_reason`` so callers can report what mngr actually said.
    A bare success flag left the API answering "Could not start the workspace
    host" with no cause, which reads as an unreachable box even when mngr
    failed for a reason it stated plainly.
    """

    is_successful: bool = Field(description="True when the host transition completed")
    failure_reason: str | None = Field(
        default=None, description="What mngr reported when the action failed; None on success"
    )


def _lead_with_error_lines(stderr: str) -> str:
    """Reorder an ``mngr`` run's stderr so its verdict comes first.

    A failing run emits its provider-level ``WARNING:`` lines first and its
    ``ERROR:`` verdict last, and the warnings routinely concern hosts other than
    the one asked about -- a stale key dir for some long-gone workspace reads as
    "outer SSH unreachable", which sounds exactly like the box being down. So a
    reader who takes the output at face value gets the wrong story.

    The defect is the ordering, not the warnings: they are real diagnostics (the
    stale key dirs above are a genuine bug worth chasing) and dropping them would
    cost a reader the only copy they have, since a caller on another host cannot
    read this one's logs. So nothing is filtered -- the ``ERROR:`` lines are
    promoted ahead of the rest, which follows verbatim.
    """
    lines = [line.rstrip() for line in stderr.splitlines() if line.strip()]
    error_lines = [line for line in lines if line.strip().startswith("ERROR:")]
    if not error_lines:
        return stderr.strip()
    remainder = [line for line in lines if not line.strip().startswith("ERROR:")]
    return "\n".join([*error_lines, *remainder])


def _restore_unattended_recovery_after_failed_stop(
    health_tracker: SystemInterfaceHealthTracker | None,
    action: MindHostAction,
    workspace_agent_id: AgentId,
) -> None:
    """Undo the pre-command STOP mark when the stop did not happen.

    A stop that errored leaves the machine as it was, so it must go back to
    healing itself; the mark would otherwise exclude it for the rest of the
    process's life. No-op for START, which never marks.
    """
    if health_tracker is not None and action is MindHostAction.STOP:
        health_tracker.allow_unattended_recovery(workspace_agent_id)


def perform_mind_host_action(
    workspace_agent_id: AgentId,
    action: MindHostAction,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    ui_publisher: UiStatePublisher | None,
    # Nullable but NOT defaulted: an install with no tracker wired passes None,
    # but a caller must say so, since a default would let a new call site skip
    # the unattended-recovery suppression by omission.
    health_tracker: SystemInterfaceHealthTracker | None,
) -> MindHostActionOutcome:
    """Stop or start one mind's host, running ``mngr`` to completion.

    Resolves the workspace to its system-services (primary) agent -- the host's
    stop/start target -- and runs ``mngr stop --stop-host`` / ``mngr start``
    synchronously. On success sets the optimistic host-state override (so the UI
    flips immediately, reconciling on the next discovery snapshot); on failure
    clears any override so the UI reverts to the authoritative discovery state.

    A successful STOP also publishes a one-shot ``workspace_stopped`` frame on
    the ``/ui/ws`` channel, so any window still open to the workspace closes
    instead of observing the dead interface.

    A STOP additionally marks ``health_tracker`` so unattended recovery leaves
    the host alone, and a successful START clears that mark. The mark goes on
    before the stop runs, alongside the optimistic host-state override: the
    interface dies the moment the stop begins, and STUCK is reached seconds
    later, so a mark applied only once ``mngr`` returned would lose that race.
    That first mark says the stop is still in flight, which keeps a probe taken
    on the way down from clearing it; it is set again as an ordinary mark on
    success, and cleared if the stop failed. See
    :meth:`SystemInterfaceHealthTracker.suppress_unattended_recovery`.
    """
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        logger.warning(
            "Could not locate the system-services agent to {} host for {}", action.value, workspace_agent_id
        )
        return MindHostActionOutcome(
            is_successful=False,
            failure_reason="could not locate the workspace's system-services agent",
        )
    info = backend_resolver.get_agent_display_info(workspace_agent_id)
    host_id = HostId(info.host_id) if info is not None else None
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    match action:
        case MindHostAction.STOP:
            argv = [mngr_binary, "stop", str(services_agent_id), "--quiet", "--stop-host"]
            transitional_state = HostState.STOPPING
            timeout_seconds = HOST_STOP_TIMEOUT_SECONDS
        case MindHostAction.START:
            argv = [mngr_binary, "start", str(services_agent_id), "--quiet"]
            transitional_state = HostState.STARTING
            timeout_seconds = HOST_START_TIMEOUT_SECONDS
        case _ as unreachable:
            assert_never(unreachable)

    if host_id is not None:
        # Before the (blocking, possibly minutes-long) mngr call -- during which
        # the VM drops out of discovery -- retain the workspace row and flip the
        # badge to the transitional state immediately. The retention keeps the row
        # on the landing page even across a page reload (an in-flight action's
        # frontend state does not survive one); it is cleared on failure below and
        # swept once discovery re-lists the host on success.
        backend_resolver.mark_host_lifecycle_transition_started(host_id)
        backend_resolver.set_host_state_override(host_id, transitional_state)
    # Likewise before the call: the stop takes the interface down within
    # seconds, long before ``mngr`` returns, and the probe loop needs only
    # ``stuck_threshold_seconds`` of that to hand the agent to the unattended
    # dispatch.
    if health_tracker is not None and action is MindHostAction.STOP:
        health_tracker.suppress_unattended_recovery(workspace_agent_id, is_stop_in_flight=True)

    cg = concurrency_group.make_concurrency_group(name="workspace-lifecycle")
    try:
        with cg:
            finished = cg.run_process_to_completion(argv, timeout=timeout_seconds, is_checked_after=False, env=env)
    except (OSError, ConcurrencyGroupError) as exc:
        logger.warning("Could not run mngr to {} host for {}: {!r}", action.value, workspace_agent_id, exc)
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
            backend_resolver.clear_host_lifecycle_transition(host_id)
        _restore_unattended_recovery_after_failed_stop(health_tracker, action, workspace_agent_id)
        return MindHostActionOutcome(is_successful=False, failure_reason=f"could not run mngr: {exc}")
    if finished.returncode != 0:
        # mngr's own diagnosis, reordered to lead with its verdict; the warnings
        # it emits first can name unrelated hosts and read as this host being
        # unreachable. They are kept -- a caller on another host has no other
        # copy of them.
        failure_reason = _lead_with_error_lines(finished.stderr)
        logger.warning(
            "Host {} for {} failed (rc={}): {}",
            action.value,
            workspace_agent_id,
            finished.returncode,
            finished.stderr.strip(),
        )
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
            backend_resolver.clear_host_lifecycle_transition(host_id)
        _restore_unattended_recovery_after_failed_stop(health_tracker, action, workspace_agent_id)
        return MindHostActionOutcome(is_successful=False, failure_reason=failure_reason)

    if host_id is not None:
        match action:
            case MindHostAction.STOP:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
            case MindHostAction.START:
                backend_resolver.set_host_state_override(host_id, HostState.RUNNING)
            case _ as unreachable:
                assert_never(unreachable)
    # The START counterpart of the pre-command STOP mark. Only on success: a
    # start still in flight leaves the interface unreachable too, and clearing
    # early would let the dispatch stack its own restart on top of this one.
    if health_tracker is not None and action is MindHostAction.START:
        health_tracker.allow_unattended_recovery(workspace_agent_id)
    # The STOP mark again, now as an ordinary one: nothing can answer a probe
    # from here on, so the mark can go back to being cleared by the first
    # machine that does.
    if health_tracker is not None and action is MindHostAction.STOP:
        health_tracker.suppress_unattended_recovery(workspace_agent_id)
    if action is MindHostAction.STOP and ui_publisher is not None:
        ui_publisher.publish_one_shot(UiWorkspaceStoppedMessage(agent_id=str(workspace_agent_id)))
    return MindHostActionOutcome(is_successful=True)
