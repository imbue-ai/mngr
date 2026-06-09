"""Derives container liveness of *local* minds (docker / lima hosts) for the
landing-page Start/Stop controls and the quit-time shutdown prompt.

The global discovery snapshot already carries each host's lifecycle state (it is
written on every poll by the single ``mngr observe --discovery-only`` and folded
into :class:`MngrCliBackendResolver` as ``host_state_by_host_id``), and the
resolver also applies a short-lived *optimistic override* on ``get_host_state``
when a UI Start/Stop fires (see ``set_host_state_override``). So this module owns
no state machinery of its own; it just classifies the resolver's host state and
scopes it to local minds:

- ``classify_host_state`` -- maps a discovery ``HostState`` to the coarse
  RUNNING / STOPPED / UNKNOWN the UI shows.
- ``get_local_workspace_agent_ids`` -- which active workspaces are local by
  their provider backend.
- ``compute_local_mind_state_by_agent_id`` -- the per-mind liveness map the
  landing page, chrome SSE, and quit prompt read.

``--discovery-only`` drops only the per-*agent* lifecycle/activity streams (the
agent process's own state); it keeps host/container state, which is exactly what
"is this container up?" needs.
"""

from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# Provider backends whose hosts run on the user's own machine and so consume
# local resources while alive. Only minds on these backends are surfaced for the
# quit-time shutdown prompt and the landing-page Start/Stop controls; remote
# minds (Modal, OVH, ...) have no "stop to free local resources" concept.
LOCAL_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset({"docker", "lima"})

# Discovery ``HostState`` values that mean the container exists but is not
# running. Mirrors the offline set the recovery-diagnostics probe uses.
_OFFLINE_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {HostState.STOPPED, HostState.STOPPING, HostState.CRASHED, HostState.FAILED}
)


class LocalMindState(UpperCaseStrEnum):
    """Container liveness of a local mind, surfaced to the landing page + quit prompt."""

    RUNNING = auto()
    STOPPED = auto()
    UNKNOWN = auto()


def classify_host_state(host_state: HostState | None) -> LocalMindState:
    """Classify a discovery ``HostState`` into the coarse liveness the UI shows.

    ``None`` (host state not known to discovery yet) and transient/odd states map
    to UNKNOWN so the UI can distinguish "we can't tell" from "confirmed stopped".
    """
    if host_state is HostState.RUNNING:
        return LocalMindState.RUNNING
    if host_state in _OFFLINE_HOST_STATES:
        return LocalMindState.STOPPED
    return LocalMindState.UNKNOWN


def _build_backend_by_provider_name(backend_resolver: BackendResolverInterface) -> dict[str, str]:
    """Map each known provider instance name to its backend (e.g. 'docker', 'modal')."""
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return {}
    return {
        str(provider.provider_name): str(provider.config.backend) for provider in backend_resolver.list_providers()
    }


def get_local_workspace_agent_ids(backend_resolver: BackendResolverInterface) -> tuple[AgentId, ...]:
    """Return active workspace agent ids whose host runs on a local provider backend (docker / lima).

    Scopes to ``list_active_workspace_ids`` (not the full ``list_known_workspace_ids``)
    so destroyed-host workspaces -- which have no landing row -- are not tracked; the
    Start/Stop controls and quit prompt are active-workspace surfaces.
    """
    backend_by_provider_name = _build_backend_by_provider_name(backend_resolver)
    local_agent_ids: list[AgentId] = []
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None or info.provider_name is None:
            continue
        backend = backend_by_provider_name.get(info.provider_name)
        if backend is not None and backend in LOCAL_PROVIDER_BACKENDS:
            local_agent_ids.append(agent_id)
    return tuple(local_agent_ids)


def compute_local_mind_state_by_agent_id(backend_resolver: BackendResolverInterface) -> dict[str, LocalMindState]:
    """Return ``{agent_id_str: LocalMindState}`` for every active local mind.

    Reads each local mind's host state from the resolver via ``get_host_state``,
    which already layers any short-lived optimistic override (set by a Start/Stop
    action) over the discovery snapshot -- so a just-issued action shows up here
    immediately and reconciles back to discovery on its own.
    """
    result: dict[str, LocalMindState] = {}
    for agent_id in get_local_workspace_agent_ids(backend_resolver):
        info = backend_resolver.get_agent_display_info(agent_id)
        host_state = backend_resolver.get_host_state(HostId(info.host_id)) if info is not None else None
        result[str(agent_id)] = classify_host_state(host_state)
    return result
