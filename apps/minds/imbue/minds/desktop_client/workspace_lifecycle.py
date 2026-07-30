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
from imbue.minds.desktop_client.chrome_event_broadcast import ChromeEventBroadcaster
from imbue.minds.desktop_client.chrome_event_broadcast import build_workspace_stopped_payload
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
# a fresh workspace build; later stops sync deltas and take ~1-2 min). START just
# resumes a disk-intact VM (no mirror), so it keeps the original short cap -- a
# genuinely hung start should surface quickly, not hold the UI for 20 minutes.
_HOST_STOP_TIMEOUT_SECONDS: Final[float] = 1200.0
_HOST_START_TIMEOUT_SECONDS: Final[float] = 300.0


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


def perform_mind_host_action(
    workspace_agent_id: AgentId,
    action: MindHostAction,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    chrome_event_broadcaster: ChromeEventBroadcaster,
) -> MindHostActionOutcome:
    """Stop or start one mind's host, running ``mngr`` to completion.

    Resolves the workspace to its system-services (primary) agent -- the host's
    stop/start target -- and runs ``mngr stop --stop-host`` / ``mngr start``
    synchronously. On success sets the optimistic host-state override (so the UI
    flips immediately, reconciling on the next discovery snapshot); on failure
    clears any override so the UI reverts to the authoritative discovery state.

    A successful STOP also broadcasts a one-shot ``workspace_stopped`` payload on
    ``chrome_event_broadcaster``, so any window still open to the workspace
    closes instead of observing the dead interface and auto-restarting the host
    -- which would silently undo the stop.
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
            timeout_seconds = _HOST_STOP_TIMEOUT_SECONDS
        case MindHostAction.START:
            argv = [mngr_binary, "start", str(services_agent_id), "--quiet"]
            transitional_state = HostState.STARTING
            timeout_seconds = _HOST_START_TIMEOUT_SECONDS
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

    cg = concurrency_group.make_concurrency_group(name="workspace-lifecycle")
    try:
        with cg:
            finished = cg.run_process_to_completion(argv, timeout=timeout_seconds, is_checked_after=False, env=env)
    except (OSError, ConcurrencyGroupError) as exc:
        logger.warning("Could not run mngr to {} host for {}: {!r}", action.value, workspace_agent_id, exc)
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
            backend_resolver.clear_host_lifecycle_transition(host_id)
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
        return MindHostActionOutcome(is_successful=False, failure_reason=failure_reason)

    if host_id is not None:
        match action:
            case MindHostAction.STOP:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
            case MindHostAction.START:
                backend_resolver.set_host_state_override(host_id, HostState.RUNNING)
            case _ as unreachable:
                assert_never(unreachable)
    if action is MindHostAction.STOP:
        chrome_event_broadcaster.broadcast(build_workspace_stopped_payload(str(workspace_agent_id)))
    return MindHostActionOutcome(is_successful=True)
