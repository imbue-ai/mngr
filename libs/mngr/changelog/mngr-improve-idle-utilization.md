Reduced the idle CPU cost of mngr's follow-mode daemons (`mngr observe`, `mngr event --follow`), which is especially significant under gVisor where each poll/timer wakeup costs ~3x native (imbue-ai/mngr-internal#700):

- `mngr event --follow` no longer wakes 10x per second: its consume loop now blocks until an event arrives or the next housekeeping deadline (source rescan / online check) is due. Housekeeping also now runs on schedule under a continuous event stream instead of only when the queue happens to be empty.

- Local event tails (per-source follow tails and the discovery-log tail) are now woken by a directory watch (inotify on Linux, FSEvents on macOS, via the new `watchdog` dependency) with a 10-second fallback poll, instead of polling every second. Delivery latency improves to milliseconds; remote (SSH/volume) tails keep the 1-second poll. New shared utility: `imbue.mngr.utils.file_watch`.

- `mngr observe`'s per-local-agent PID watchers now block in an event-driven poll(2) over the process's pidfd plus a stop pipe on Linux (no timer at all); the psutil fallback (macOS, pre-5.3 kernels) polls at 3s instead of 1s.

- The observe activity worker's queue timeout (which only bounds child-process health checks, not activity latency) went from 2s to 5s, and the `--discovery-only` main thread no longer wakes every second just to re-check its stop flag.
