Fixed race condition in `list_snapshots` that caused it to return an empty list when called immediately after snapshot creation.

The `list_snapshots` method in `ModalProviderInstance` was reading the host record from the cache (`use_cache=True`), which could return stale data if the cache hadn't been updated after a snapshot was created during `stop_host`. This caused the flaky test `test_restart_after_graceful_stop_without_initial_snapshot` to fail intermittently with `assert len(snapshots) == 1` failing because `list_snapshots` returned an empty list.

Changed `list_snapshots` to bypass the cache and read directly from the volume (`use_cache=False`) to ensure fresh data is always returned.
