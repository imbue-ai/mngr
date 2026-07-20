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

FAST_POLL_SECONDS = 0.3
STEADY_POLL_SECONDS = 1.0
IDLE_POLL_SECONDS = 4.0
# How long a send (or a RUNNING observation) keeps an agent in fast mode. Long
# enough to span a send -> agent-starts-working -> first-tokens gap and to keep a
# brief fast tail after work ends so the final message arrives promptly.
FAST_WINDOW_SECONDS = 15.0

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
    while any number of transcript SSE loops (other threads) read the deadline.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fast_until: dict[str, float] = {}

    def poke(self, agent_name: str, now: float | None = None) -> None:
        """Enter (or extend) fast mode for ``agent_name`` for one fast window."""
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
