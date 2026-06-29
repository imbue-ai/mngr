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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# A host stop/start shells out to ``mngr`` and blocks until the host transition
# resolves before returning the outcome.
_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 300.0


class MindHostAction(UpperCaseStrEnum):
    """Which lifecycle action a Start/Stop runs on a mind's host."""

    STOP = auto()
    START = auto()


def perform_mind_host_action(
    workspace_agent_id: AgentId,
    action: MindHostAction,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> bool:
    """Stop or start one mind's host, running ``mngr`` to completion; return True on success.

    Resolves the workspace to its system-services (primary) agent -- the host's
    stop/start target -- and runs ``mngr stop --stop-host`` / ``mngr start``
    synchronously. On success sets the optimistic host-state override (so the UI
    flips immediately, reconciling on the next discovery snapshot); on failure
    clears any override so the UI reverts to the authoritative discovery state.
    """
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        logger.warning(
            "Could not locate the system-services agent to {} host for {}", action.value, workspace_agent_id
        )
        return False
    info = backend_resolver.get_agent_display_info(workspace_agent_id)
    host_id = HostId(info.host_id) if info is not None else None
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    match action:
        case MindHostAction.STOP:
            argv = [mngr_binary, "stop", str(services_agent_id), "--quiet", "--stop-host"]
        case MindHostAction.START:
            argv = [mngr_binary, "start", str(services_agent_id), "--quiet"]
        case _ as unreachable:
            assert_never(unreachable)

    cg = concurrency_group.make_concurrency_group(name="workspace-lifecycle")
    try:
        with cg:
            finished = cg.run_process_to_completion(
                argv, timeout=_LIFECYCLE_TIMEOUT_SECONDS, is_checked_after=False, env=env
            )
    except (OSError, ConcurrencyGroupError) as exc:
        logger.warning("Could not run mngr to {} host for {}: {!r}", action.value, workspace_agent_id, exc)
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
        return False
    if finished.returncode != 0:
        logger.warning(
            "Host {} for {} failed (rc={}): {}",
            action.value,
            workspace_agent_id,
            finished.returncode,
            finished.stderr.strip(),
        )
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
        return False

    if host_id is not None:
        match action:
            case MindHostAction.STOP:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
            case MindHostAction.START:
                backend_resolver.set_host_state_override(host_id, HostState.RUNNING)
            case _ as unreachable:
                assert_never(unreachable)
    return True
