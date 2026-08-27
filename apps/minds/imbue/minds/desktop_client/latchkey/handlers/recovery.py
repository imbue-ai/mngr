"""Host-permissions recovery shared by the grant handlers.

Lives alongside the handlers (like :mod:`.messaging`) so the two that
need it -- :mod:`.predefined` and :mod:`.file_sharing` -- import a
sibling rather than each other. Runs at grant time, the moment the
canonical per-host ``latchkey_permissions.json`` must exist for the
approval to take effect: a host whose canonical file was never
materialized would otherwise take the grant into a file the agent's
gateway JWT does not resolve to.
"""

from pathlib import Path

from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import maybe_recover_host_permissions_for_agent
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyStoreError


def maybe_recover_host_permissions(
    latchkey: Latchkey,
    backend_resolver: BackendResolverInterface,
    event: StreamedPermissionRequest,
) -> None:
    """Recreate a missing per-host permissions file for the request's agent.

    The request's ``target`` is the agent's opaque permissions handle (what
    its gateway JWT resolves to).
    :func:`maybe_recover_host_permissions_for_agent` swings that handle into
    the canonical host path when the latter is missing and idempotently
    re-registers the agent in the host's allowlist. Best-effort: failures are
    logged and the grant proceeds (it can still land in the opaque file).
    No-op when the host is not yet known to discovery.
    """
    target = event.target
    agent_id = AgentId(event.agent_id)
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is None:
        return
    try:
        host_id = HostId(info.host_id)
    except ValueError:
        # Placeholder host ids ("localhost", static/in-memory resolvers) mean
        # "unknown host" -- nothing to recover into.
        return
    try:
        did_recover = maybe_recover_host_permissions_for_agent(
            latchkey=latchkey,
            host_id=host_id,
            agent_id=agent_id,
            opaque_permissions_path=Path(target),
        )
    except LatchkeyStoreError as e:
        logger.opt(exception=e).error(
            "Could not recover missing latchkey permissions file for host {} (agent {}): {}",
            host_id,
            event.agent_id,
            e,
        )
        return
    if did_recover:
        logger.info(
            "Recovered missing latchkey permissions file for host {} (agent {}) from opaque handle {}",
            host_id,
            event.agent_id,
            target,
        )
