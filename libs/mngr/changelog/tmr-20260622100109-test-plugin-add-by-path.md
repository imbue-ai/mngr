Fixed the `test_plugin_add_by_path` release test so it no longer fails with a spurious pytest timeout: added a 60s per-test timeout to accommodate the ~10s mngr cold-start cost, matching the other subprocess-driven plugin e2e tests.

Clarified the test's inline comment to accurately describe why `mngr plugin add --path` aborts cleanly in the test environment.
