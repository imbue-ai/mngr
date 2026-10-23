The codex `common_transcript.sh` tests now stop their background poll daemon with `running_in_own_process_group` from `imbue.mngr.utils.testing` (MIND-377), the same stop the claude watcher tests use.

The old stop sent SIGTERM to the daemon's process group and returned as soon as the daemon's bash process had exited. Two kinds of process could outlive it: a child being forked at the moment of the signal, which gVisor (the kernel in CI's Modal sandboxes) lets join the group after the signal has gone out, and any other process left in the group once bash was gone. A stand-in that rejoins the group after the signal stayed alive 3 times out of 3 against the old stop. The new stop sends SIGKILL to the group until every process in it has exited, and only then reaps bash.

`_start_watcher` and `_stop_watcher` are replaced by a `_running_watcher()` context manager, so a test cannot start the daemon without stopping it. The daemon's stderr now goes to pytest's captured output instead of `/dev/null`.

Tests only. No change to the shipped script or to codex agents' behaviour.
