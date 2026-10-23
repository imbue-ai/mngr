Fixed a CI flake in the `common_transcript.sh` tests (MIND-377): stopping a test's background watcher could leave one of its child processes running.

The tests start the watcher (`common_transcript.sh`) in its own process group and stop it by sending SIGKILL to the whole group. CI runs tests in Modal sandboxes, which use gVisor (a user-space Linux kernel), and gVisor lets a child that is being forked at the moment of the kill join the group after the signal has gone out. That child never receives the signal. On Linux the kernel restarts the fork to prevent this. Replaying the test's stop 5000 times in a Modal sandbox left a survivor 3 times.

The stop now sends SIGKILL to the group repeatedly until every process in it has exited. It knows they have exited because each one inherits the write end of a pipe that nothing writes to, and that pipe reaches end-of-file only once the last of them has closed it. The watcher's bash process is reaped only after that, because its pid is the group id and reaping it earlier would let an unrelated group reuse the number. The convert lock is deleted after the group is empty, as before. The stop lives in `running_in_own_process_group` in `imbue.mngr.utils.testing`, which the codex watcher tests now use too.

`ScriptRunner.start_watcher` and `ScriptRunner.stop_watcher` are replaced by a `ScriptRunner.running_watcher()` context manager, so a test cannot start a watcher without stopping it. The watcher's stderr now goes to pytest's captured output instead of `/dev/null`.

Added a test that pins the fix without relying on gVisor timing: a stand-in process leaves the group before the kill, rejoins it afterwards, and the stop must still kill it. It fails against the old single-kill stop on every run. `test_stopping_a_watcher_kills_the_converter_that_outlived_it` is no longer marked `@pytest.mark.flaky`.

Tests only. No change to the shipped script or to any agent's behaviour.
