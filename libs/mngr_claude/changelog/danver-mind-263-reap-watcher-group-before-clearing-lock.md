Hardened `stop_watcher`, the helper in the `common_transcript.sh` tests that shuts down a
background watcher started by a test.

The watcher (`common_transcript.sh`) converts a Claude transcript by running a Python converter
as a child process, and the tests start it in its own process group so one signal reaches both.
A lock directory, the convert lock, stops two conversions from running at once. `stop_watcher`
used to send SIGTERM to the group, wait for the watcher's bash process, and then delete the lock
directory. It now sends SIGKILL instead. SIGKILL cannot be caught or ignored, so deleting the lock
directory afterwards no longer depends on nothing in the group handling SIGTERM. Nothing does
today, so this is a hardening rather than a bug fix: with the scripts as they are, the old
sequence also killed everything in the group.

The kill is sent before the wait. Waiting reaps the bash process and frees its pid for reuse, and
that pid is also the process group id, so a kill sent after the wait could hit an unrelated group.

Added a test that stops a stand-in watcher whose child ignores SIGTERM and checks that nothing in
the group is left running once `stop_watcher` returns.

Tests only. No change to the shipped script or to any agent's behaviour.
