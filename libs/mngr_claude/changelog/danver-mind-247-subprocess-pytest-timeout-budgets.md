Marked `test_running_watcher_defers_the_open_trailing_inference` as a known flake so a single
failure stops turning CI red while its cause is still open (tracked as MIND-263).

The cause is measured but not fixed. The test drives the real `common_transcript.sh` watcher and
then runs a turn-end flush through a helper that allows the flush 10 seconds. When the flush has
to wait for the convert lock, the script waits its lock timeout, gives up, waits its retry delay,
and tries once more -- 60.76s end to end on the shipped 30s default, measured. The helper kills it
long before that, so contention surfaces as `subprocess.TimeoutExpired` rather than the skip the
script performs.

Also corrected the `stop_watcher` docstring, which claimed the lock directory is only cleared once
the watcher's process group is dead. Only the bash parent is waited for; the converter shares that
group and can outlive it, so the lock can be cleared while a live converter still holds it.
Escalating to the group after the parent has been reaped is not the fix -- its pid is available for
reuse at that point, so the signal could reach an unrelated process group.

Tests only -- no change to the shipped script or to any agent's behaviour.
