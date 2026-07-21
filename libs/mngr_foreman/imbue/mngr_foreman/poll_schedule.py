"""Adaptive transcript-poll cadence: poll fast when something is likely to change.

The transcript SSE loop used to sleep a fixed 1.5s between polls, so a reply
could sit up to that long before reaching the browser on top of mngr's ~1s
upstream mirror delay. Instead we vary the interval per agent:

* **fast** (~0.3s) right after a message is sent to the agent, and while the
  agent is ``RUNNING`` (actively working) -- the windows where new events land;
* **idle** (~4s) when the agent is ``WAITING`` with no recent send -- nothing is
  changing, so don't burn polls (or the warm SSH connection);
* **steady** (~1s) otherwise.

Stat-before-read (see ``TranscriptTailer``) makes a fast poll nearly free when
the file hasn't grown, so fast mode costs a cheap ``stat`` on the warm
connection, not a full read.
"""

from __future__ import annotations

import threading
import time

FAST_POLL_SECONDS = 0.2
STEADY_POLL_SECONDS = 1.0
IDLE_POLL_SECONDS = 4.0
# How long a send (or a RUNNING observation) keeps an agent in fast mode. Long
# enough to span a send -> agent-starts-working -> first-tokens gap and to keep a
# brief fast tail after work ends so the final message arrives promptly.
FAST_WINDOW_SECONDS = 20.0

_WAITING_STATE = "WAITING"


def interval_for(now: float, fast_until: float, state: str | None) -> float:
    """Pure cadence rule: fast inside the window, idle when waiting, else steady."""
    if now < fast_until:
        return FAST_POLL_SECONDS
    if state is not None and state.upper() == _WAITING_STATE:
        return IDLE_POLL_SECONDS
    return STEADY_POLL_SECONDS


class ActivityTracker:
    """Per-agent fast-mode deadlines, shared between the send route and SSE loops.

    Thread-safe: the send endpoint (one thread) pokes an agent into fast mode
    while any number of transcript SSE loops (other threads) wait on the deadline.
    A poke also *wakes* any loop currently sleeping for that agent, so a send
    lands promptly even if the loop was mid idle-interval -- that early wake is
    the difference between "next poll in up to 4s" and "next poll now".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fast_until: dict[str, float] = {}
        self._wakers: dict[str, threading.Event] = {}

    def _waker(self, agent_name: str) -> threading.Event:
        with self._lock:
            waker = self._wakers.get(agent_name)
            if waker is None:
                waker = threading.Event()
                self._wakers[agent_name] = waker
            return waker

    def poke(self, agent_name: str, now: float | None = None) -> None:
        """Enter fast mode for ``agent_name`` and wake any loop sleeping on it."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._fast_until[agent_name] = moment + FAST_WINDOW_SECONDS
        self._waker(agent_name).set()

    def mark_running(self, agent_name: str, now: float | None = None) -> None:
        """Extend fast mode because the agent is working; does not wake sleepers.

        Called by the loop itself right before it sleeps, so (unlike ``poke``) it
        must not wake the very loop that is about to wait.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._fast_until[agent_name] = moment + FAST_WINDOW_SECONDS

    def fast_until(self, agent_name: str) -> float:
        with self._lock:
            return self._fast_until.get(agent_name, 0.0)

    def next_interval(self, agent_name: str, state: str | None, now: float | None = None) -> float:
        """Interval before the next poll for ``agent_name`` given its live state."""
        moment = time.monotonic() if now is None else now
        return interval_for(moment, self.fast_until(agent_name), state)

    def wait_for_next_poll(self, agent_name: str, state: str | None) -> None:
        """Block until the next poll is due, or return early if a poke wakes us.

        Clears *after* waiting (not before), so a poke that arrives while the
        caller is busy polling survives to shorten the next wait rather than being
        wiped unseen.
        """
        waker = self._waker(agent_name)
        waker.wait(self.next_interval(agent_name, state))
        waker.clear()
