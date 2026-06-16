Fixed: the synchronous transcript flush at turn end (which keeps a WAITING-signal consumer
from outrunning the common-transcript converter) now runs on *every* turn-end path.
Previously it lived in `wait_for_stop_hook.sh`'s `run_post_completion`, which is skipped on
the no-`/proc` fast path (macOS / local agents, where the Claude-ancestor PID lookup fails)
and on the SIGTERM/SIGINT handler -- so on those paths the marker was cleared without
flushing and the converter race remained. The flush now lives in `mark_inactive`, which
every path calls before clearing the `active` marker.
