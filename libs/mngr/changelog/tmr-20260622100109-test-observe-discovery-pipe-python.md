Fixed the `test_observe_discovery_pipe_python` e2e tutorial test so it reliably exercises the `mngr observe --discovery-only` streaming pipe:

- The discovery-cache warm-up now lists with `--on-error continue` so that an enabled-but-unconfigured provider (e.g. AWS/GCP/Azure without credentials) no longer aborts the listing before the full discovery snapshot is written to disk.

- The `mngr observe --discovery-only` invocations now use a timeout window that comfortably exceeds mngr's process startup cost, instead of a short window that killed the process mid-startup before it could emit the cached snapshot.
