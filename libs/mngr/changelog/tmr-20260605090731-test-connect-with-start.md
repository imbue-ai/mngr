`mngr connect` now honors a custom `connect_command` (via the new
`--connect-command` flag or the `connect_command` config), running it instead
of the builtin tmux attach -- matching how `mngr create` and `mngr start`
already behave. A re-entrancy guard (the `MNGR_CONNECT_COMMAND_ACTIVE`
environment variable) prevents infinite recursion when a custom connect command
itself invokes `mngr connect`.
