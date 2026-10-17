The e2e tutorial tests no longer depend on GNU `timeout`, which macOS does not ship.

Commands that stream forever (`mngr event --follow`, `mngr observe`) were bounded with `timeout N`. On a BSD userland that is `127: command not found`, so the test never ran `mngr` at all -- and where the call ended in `|| true`, the 127 was swallowed and the assertion failed with something unrelated instead. Six release tests failed this way on every macOS shard.

They now use `time_bounded()`, the perl form the style guide's portable-shell section prescribes, which is also already used in product code (`workspace_version.py`). `exec` keeps the command's own pid and exit status, so the assertions that prove a stream stayed open still work.

Those assertions move off the literal 124. GNU `timeout` exits 124 however you observe it, because it exits normally; the perl form lets the alarm kill the command, and a signal death reads differently depending on whether a shell survives to report it. The bound is parenthesised so one always does, which makes the value 128 + the signal on both platforms -- bare, macOS `sh` execs itself away and the caller reaps the negated signal instead.
