"""Live registry of running coding agents, kept fresh by a discovery poll loop.

Every ``POLL_INTERVAL_SECONDS`` a background thread calls ``list_agents``
in-process -- the same discovery ``mngr list`` runs, no subprocess -- keeps only
the *live coding* agents (a coding-harness type in a RUNNING or WAITING state),
and publishes that set to the SSE subscribers and the warm pool. Dead, stopped,
done, and non-coding agents never appear.

There are no per-agent deltas: each poll whose projected result changed
broadcasts one full snapshot, which the browser re-renders. The loop is
sequential (list, then wait the interval, then list again), so a slow list just
delays the next pass and never overlaps itself.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from typing import Final

from loguru import logger

from imbue.mngr.api.list import list_agents
from imbue.mngr.api.providers import get_all_provider_instances
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.utils.thread_cleanup import cleanup_thread_local_resources
from imbue.mngr_foreman.harness import transcript_strategy_for

# How often the discovery loop re-lists agents (the interval *between* passes).
# Discovery only tracks *membership* (which agents exist), which changes on the order
# of minutes; per-agent live status (WORKING / NEEDS INPUT / READY) is computed
# on-demand from the tmux pane, not here. So a slow cadence is plenty fresh and keeps
# the box quiet -- a 3s cadence re-ran docker ps + per-container probes 20x/minute for
# no benefit. A newly-appeared agent still warms within one interval via on_change.
POLL_INTERVAL_SECONDS: Final[float] = 10.0
# How often to re-enumerate the configured providers (cheap, config-based).
_PROVIDER_REFRESH_SECONDS: Final[float] = 30.0

# The coding-harness agent types foreman shows -- the transcript-driven agents a
# user actually chats with. Every other type (mngr system/worker types) is hidden.
CODING_AGENT_TYPES: Final[frozenset[str]] = frozenset({"claude", "codex", "opencode", "pi-coding"})

# Only actively-running agents are shown. Every other lifecycle state
# (STOPPED / DONE / REPLACED / UNKNOWN / RUNNING_UNKNOWN_AGENT_TYPE and any
# tombstone) is hidden -- you cannot open, warm, or message a dead agent.
LIVE_STATES: Final[frozenset[str]] = frozenset({"RUNNING", "WAITING"})

# Bound each subscriber queue so a dead/slow SSE client cannot grow memory
# without limit; on overflow we drop the client (it reconnects and re-seeds).
_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 256
# How often an idle subscriber stream yields a heartbeat. Bounds the blocking
# ``queue.get`` so a client that dropped mid-idle-period (membership rarely changes,
# and SSE is write-only so a dead socket is invisible until we write) is detected
# within one interval instead of leaking its handler thread + fd forever.
_SUBSCRIBER_HEARTBEAT_SECONDS: Final[float] = 5.0


def _state_of(agent: AgentDetails) -> str:
    return str(agent.state.value if hasattr(agent.state, "value") else agent.state).upper()


def _is_live_coding(agent: AgentDetails) -> bool:
    """True for a coding-harness agent that is currently running or waiting."""
    return agent.type in CODING_AGENT_TYPES and _state_of(agent) in LIVE_STATES


class AgentRegistry:
    """Thread-safe set of live coding agents, refreshed by a discovery poll loop."""

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self._mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        self._agents: dict[str, AgentDetails] = {}
        # Agents keyed by the provider that reported them, so each provider's set can
        # be refreshed independently -- a hung provider only staleness-affects its
        # own agents. ``_agents`` is the flattened, published view.
        self._agents_by_provider: dict[str, dict[str, AgentDetails]] = {}
        self._provider_inflight: dict[str, bool] = {}
        self._provider_names: tuple[str, ...] = ()
        self._provider_names_at: float = 0.0
        self._subscribers: set[queue.Queue[dict]] = set()
        # Fired whenever the live-agent *name set* changes, so the warm pool warms
        # newly-appeared agents and drops departed ones without waiting out its own
        # keepalive interval.
        self._on_change: Callable[[], None] | None = None
        # Tags each card with whether the user parked it (foreman-local state, injected
        # by the app). Default: nothing parked.
        self._is_backburner: Callable[[str], bool] = lambda _id: False
        self._live_names: frozenset[str] = frozenset()
        self._last_broadcast: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the background discovery poll loop (idempotent, does not block)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._poll_loop, name="foreman-registry-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the live-agent name set changes."""
        self._on_change = callback

    def set_backburner_predicate(self, is_backburner: Callable[[str], bool]) -> None:
        """Inject the "is this agent id parked?" test used to tag each card."""
        self._is_backburner = is_backburner

    def republish(self) -> None:
        """Broadcast a fresh snapshot now (e.g. right after a backburner toggle) so
        every open home page reflects the change immediately, not on the next poll."""
        self._publish()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            # A single bad pass (e.g. Thread.start() raising under OS resource pressure)
            # must never kill the only discovery loop -- the whole agent list would then
            # freeze until a process restart. Mirror the warm pool's _safe_tick guard.
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001 - keep polling; one bad pass is not fatal
                logger.warning("Discovery poll pass failed (continuing): {}", e)
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _provider_names_cached(self) -> tuple[str, ...]:
        """Configured provider names, re-enumerated occasionally (cheap, config-based)."""
        now = time.monotonic()
        if not self._provider_names or now - self._provider_names_at > _PROVIDER_REFRESH_SECONDS:
            try:
                self._provider_names = tuple(p.name for p in get_all_provider_instances(self._mngr_ctx))
                self._provider_names_at = now
            except Exception as e:  # noqa: BLE001 - keep the last list if enumeration fails
                logger.debug("Could not enumerate providers (keeping last): {}", e)
        return self._provider_names

    def _poll_once(self) -> None:
        """Kick off an independent discovery for each provider that isn't mid-poll.

        Each provider is listed in its OWN thread: a provider whose call blocks
        (e.g. an unreachable host after an IP change -- mngr's discovery does not
        self-abort on a hung provider) can no longer freeze the whole registry. A
        per-provider in-flight guard stops a hung provider from piling up threads;
        its agents simply stay last-known until it recovers, while every other
        provider (notably ``local``) keeps refreshing every interval.
        """
        for pname in self._provider_names_cached():
            with self._lock:
                if self._provider_inflight.get(pname):
                    continue
                self._provider_inflight[pname] = True
            try:
                threading.Thread(
                    target=self._poll_provider, args=(pname,), name=f"foreman-registry-{pname}", daemon=True
                ).start()
            except Exception as e:  # noqa: BLE001 - a failed spawn must not strand the provider
                # The thread never ran, so its finally-clause never resets the guard;
                # clear it here or this provider would stay "in flight" forever.
                with self._lock:
                    self._provider_inflight[pname] = False
                logger.warning("Could not start discovery thread for provider {}: {}", pname, e)

    def _poll_provider(self, pname: str) -> None:
        """List one provider's agents and republish; never blocks the other providers."""
        try:
            self._poll_provider_inner(pname)
        finally:
            # This runs in a throwaway per-poll thread; discovery touches pyinfra,
            # which leaves a thread-local gevent Hub whose wakeup pipe would leak
            # when the thread exits. Destroy it (no-op if none was created).
            cleanup_thread_local_resources()

    def _poll_provider_inner(self, pname: str) -> None:
        try:
            result = list_agents(
                self._mngr_ctx,
                is_streaming=False,
                provider_names=(pname,),
                error_behavior=ErrorBehavior.CONTINUE,
                # Reuse provider caches (SSH connections, listing snapshots) across polls
                # instead of a full re-probe every pass -- that full re-probe was the CPU
                # hog. Discovery still runs each poll (finds new/dropped agents); a stale
                # connection self-heals via the provider's on_connection_error eviction.
                # ponytail: no periodic hard reset; add one if IP-change staleness ever bites.
                reset_caches=False,
            )
            live = {str(a.id): a for a in result.agents if _is_live_coding(a)}
        except Exception as e:  # noqa: BLE001 - a bad provider must not kill its slot
            logger.debug("Discovery for provider {} failed (keeping last set): {}", pname, e)
            with self._lock:
                self._provider_inflight[pname] = False
            return
        with self._lock:
            self._agents_by_provider[pname] = live
            merged: dict[str, AgentDetails] = {}
            for prov in self._agents_by_provider.values():
                merged.update(prov)
            self._agents = merged
            self._provider_inflight[pname] = False
        self._publish()

    def _publish(self) -> None:
        """Broadcast a fresh snapshot if it changed, and wake the pool if membership did."""
        cards = self.snapshot()  # acquires the lock internally
        payload = json.dumps(cards, sort_keys=True)
        with self._lock:
            names = frozenset(str(a.name) for a in self._agents.values())
            changed_payload = payload != self._last_broadcast
            if changed_payload:
                self._last_broadcast = payload
            changed_names = names != self._live_names
            if changed_names:
                self._live_names = names
        if changed_payload:
            self._broadcast({"type": "snapshot", "agents": cards})
        if changed_names and self._on_change is not None:
            self._on_change()

    # --- read + subscribe ------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            agents = list(self._agents.values())
        return [_agent_to_card(a, self._is_backburner) for a in sorted(agents, key=_sort_key)]

    def get_agent(self, name_or_id: str) -> AgentDetails | None:
        with self._lock:
            for agent in self._agents.values():
                if str(agent.name) == name_or_id or str(agent.id) == name_or_id:
                    return agent
        return None

    def subscribe(self) -> Iterator[dict]:
        """Yield an initial snapshot, then live snapshots, with periodic heartbeats.

        The ``queue.get`` is bounded by a heartbeat interval so a client that dropped
        its connection during an idle stretch (no membership change to push, no OS-level
        SSE probe) is noticed when the next yield fails, instead of leaking a handler
        thread + fd blocked forever -- and a subscriber whose queue overflowed and was
        discarded by ``_broadcast`` still gets woken to exit rather than hanging on a
        queue nothing will ever fill again.
        """
        q: queue.Queue[dict] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.add(q)
        try:
            yield {"type": "snapshot", "agents": self.snapshot()}
            while True:
                try:
                    yield q.get(timeout=_SUBSCRIBER_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield {"type": "heartbeat"}
        finally:
            with self._lock:
                self._subscribers.discard(q)

    def _broadcast(self, message: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except queue.Full:
                logger.debug("Dropping a slow foreman list-stream subscriber (queue full)")
                with self._lock:
                    self._subscribers.discard(q)


def _sort_key(agent: AgentDetails) -> tuple:
    # Most-recently-active first; agents with no activity sort last.
    activity = agent.agent_activity_time or agent.user_activity_time or agent.create_time
    return (0 if activity else 1, -(activity.timestamp() if activity else 0.0), str(agent.name))


def _agent_to_card(agent: AgentDetails, is_backburner: Callable[[str], bool]) -> dict:
    """Project an ``AgentDetails`` down to the fields the list UI needs."""
    activity = agent.agent_activity_time or agent.user_activity_time
    return {
        "id": str(agent.id),
        "name": str(agent.name),
        "type": agent.type,
        "state": _state_of(agent),
        "host_name": agent.host.name,
        "provider": str(agent.host.provider_name),
        "labels": dict(agent.labels),
        # Foreman-local parked flag (not an mngr label) -- the home page files parked
        # agents under the Backburner section.
        "backburner": is_backburner(str(agent.id)),
        "activity_time": activity.isoformat() if activity else None,
        # Chat (live transcript + composer) is available for any type foreman has a
        # transcript strategy for (claude, codex, opencode, pi-coding); others are
        # terminal-only.
        "supports_chat": transcript_strategy_for(agent.type) is not None,
    }
