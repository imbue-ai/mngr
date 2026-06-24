"""Derives container liveness of minds whose host can be resumed (and, for local
providers, stopped) from minds, for the landing-page Start/Stop controls and the
quit-time shutdown prompt.

The global discovery snapshot already carries each host's lifecycle state (it is
written on every poll by the single ``mngr observe --discovery-only`` and folded
into :class:`MngrCliBackendResolver` as ``host_state_by_host_id``), and the
resolver also applies a short-lived *optimistic override* on ``get_host_state``
when a UI Start/Stop fires (see ``set_host_state_override``). So this module owns
no state machinery of its own; it just classifies the resolver's host state and
scopes it to the relevant minds:

- ``provider_backend_supports_shutdown`` -- the gate for "can minds *stop* this
  provider's host?" (docker / lima only; Modal cannot stop host compute). Gates
  the Stop button and the quit-time shutdown prompt.
- ``provider_backend_supports_resume`` -- the gate for "can minds *resume*
  (start) this provider's host?" A superset that also includes Modal (resumable
  from a snapshot). Gates the Start/Resume button and per-mind liveness.
- ``classify_host_state`` -- maps a discovery ``HostState`` to the coarse
  RUNNING / STOPPED / UNKNOWN the UI shows.
- ``get_shutdown_capable_workspace_agent_ids`` /
  ``get_resume_capable_workspace_agent_ids`` -- which active workspaces sit on a
  shutdown- / resume-capable provider.
- ``compute_mind_liveness_by_agent_id`` -- the per-mind liveness map the landing
  page, workspace-list SSE, and quit prompt read.

``--discovery-only`` drops only the per-*agent* lifecycle/activity streams (the
agent process's own state); it keeps host/container state, which is exactly what
"is this container up?" needs.
"""

from collections.abc import Callable
from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# Provider backends whose hosts minds can *stop* (and start). These are the local
# backends, whose hosts run on the user's own machine and so consume local
# resources while alive. Modal is deliberately excluded: ``mngr stop --stop-host``
# is unsupported for Modal (``supports_shutdown_hosts=False`` -- "Modal cannot stop
# host compute"), so a Stop button / the quit-time shutdown prompt would error.
# This set gates the Stop button and the quit prompt. See
# ``provider_backend_supports_shutdown``.
_SHUTDOWN_CAPABLE_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset({"docker", "lima"})

# Provider backends whose stopped/paused hosts minds can *resume* (start). It adds
# Modal -- whose hosts cannot be manually stopped but can be resumed from a
# snapshot (``mngr start`` restores the latest resumable snapshot) -- to the
# shutdown-capable set. Derived as a superset (rather than a hand-maintained
# literal) so the "every shutdown-capable backend is resume-capable" invariant
# holds automatically when the shutdown set grows. Gates the Start/Resume button
# and the per-mind liveness shown in the workspace list. See
# ``provider_backend_supports_resume``.
_RESUME_CAPABLE_PROVIDER_BACKENDS: Final[frozenset[str]] = _SHUTDOWN_CAPABLE_PROVIDER_BACKENDS | frozenset({"modal"})

# Discovery ``HostState`` values that mean the container exists but is not
# running. Kept in sync with the recovery-diagnostics probe's offline set
# (``recovery_probe._OFFLINE_HOST_STATES``) so the landing page and the recovery
# flow agree on what counts as "down". ``PAUSED`` is offline-but-resumable: a host
# that was snapshotted and torn down (Modal's idle/timeout path today; the state
# is provider-agnostic) maps to STOPPED so the UI offers a resume rather than
# treating it as a failure.
_OFFLINE_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {HostState.STOPPED, HostState.STOPPING, HostState.CRASHED, HostState.FAILED, HostState.PAUSED}
)


class MindLiveness(UpperCaseStrEnum):
    """Container liveness of a mind, surfaced to the landing page + quit prompt."""

    RUNNING = auto()
    STOPPED = auto()
    UNKNOWN = auto()


def provider_backend_supports_shutdown(backend: str) -> bool:
    """Whether a provider on ``backend`` exposes host *stop* to minds today.

    The gate behind the Stop button and the quit-time shutdown prompt. Only the
    local (docker / lima) backends qualify; Modal cannot stop host compute. Widen
    this when other providers gain host shutdown support.
    """
    return backend in _SHUTDOWN_CAPABLE_PROVIDER_BACKENDS


def provider_backend_supports_resume(backend: str) -> bool:
    """Whether a provider on ``backend`` exposes host *resume* (start) to minds.

    A superset of ``provider_backend_supports_shutdown``: it also includes Modal,
    whose hosts cannot be manually stopped but can be resumed from a snapshot. The
    gate behind the Start/Resume button and the per-mind liveness shown in the
    workspace list.
    """
    return backend in _RESUME_CAPABLE_PROVIDER_BACKENDS


def provider_backend_suppresses_recovery_auto_restart(backend: str) -> bool:
    """Whether the recovery page must wait for an explicit click before restarting.

    True for resume-only backends -- those that can be resumed from a snapshot but
    cannot be manually stopped (exactly Modal today). For these, ``mngr start``
    terminates the live sandbox and restores it from a snapshot, which is too
    destructive to fire automatically on page open: the user is required to click
    "Restart workspace" first. Shutdown-capable backends (docker / lima) are
    unaffected and keep their auto-dispatch behavior.
    """
    return provider_backend_supports_resume(backend) and not provider_backend_supports_shutdown(backend)


def resolve_agent_backend(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str | None:
    """Return the provider backend (e.g. 'docker', 'modal') for ``agent_id``, or None.

    Maps the workspace agent through its provider instance to the backend, the same
    mapping the shutdown/resume capability gates use. None when the agent, its
    provider, or the provider's backend cannot be resolved yet (pre-discovery).
    """
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is None or info.provider_name is None:
        return None
    return _build_backend_by_provider_name(backend_resolver).get(info.provider_name)


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


def _active_workspace_agent_ids_where(
    backend_resolver: BackendResolverInterface, backend_predicate: Callable[[str], bool]
) -> tuple[AgentId, ...]:
    """Return active workspace agent ids whose host backend satisfies ``backend_predicate``.

    Scopes to ``list_active_workspace_ids`` (not the full ``list_known_workspace_ids``)
    so destroyed-host workspaces -- which have no landing row -- are not tracked; the
    Start/Stop controls and quit prompt are active-workspace surfaces.
    """
    backend_by_provider_name = _build_backend_by_provider_name(backend_resolver)
    capable_agent_ids: list[AgentId] = []
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None or info.provider_name is None:
            continue
        backend = backend_by_provider_name.get(info.provider_name)
        if backend is not None and backend_predicate(backend):
            capable_agent_ids.append(agent_id)
    return tuple(capable_agent_ids)


def get_shutdown_capable_workspace_agent_ids(backend_resolver: BackendResolverInterface) -> tuple[AgentId, ...]:
    """Return active workspace agent ids whose host runs on a shutdown-capable provider.

    Gates the Stop button and the quit-time shutdown prompt.
    """
    return _active_workspace_agent_ids_where(backend_resolver, provider_backend_supports_shutdown)


def get_resume_capable_workspace_agent_ids(backend_resolver: BackendResolverInterface) -> tuple[AgentId, ...]:
    """Return active workspace agent ids whose host runs on a resume-capable provider.

    A superset of ``get_shutdown_capable_workspace_agent_ids`` (adds Modal). Gates the
    Start/Resume button and per-mind liveness shown in the workspace list.
    """
    return _active_workspace_agent_ids_where(backend_resolver, provider_backend_supports_resume)


def compute_mind_liveness_by_agent_id(backend_resolver: BackendResolverInterface) -> dict[str, MindLiveness]:
    """Return ``{agent_id_str: MindLiveness}`` for every active resume-capable mind.

    Resume-capable (not just shutdown-capable) so Modal minds -- which can be
    resumed but not manually stopped -- also get liveness in the workspace list.

    Reads each mind's host state from the resolver via ``get_host_state``, which
    already layers any short-lived optimistic override (set by a Start/Stop
    action) over the discovery snapshot -- so a just-issued action shows up here
    immediately and reconciles back to discovery on its own.
    """
    result: dict[str, MindLiveness] = {}
    for agent_id in get_resume_capable_workspace_agent_ids(backend_resolver):
        info = backend_resolver.get_agent_display_info(agent_id)
        host_state = backend_resolver.get_host_state(HostId(info.host_id)) if info is not None else None
        result[str(agent_id)] = classify_host_state(host_state)
    return result
