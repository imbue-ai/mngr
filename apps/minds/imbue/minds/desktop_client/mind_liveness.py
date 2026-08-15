"""Derives container liveness of minds whose host can be shut down (and started)
from minds, for the landing-page Start/Stop controls and the quit-time shutdown
prompt.

The global discovery snapshot already carries each host's lifecycle state (it is
written on every poll by the single ``mngr observe --discovery-only`` and folded
into :class:`MngrCliBackendResolver` as ``host_state_by_host_id``), and the
resolver also applies a short-lived *optimistic override* on ``get_host_state``
when a UI Start/Stop fires (see ``set_host_state_override``). So this module owns
no state machinery of its own; it just classifies the resolver's host state and
scopes it to shutdown-capable minds:

- ``provider_backend_supports_shutdown`` -- the *single* gate for "can this
  provider's host be stopped/started from minds today?" The local (docker /
  lima) backends, the cloud-VM backends (aws / gcp / azure), and imbue_cloud
  workspaces qualify; widen this one predicate when other providers gain host
  shutdown support.
- ``classify_host_state`` -- maps a discovery ``HostState`` to the coarse
  RUNNING / STOPPED / UNKNOWN the UI shows.
- ``get_shutdown_capable_workspace_agent_ids`` -- which active workspaces sit on
  a shutdown-capable provider.
- ``compute_mind_liveness_by_agent_id`` -- the per-mind liveness map the
  workspace list and quit prompt read (one resolver walk).

``--discovery-only`` drops only the per-*agent* lifecycle/activity streams (the
agent process's own state); it keeps host/container state, which is exactly what
"is this container up?" needs.
"""

from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# Provider backends whose hosts can currently be stopped and started from minds.
# Local backends (docker / lima) consume the user's own machine while alive, and
# the cloud-VM backends (aws / gcp / azure) support real VM-level stop/start via
# ``mngr stop --stop-host`` (EC2 stop / GCE stop / Azure deallocate) -- for the
# bring-your-own-key-account flow, stopping is what halts the user's own cloud
# billing, and the provider's offline state bucket keeps a stopped workspace
# visible in the UI. imbue_cloud workspaces now support full VM-level
# stop/start too: ``mngr stop`` halts the slice VM and uploads it to the
# tier's storage bucket (freeing the bare-metal slot), and ``mngr start``
# restores it -- in place within the retention window, or onto any
# same-region box after it. Remote backends without a real host-stop
# (Modal, OVH leases) stay out. This is the *one* place that encodes that
# restriction: when another provider gains host shutdown support, widen this set
# (or replace it with a richer per-provider capability check) and every Start /
# Stop surface follows. See ``provider_backend_supports_shutdown``.
_SHUTDOWN_CAPABLE_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset(
    {"docker", "lima", "aws", "gcp", "azure", "imbue_cloud"}
)

# Discovery ``HostState`` values that mean the container exists but is not
# running. Mirrors the offline set the recovery-diagnostics probe uses.
_OFFLINE_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {HostState.STOPPED, HostState.STOPPING, HostState.CRASHED, HostState.FAILED}
)


class MindLiveness(UpperCaseStrEnum):
    """Container liveness of a mind, surfaced to the landing page + quit prompt."""

    RUNNING = auto()
    STOPPED = auto()
    UNKNOWN = auto()


def provider_backend_supports_shutdown(backend: str) -> bool:
    """Whether a provider on ``backend`` exposes host stop/start to minds today.

    The single gate behind every Start / Stop surface and the quit-time prompt.
    Local (docker / lima), BYO-cloud VM (aws / gcp / azure), and imbue_cloud
    backends qualify; widen this when other providers gain host shutdown support.
    """
    return backend in _SHUTDOWN_CAPABLE_PROVIDER_BACKENDS


def classify_host_state(host_state: HostState | None) -> MindLiveness:
    """Classify a discovery ``HostState`` into the coarse liveness the UI shows.

    ``None`` (host state not known to discovery yet) and transient/odd states map
    to UNKNOWN so the UI can distinguish "we can't tell" from "confirmed stopped".
    """
    if host_state is HostState.RUNNING:
        return MindLiveness.RUNNING
    if host_state in _OFFLINE_HOST_STATES:
        return MindLiveness.STOPPED
    return MindLiveness.UNKNOWN


def _build_backend_by_provider_name(backend_resolver: BackendResolverInterface) -> dict[str, str]:
    """Map each known provider instance name to its backend (e.g. 'docker', 'modal')."""
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return {}
    return {
        str(provider.provider_name): str(provider.config.backend) for provider in backend_resolver.list_providers()
    }


class _ShutdownCapableWorkspace(FrozenModel):
    """One active workspace on a shutdown-capable provider, from a resolver walk."""

    agent_id: AgentId = Field(description="Workspace agent id")
    host_id: HostId = Field(description="Host currently running the workspace")


def _walk_shutdown_capable_workspaces(backend_resolver: BackendResolverInterface) -> list[_ShutdownCapableWorkspace]:
    """The single resolver walk behind every shutdown-capability surface.

    Scopes to ``list_active_workspace_ids`` (not the full ``list_known_workspace_ids``)
    so destroyed-host workspaces -- which have no landing row -- are not tracked; the
    Start/Stop controls and quit prompt are active-workspace surfaces.
    """
    backend_by_provider_name = _build_backend_by_provider_name(backend_resolver)
    workspaces: list[_ShutdownCapableWorkspace] = []
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None or info.provider_name is None:
            continue
        backend = backend_by_provider_name.get(info.provider_name)
        if backend is not None and provider_backend_supports_shutdown(backend):
            workspaces.append(_ShutdownCapableWorkspace(agent_id=agent_id, host_id=HostId(info.host_id)))
    return workspaces


def get_shutdown_capable_workspace_agent_ids(backend_resolver: BackendResolverInterface) -> tuple[AgentId, ...]:
    """Return active workspace agent ids whose host runs on a shutdown-capable provider."""
    return tuple(workspace.agent_id for workspace in _walk_shutdown_capable_workspaces(backend_resolver))


def compute_mind_liveness_by_agent_id(backend_resolver: BackendResolverInterface) -> dict[str, MindLiveness]:
    """Return ``{agent_id_str: MindLiveness}`` for every active shutdown-capable mind.

    One resolver walk. Liveness reads each mind's host state via
    ``get_host_state``, which already layers any short-lived optimistic
    override (set by a Start/Stop action) over the discovery snapshot -- so a
    just-issued action shows up here immediately and reconciles back to
    discovery on its own.
    """
    result: dict[str, MindLiveness] = {}
    for workspace in _walk_shutdown_capable_workspaces(backend_resolver):
        result[str(workspace.agent_id)] = classify_host_state(backend_resolver.get_host_state(workspace.host_id))
    return result
