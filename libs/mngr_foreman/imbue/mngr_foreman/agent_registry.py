"""Live registry of running coding agents, fed by mngr's push-based observe stream.

Instead of polling the blocking ``list_agents`` in-process (which freezes the whole
registry the moment one provider -- a dead docker/ssh host after an IP change --
hangs), the registry spawns ``mngr observe --stream-events`` as a long-lived
subprocess and folds its stdout JSONL into the live-agent set. The observer runs
discovery *out of process*: a single unreachable provider can no longer wedge the
registry, because the observer emits each provider's result as it completes and
keeps the whole pipeline flowing.

Why ``--stream-events`` (and not ``--discovery-only``): only the full observer
carries lifecycle **state** (RUNNING / WAITING / UNKNOWN) and full ``AgentDetails``
on stdout. ``--discovery-only`` emits raw ``DiscoveredAgent`` records with no state,
which cannot drive the live-coding filter or ``get_agent`` (which must return an
``AgentDetails`` its consumers read ``.state`` / ``.type`` / ``.host`` / ``.plugin``
off). The stream carries three event types:

* ``AGENTS_FULL_STATE`` -- a complete snapshot of every known agent (real
  ``AgentDetails``). The observer *already* implements the keep-last-known model:
  when a provider is unreachable it synthesizes an ``UNKNOWN`` ``AgentDetails`` for
  that provider's last-known agents (state + host state ``UNKNOWN``, every other
  field last-known) rather than dropping them. So an unreachable provider keeps its
  agents in the snapshot, marked ``UNKNOWN``.
* ``AGENT_STATE`` -- one agent's updated state (upsert).
* ``AGENT_REMOVED`` -- an agent was destroyed (drop it).

The registry shows *live coding* agents -- a coding-harness type (claude / codex /
opencode / pi-coding) in RUNNING / WAITING -- **plus** UNKNOWN ones (a provider went
unreachable): keeping UNKNOWN agents is the keep-last-known guarantee, so killing or
unreachable docker never empties the list. Genuinely dead agents (STOPPED / DONE /
REPLACED) are filtered out, and destroyed agents leave via ``AGENT_REMOVED``.

A freshness watchdog guards the pipeline: a *provider* outage keeps the stream
FRESH (the observer folds the failure in and keeps emitting), so a stream that goes
*silent* means the pipeline itself broke (observer wedged or died). If no event
arrives within the freshness bound the watchdog bounces the subprocess (kill +
respawn); a dead subprocess is also respawned the instant its stdout closes. This
mirrors the discovery-health-watchdog plan's producer-bounce pattern, minimized to
the single process the registry owns.

Publishing is unchanged: each folded event that changes the projected snapshot
broadcasts one full snapshot to the SSE subscribers, and a change to the live-agent
*name set* fires ``on_change`` so the warm pool warms/drops connections at once.
"""

from __future__ import annotations

import json
import queue
import shutil
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_foreman.harness import transcript_strategy_for

# The coding-harness agent types foreman shows -- the transcript-driven agents a
# user actually chats with. Every other type (mngr system/worker types) is hidden.
CODING_AGENT_TYPES: Final[frozenset[str]] = frozenset({"claude", "codex", "opencode", "pi-coding"})

# Actively-running lifecycle states.
LIVE_STATES: Final[frozenset[str]] = frozenset({"RUNNING", "WAITING"})
# An agent whose provider is currently unreachable: the observer keeps its
# last-known AgentDetails but marks it UNKNOWN. We KEEP showing these (marked
# unreachable) rather than dropping them -- an unreachable provider must not empty
# the list. Every other non-live state (STOPPED / DONE / REPLACED /
# RUNNING_UNKNOWN_AGENT_TYPE) is hidden -- you cannot open, warm, or message it.
UNREACHABLE_STATE: Final[str] = "UNKNOWN"
SHOWN_STATES: Final[frozenset[str]] = LIVE_STATES | {UNREACHABLE_STATE}

# Bound each subscriber queue so a dead/slow SSE client cannot grow memory
# without limit; on overflow we drop the client (it reconnects and re-seeds).
_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 256

# --- observe stream event types (mngr observe --stream-events stdout) ---------
_EVENT_FULL_STATE: Final[str] = "AGENTS_FULL_STATE"
_EVENT_AGENT_STATE: Final[str] = "AGENT_STATE"
_EVENT_AGENT_REMOVED: Final[str] = "AGENT_REMOVED"

# --- freshness watchdog + subprocess supervision ------------------------------
# Cold start: the observer emits an initial full snapshot within a few seconds of
# launch, so no event within this window means the pipeline never came up -> bounce.
_OBSERVE_STARTUP_STALE_SECONDS: Final[float] = 35.0
# Steady state: once we've seen the first event, a healthy-but-idle fleet may only
# re-emit a full snapshot every mngr FULL_STATE_INTERVAL_SECONDS (300s upstream);
# a provider outage keeps the stream fresher than this (it triggers snapshots), so
# only a genuinely wedged pipeline stays silent this long. The margin over 300s
# keeps a healthy idle fleet from ever being falsely bounced.
_OBSERVE_STALE_SECONDS: Final[float] = 360.0
# How often the watchdog checks freshness.
_WATCHDOG_TICK_SECONDS: Final[float] = 5.0
# Pause before respawning after the stream ends, so a crash-loop can't spin hot.
_RESPAWN_BACKOFF_SECONDS: Final[float] = 2.0
# Subdir under the mngr host dir for this observer's event files + lock, kept
# separate so foreman's observer never contends the default-dir observe lock
# ("only one observer per --events-dir").
_OBSERVE_EVENTS_SUBDIR: Final[str] = "foreman-observe"


def _state_of(agent: AgentDetails) -> str:
    return str(agent.state.value if hasattr(agent.state, "value") else agent.state).upper()


def _is_shown_coding(agent: AgentDetails) -> bool:
    """True for a coding-harness agent that is running, waiting, or unreachable.

    UNKNOWN is included so an agent whose provider just went unreachable stays in
    the list (marked unreachable) instead of vanishing -- the keep-last-known
    guarantee. mngr's observer only ever produces UNKNOWN for a *previously-known*
    agent whose provider errored, so this cannot resurrect a genuinely-dead agent.
    """
    return agent.type in CODING_AGENT_TYPES and _state_of(agent) in SHOWN_STATES


def _to_agent_details(raw: object) -> AgentDetails | None:
    """Reconstruct a real ``AgentDetails`` from a serialized observe event agent."""
    if not isinstance(raw, dict):
        return None
    try:
        return AgentDetails.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - a malformed agent must not kill the stream
        logger.debug("observe: could not parse agent details (skipping): {}", e)
        return None


class AgentRegistry:
    """Thread-safe set of live coding agents, fed by the mngr observe stream."""

    def __init__(self, mngr_ctx: MngrContext) -> None:
        self._mngr_ctx = mngr_ctx
        self._lock = threading.Lock()
        # The published view: shown (live-coding + unreachable) agents keyed by id.
        self._agents: dict[str, AgentDetails] = {}
        self._subscribers: set[queue.Queue[dict]] = set()
        # Fired whenever the live-agent *name set* changes, so the warm pool warms
        # newly-appeared agents and drops departed ones without waiting out its own
        # keepalive interval.
        self._on_change: Callable[[], None] | None = None
        self._live_names: frozenset[str] = frozenset()
        self._last_broadcast: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        # Subprocess + freshness state, guarded by _proc_lock (kept off the main
        # lock so folding an event never contends with the watchdog).
        self._proc_lock = threading.Lock()
        self._proc: RunningProcess | None = None
        self._last_event_at: float = 0.0
        self._seen_event: bool = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the observe-stream consumer + watchdog (idempotent, does not block)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_observe, name="foreman-registry-observe", daemon=True)
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="foreman-registry-watchdog", daemon=True
        )
        self._thread.start()
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)

    def set_on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the live-agent name set changes."""
        self._on_change = callback

    # --- observe subprocess supervision ----------------------------------

    def _run_observe(self) -> None:
        """Spawn the observer, read its stdout line-by-line, and respawn if it ends."""
        while not self._stop.is_set():
            proc = self._spawn()
            if proc is None:
                self._stop.wait(_RESPAWN_BACKOFF_SECONDS)
                continue
            try:
                for line, is_stdout in proc.stream_stdout_and_stderr():
                    if self._stop.is_set():
                        break
                    if is_stdout:
                        self._consume_line(line)
            except Exception as e:  # noqa: BLE001 - a read error must not kill the loop
                logger.debug("mngr observe reader loop error: {}", e)
            finally:
                self._terminate(proc)
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
            if not self._stop.is_set():
                logger.info("mngr observe stream ended; respawning in {}s", _RESPAWN_BACKOFF_SECONDS)
                self._stop.wait(_RESPAWN_BACKOFF_SECONDS)

    def _spawn(self) -> RunningProcess | None:
        """Launch ``mngr observe --stream-events`` streaming JSONL events to stdout.

        Spawned through the mngr ``ConcurrencyGroup`` (the same wrapper the observer
        itself uses to run ``mngr observe --discovery-only``): the child is reaped
        with the group, its output is pumped to a queue we tail via
        ``stream_stdout_and_stderr``, and ``is_checked_by_group=False`` because we
        stop it explicitly (SIGTERM yields a non-zero exit that is not a failure).
        """
        try:
            events_dir = self._events_dir()
            proc = self._mngr_ctx.concurrency_group.run_process_in_background(
                command=[
                    self._mngr_binary(),
                    "observe",
                    "--stream-events",
                    "--headless",
                    "--events-dir",
                    str(events_dir),
                ],
                is_checked_by_group=False,
                name="foreman-observe",
            )
        except Exception as e:  # noqa: BLE001 - spawn failure is retried by the loop
            logger.error("Failed to spawn mngr observe: {}", e)
            return None
        now = time.monotonic()
        with self._proc_lock:
            self._proc = proc
            self._last_event_at = now
            self._seen_event = False
        logger.info("Spawned mngr observe --stream-events")
        return proc

    def _mngr_binary(self) -> str:
        """Resolve the mngr binary: prefer the running venv's, else PATH, else 'mngr'."""
        candidate = Path(sys.executable).with_name("mngr")
        if candidate.exists():
            return str(candidate)
        return shutil.which("mngr") or "mngr"

    def _events_dir(self) -> Path:
        """A dedicated events dir + lock for foreman's observer (isolated from others)."""
        base = self._mngr_ctx.config.default_host_dir.expanduser()
        events_dir = base / _OBSERVE_EVENTS_SUBDIR
        events_dir.mkdir(parents=True, exist_ok=True)
        return events_dir

    @staticmethod
    def _terminate(proc: RunningProcess) -> None:
        """Stop the observe subprocess (SIGTERM, escalating to kill via the group)."""
        try:
            proc.terminate(force_kill_seconds=5.0)
        except Exception as e:  # noqa: BLE001 - teardown must never raise
            logger.debug("Error terminating observe subprocess: {}", e)

    # --- freshness watchdog ----------------------------------------------

    def _watchdog(self) -> None:
        """Bounce the observer if its stream goes silent past the freshness bound."""
        while not self._stop.wait(_WATCHDOG_TICK_SECONDS):
            try:
                self._watchdog_tick()
            except Exception as e:  # noqa: BLE001 - a bad tick must not kill the watchdog
                logger.debug("registry watchdog error: {}", e)

    def _watchdog_tick(self) -> bool:
        """One freshness check; bounce the subprocess if stale. Returns True if bounced.

        A live-but-silent process past the freshness bound is a wedged pipeline (a
        provider outage keeps the stream fresh, by mngr's design). Killing it trips
        the reader loop's EOF, which respawns a fresh observer. A process that has
        already exited needs no bounce here -- the reader loop owns respawning it.
        """
        with self._proc_lock:
            proc = self._proc
            last_event_at = self._last_event_at
            seen_event = self._seen_event
        if proc is None or proc.poll() is not None:
            return False
        if not self._is_stale_at(time.monotonic(), last_event_at, seen_event):
            return False
        logger.warning(
            "mngr observe stream stale (no event for >{}s); bouncing subprocess",
            _OBSERVE_STALE_SECONDS if seen_event else _OBSERVE_STARTUP_STALE_SECONDS,
        )
        self._terminate(proc)
        return True

    @staticmethod
    def _is_stale_at(now: float, last_event_at: float, seen_event: bool) -> bool:
        """True if the stream has been silent past the applicable freshness bound.

        A wider bound applies before the first event (cold-start: the pipeline must
        prove itself) than after (steady-state: a healthy idle fleet legitimately
        emits only the periodic full snapshot).
        """
        threshold = _OBSERVE_STALE_SECONDS if seen_event else _OBSERVE_STARTUP_STALE_SECONDS
        return (now - last_event_at) > threshold

    # --- event folding ---------------------------------------------------

    def _consume_line(self, raw: str) -> None:
        """Parse one stdout JSONL line, mark freshness, and fold it into the set."""
        line = raw.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # The observer's stdout is pure JSONL, so a non-JSON line is anomalous
            # (corruption / a stray write) -- surface it rather than swallow it.
            logger.warning("observe: ignoring non-JSON stdout line: {:.120}", line)
            return
        now = time.monotonic()
        with self._proc_lock:
            self._last_event_at = now
            self._seen_event = True
        if isinstance(event, dict):
            self._apply_event(event)

    def _apply_event(self, event: dict[str, Any]) -> None:
        """Fold one observe event (full snapshot / single upsert / removal) into the set."""
        etype = event.get("type")
        if etype == _EVENT_FULL_STATE:
            shown: dict[str, AgentDetails] = {}
            for raw_agent in event.get("agents") or ():
                agent = _to_agent_details(raw_agent)
                if agent is not None and _is_shown_coding(agent):
                    shown[str(agent.id)] = agent
            with self._lock:
                self._agents = shown
            self._publish()
        elif etype == _EVENT_AGENT_STATE:
            agent = _to_agent_details(event.get("agent"))
            if agent is None:
                return
            agent_id = str(agent.id)
            with self._lock:
                if _is_shown_coding(agent):
                    self._agents[agent_id] = agent
                else:
                    self._agents.pop(agent_id, None)
            self._publish()
        elif etype == _EVENT_AGENT_REMOVED:
            agent_id = str(event.get("agent_id") or "")
            if not agent_id:
                return
            with self._lock:
                removed = self._agents.pop(agent_id, None) is not None
            if removed:
                self._publish()
        else:
            logger.trace("observe: ignoring event type {}", etype)

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
        return [_agent_to_card(a) for a in sorted(agents, key=_sort_key)]

    def get_agent(self, name_or_id: str) -> AgentDetails | None:
        with self._lock:
            for agent in self._agents.values():
                if str(agent.name) == name_or_id or str(agent.id) == name_or_id:
                    return agent
        return None

    def subscribe(self) -> Iterator[dict]:
        """Yield an initial snapshot then live snapshots until the client leaves."""
        q: queue.Queue[dict] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.add(q)
        try:
            yield {"type": "snapshot", "agents": self.snapshot()}
            while True:
                yield q.get()
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


def _agent_to_card(agent: AgentDetails) -> dict:
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
        "activity_time": activity.isoformat() if activity else None,
        # Chat (live transcript + composer) is available for any type foreman has a
        # transcript strategy for (claude, codex, opencode, pi-coding); others are
        # terminal-only.
        "supports_chat": transcript_strategy_for(agent.type) is not None,
    }
