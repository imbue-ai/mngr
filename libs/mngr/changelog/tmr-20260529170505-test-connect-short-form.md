# mngr connect now honors connect_command

The `mngr connect` command (and its `conn` alias) now respects a custom
`connect_command` -- either from the new `--connect-command` flag or from
configuration -- instead of always running the builtin tmux/SSH attach. This
matches the existing behavior of `create` and `start`, so a `connect_command`
configured to (for example) open a new terminal window is now used by `connect`
too.

Also fixed the `mngr conn` tutorial e2e test (`test_connect_short_form`), which
was timing out on agent creation under the default 10s test timeout and could
not exercise the interactive attach in the TTY-less test runner.
