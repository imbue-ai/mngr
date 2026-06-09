"""Derives container liveness of *local* minds (docker / lima hosts) for the
landing-page Start/Stop controls and the quit-time shutdown prompt.

The global discovery snapshot already carries each host's lifecycle state (it is
written on every poll by the single ``mngr observe --discovery-only`` and folded
into :class:`MngrCliBackendResolver` as ``host_state_by_host_id``). That is the
same ``mngr list`` host state a dedicated liveness poll would read, so rather than
run a second poll, this module reads host state straight off the resolver.

``--discovery-only`` drops only the per-*agent* lifecycle/activity streams (the
agent process's own state); it keeps host/container state, which is exactly what
"is this container up?" needs.

This module owns:

- ``classify_host_state`` -- maps a discovery ``HostState`` to the coarse
  RUNNING / STOPPED / UNKNOWN the UI shows.
- ``get_local_workspace_agent_ids`` -- classifies which active workspaces are
  local by their provider backend.
- ``LocalMindStateProvider`` -- computes each local mind's state from the
  resolver, applying a short-lived *optimistic override* set by a user-initiated
  Start/Stop so the UI flips at once instead of waiting for the next discovery
  snapshot. The override is dropped as soon as discovery agrees with it (or a TTL
  elapses), so discovery stays authoritative.
"""

import threading
import time
from collections.abc import Callable
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
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

# How long an optimistic override is trusted before discovery is believed
# instead. A Start/Stop's ``mngr`` command has already returned by the time the
# override is set, so the next discovery snapshot (~10s) normally confirms it
# well within this window; the TTL only bounds how long a *stuck* discovery
# (e.g. a provider erroring) can keep showing a stale optimistic state.
_OVERRIDE_TTL_SECONDS: Final[float] = 90.0


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


class _Override(FrozenModel):
    """A short-lived optimistic state set by a user-initiated Start/Stop."""

    state: LocalMindState
    set_at_monotonic: float


class LocalMindStateProvider(MutableModel):
    """Computes each local mind's container liveness from the discovery resolver.

    Discovery host state is authoritative. A user-initiated Start/Stop sets an
    optimistic override (via :meth:`set_override`) so the landing page and quit
    prompt reflect the new state immediately; the override wins until discovery
    agrees with it or its TTL elapses, at which point it is dropped.

    On-change callbacks (mirroring the resolver's) let the chrome SSE wake the
    instant an override is set, so the UI does not wait for the next discovery
    snapshot to flip.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _override_by_agent_id: dict[str, _Override] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def add_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback fired whenever an optimistic override is set or cleared.

        Callbacks run on whichever thread caused the change (a Start/Stop worker);
        keep them fast and non-blocking -- they should just signal an event.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def remove_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Unregister a previously registered change callback (no-op if absent)."""
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError) as e:
                logger.warning("LocalMindStateProvider on-change callback failed: {}", e)

    def set_override(self, agent_id: AgentId, state: LocalMindState) -> None:
        """Optimistically set a mind's state after a user Start/Stop; fires on-change."""
        with self._lock:
            self._override_by_agent_id[str(agent_id)] = _Override(state=state, set_at_monotonic=time.monotonic())
        self._fire_on_change()

    def clear_override(self, agent_id: AgentId) -> None:
        """Drop any optimistic override for ``agent_id`` so discovery state shows; fires on-change.

        Used when a Start/Stop did not complete: the container never reached the
        target state, so the optimistic guess must give way to discovery at once.
        """
        with self._lock:
            existed = self._override_by_agent_id.pop(str(agent_id), None) is not None
        if existed:
            self._fire_on_change()

    def compute_state_by_agent_id(self, backend_resolver: BackendResolverInterface) -> dict[str, LocalMindState]:
        """Return ``{agent_id_str: LocalMindState}`` for every active local mind.

        Reads discovery host state per mind, then applies any fresh optimistic
        override. Overrides that discovery now agrees with (or that have aged past
        the TTL) are dropped, as are overrides for minds no longer active/local, so
        stale optimistic state never lingers.
        """
        local_agent_ids = get_local_workspace_agent_ids(backend_resolver)
        discovery_state_by_agent_id = {
            str(agent_id): self._discovery_state(backend_resolver, agent_id) for agent_id in local_agent_ids
        }
        now = time.monotonic()
        result: dict[str, LocalMindState] = {}
        with self._lock:
            # Drop overrides for minds that left the active-local set (destroyed, etc.).
            for aid_str in tuple(self._override_by_agent_id):
                if aid_str not in discovery_state_by_agent_id:
                    del self._override_by_agent_id[aid_str]
            for aid_str, discovery_state in discovery_state_by_agent_id.items():
                override = self._override_by_agent_id.get(aid_str)
                if (
                    override is not None
                    and discovery_state != override.state
                    and (now - override.set_at_monotonic) <= _OVERRIDE_TTL_SECONDS
                ):
                    result[aid_str] = override.state
                else:
                    self._override_by_agent_id.pop(aid_str, None)
                    result[aid_str] = discovery_state
        return result

    def _discovery_state(self, backend_resolver: BackendResolverInterface, agent_id: AgentId) -> LocalMindState:
        """Resolve one mind's liveness from discovery: agent -> host -> host state."""
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None:
            return LocalMindState.UNKNOWN
        host_state = backend_resolver.get_host_state(HostId(info.host_id))
        return classify_host_state(host_state)
