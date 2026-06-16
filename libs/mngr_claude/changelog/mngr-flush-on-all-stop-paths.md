Fixed: the synchronous transcript flush at turn end (which keeps a WAITING-signal consumer
from outrunning the common-transcript converter) now runs on *every* turn-end path.
Previously it lived in `wait_for_stop_hook.sh`'s `run_post_completion`, which is skipped on
the no-`/proc` fast path (macOS / local agents, where the Claude-ancestor PID lookup fails)
and on the SIGTERM/SIGINT handler -- so on those paths the marker was cleared without
flushing and the converter race remained. The flush now lives in `mark_inactive`, which
every path calls before clearing the `active` marker.

The flush's lock-acquire wait -- its only potentially-slow step -- is now bounded by an
explicit per-call timeout, so the SIGTERM/SIGINT handler can't block on it: interrupts cap
the wait at 2s (`HOOK_FLUSH_LOCK_TIMEOUT_SIGNAL`) while normal turn-end paths use 30s
(`HOOK_FLUSH_LOCK_TIMEOUT`). The bound is a portable `MNGR_CONVERT_LOCK_TIMEOUT` handed to
each converter pass rather than a `timeout(1)` wrapper, which macOS lacks.
