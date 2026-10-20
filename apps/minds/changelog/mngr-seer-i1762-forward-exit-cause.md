The unexpected-exit report for the `mngr forward` subprocess now also records:

- the error class and message (scrubbed) that mngr's CLI reported for the failed plugin command;

- whether the backend had already received SIGTERM or SIGINT when the plugin died, and how long before.

That second fact tells a plugin that died alongside its parent, both stopped by one process-group signal, from one that crashed on its own. Backend Sentry events now carry an `is_under_pytest` tag, so failures from test runs (each of which counts as a new anonymous install) can be separated from real installs.

The report's `seconds_since_spawn` is now measured at the moment the plugin exited, not after the up-to-2-second wait for its pipes to drain.
