`mngr config` and `mngr plugin` now require a subcommand, matching every other command group. Previously each was `invoke_without_command`, so running it bare printed help and exited 0. Now a bare `mngr config` / `mngr plugin` prints the standard usage/help listing and exits with the usual usage-error code (2), exactly like `mngr snapshot` and friends. The full man-page-style help is still available via `mngr config --help` / `mngr plugin --help`.

Also dropped a vestigial group-level `--scope` option on `mngr config` that only the removed bare-invocation path ever read (it was silently ignored when passed before a subcommand, e.g. `mngr config --scope user list`). Per-subcommand `--scope` is unaffected: `mngr config list --scope user`, `mngr config set ... --scope user`, etc. all work as before.

Regenerated the `config`, `plugin`, and `usage` command reference docs. `config`/`plugin` now render `COMMAND` (required) instead of `[COMMAND]`; the `usage` doc reflects a rendering change in the newer `click` release pulled in by the workspace dependency refresh (`usage` remains runnable bare -- it prints the usage snapshot).

Internal type-annotation touch-up in a create test to satisfy the updated type checker.
