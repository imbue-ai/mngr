"""Tracks container liveness of *local* minds (docker / lima hosts) for the
landing-page Start/Stop controls and the quit-time shutdown prompt.

The global discovery snapshot already carries each host's lifecycle state, but
it only re-polls every few minutes because it fans out across every provider
(including remote ones that mean real network calls). That cadence is too slow
to drive a responsive "is this container up?" badge, so minds runs a separate,
cheap poll scoped to *local* providers only (see the poll loop in ``app.py``).

This module owns the pieces that poll feeds:

- ``LocalMindLivenessTracker`` -- the per-agent liveness state, with on-change
  callbacks the chrome SSE subscribes to (mirrors ``SystemInterfaceHealthTracker``).
- ``get_local_workspace_agent_ids`` -- classifies which workspaces are local by
  their provider backend.
- the ``mngr list`` argv builder + output parser the poll loop uses to read each
  local host's container state without ever starting a stopped container.

A user-initiated Stop / Start sets the tracker state directly (so the UI flips
at once) and also pokes the poll to re-read; an externally-stopped container is
reflected by the next poll tick.
"""

import json
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.mngr.primitives import AgentId

# Provider backends whose hosts run on the user's own machine and so consume
# local resources while alive. Only minds on these backends are surfaced for the
# quit-time shutdown prompt and the landing-page Start/Stop controls; remote
# minds (Modal, OVH, ...) have no "stop to free local resources" concept.
LOCAL_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset({"docker", "lima"})

# host.state values (from ``mngr list``) that mean the container is not running.
# Mirrors the offline set the recovery-diagnostics probe uses.
_OFFLINE_HOST_STATES: Final[frozenset[str]] = frozenset({"STOPPED", "STOPPING", "CRASHED", "FAILED"})
_RUNNING_HOST_STATE: Final[str] = "RUNNING"


class LocalMindState(UpperCaseStrEnum):
    """Container liveness of a local mind, surfaced to the landing page + quit prompt."""

    RUNNING = auto()
    STOPPED = auto()
    UNKNOWN = auto()


OnLivenessChangeCallback = Callable[[AgentId, LocalMindState], None]


class LocalMindLivenessTracker(MutableModel):
    """Per-local-mind container-liveness state, fed by the liveness poll and Start/Stop actions."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _state_by_agent_id: dict[str, LocalMindState] = PrivateAttr(default_factory=dict)
    _on_change_callbacks: list[OnLivenessChangeCallback] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def add_on_change_callback(self, callback: OnLivenessChangeCallback) -> None:
        """Register a callback fired whenever a local mind's liveness changes.

        Callbacks receive ``(agent_id, new_state)`` and run on whichever thread
        caused the transition (poll loop or Start/Stop worker). Keep them fast
        and non-blocking.
        """
        with self._lock:
            self._on_change_callbacks.append(callback)

    def remove_on_change_callback(self, callback: OnLivenessChangeCallback) -> None:
        """Unregister a previously registered change callback (no-op if absent)."""
        with self._lock:
            try:
                self._on_change_callbacks.remove(callback)
            except ValueError:
                pass

    def set_state(self, agent_id: AgentId, state: LocalMindState) -> None:
        """Set a single mind's liveness, firing on-change only if it actually changed.

        Used by the Start/Stop workers to reflect a user-initiated transition
        immediately, ahead of the next poll tick.
        """
        aid_str = str(agent_id)
        with self._lock:
            if self._state_by_agent_id.get(aid_str) == state:
                return
            self._state_by_agent_id[aid_str] = state
        self._fire_on_change(agent_id, state)

    def apply_poll_results(self, state_by_agent_id: Mapping[str, LocalMindState]) -> None:
        """Replace tracked liveness with a fresh poll snapshot, firing on-change for each change.

        ``state_by_agent_id`` is the authoritative set of currently-known local
        minds; agents absent from it (e.g. destroyed) are dropped from tracking.
        """
        changed: list[tuple[AgentId, LocalMindState]] = []
        with self._lock:
            for aid_str, state in state_by_agent_id.items():
                if self._state_by_agent_id.get(aid_str) != state:
                    changed.append((AgentId(aid_str), state))
            self._state_by_agent_id = dict(state_by_agent_id)
        for agent_id, state in changed:
            self._fire_on_change(agent_id, state)

    def get_state(self, agent_id: AgentId) -> LocalMindState:
        """Return the current liveness for ``agent_id`` (UNKNOWN if untracked)."""
        with self._lock:
            return self._state_by_agent_id.get(str(agent_id), LocalMindState.UNKNOWN)

    def snapshot_all(self) -> dict[AgentId, LocalMindState]:
        """Return a copy of every tracked local mind's liveness."""
        with self._lock:
            return {AgentId(aid): state for aid, state in self._state_by_agent_id.items()}

    def _fire_on_change(self, agent_id: AgentId, new_state: LocalMindState) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id, new_state)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("LocalMindLivenessTracker on-change callback failed for {}: {}", agent_id, e)


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


def get_local_provider_names(backend_resolver: BackendResolverInterface) -> tuple[str, ...]:
    """Return the names of provider instances whose backend is local (docker / lima)."""
    backend_by_provider_name = _build_backend_by_provider_name(backend_resolver)
    return tuple(
        provider_name
        for provider_name, backend in backend_by_provider_name.items()
        if backend in LOCAL_PROVIDER_BACKENDS
    )


def build_local_host_state_list_argv(
    mngr_binary: str,
    local_provider_names: Sequence[str],
    local_agent_ids: Sequence[AgentId],
) -> list[str]:
    """Build argv for a read-only ``mngr list`` scoped to the local providers' hosts.

    ``--provider`` restricts discovery fan-out to the local backends (so the poll
    never touches remote providers), the ``id == ...`` include narrows the payload
    to the workspaces we care about, and ``--on-error continue`` keeps one bad host
    from blanking the whole listing. ``mngr list`` is a pure read -- it never
    starts a stopped container.
    """
    argv = [mngr_binary, "list", "--format", "json", "--quiet", "--on-error", "continue"]
    for provider_name in local_provider_names:
        argv += ["--provider", provider_name]
    if local_agent_ids:
        include = " || ".join(f'id == "{agent_id}"' for agent_id in local_agent_ids)
        argv += ["--include", include]
    return argv


def parse_local_mind_states_from_list_json(
    list_json: str,
    local_agent_ids: Sequence[AgentId],
) -> dict[str, LocalMindState]:
    """Map each local agent id to its container liveness from ``mngr list --format json`` output.

    Agents missing from the listing (their host could not be enumerated) map to
    UNKNOWN rather than being dropped, so the UI can distinguish "we couldn't
    tell" from "confirmed stopped".
    """
    host_state_by_agent_id = _parse_host_state_by_agent_id(list_json)
    return {
        str(agent_id): _classify_host_state(host_state_by_agent_id.get(str(agent_id))) for agent_id in local_agent_ids
    }


def _parse_host_state_by_agent_id(list_json: str) -> dict[str, str]:
    """Pull ``host.state`` for every agent row in ``mngr list --format json`` output."""
    try:
        agents = json.loads(list_json).get("agents", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Could not parse `mngr list` output for local-mind liveness: {}", exc)
        return {}
    host_state_by_agent_id: dict[str, str] = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("id")
        host = agent.get("host")
        if not isinstance(agent_id, str) or not isinstance(host, dict):
            continue
        state = host.get("state")
        if isinstance(state, str):
            host_state_by_agent_id[agent_id] = state
    return host_state_by_agent_id


def _classify_host_state(host_state: str | None) -> LocalMindState:
    """Classify a raw ``host.state`` string into a coarse liveness state."""
    if host_state is None:
        return LocalMindState.UNKNOWN
    upper = host_state.upper()
    if upper == _RUNNING_HOST_STATE:
        return LocalMindState.RUNNING
    if upper in _OFFLINE_HOST_STATES:
        return LocalMindState.STOPPED
    return LocalMindState.UNKNOWN
