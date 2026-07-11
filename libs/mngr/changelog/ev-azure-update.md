`mngr config` and `mngr plugin` now require a subcommand, matching every other command group. Previously each was `invoke_without_command`, so running it bare printed help and exited 0. Now a bare `mngr config` / `mngr plugin` prints the standard usage/help listing and exits with the usual usage-error code (2), exactly like `mngr snapshot` and friends. The full man-page-style help is still available via `mngr config --help` / `mngr plugin --help`.

Regenerated the `config`, `plugin`, and `usage` command reference docs. `config`/`plugin` now render `COMMAND` (required) instead of `[COMMAND]`; the `usage` doc reflects a rendering change in the newer `click` release pulled in by the workspace dependency refresh (`usage` remains runnable bare -- it prints the usage snapshot).

Internal type-annotation touch-up in a create test to satisfy the updated type checker.
