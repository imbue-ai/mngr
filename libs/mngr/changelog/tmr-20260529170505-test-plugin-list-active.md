Strengthened the e2e tutorial test for `mngr plugin list --active`. It now
verifies the `--active` contract (every listed plugin is enabled, and the active
set is a subset of the full plugin list) by parsing JSON output instead of only
checking that the command exits successfully. Added a companion test that
disables a plugin and confirms it disappears from `--active` while remaining in
the full `mngr plugin list` output, so the filtering behavior is actually
exercised (by default every plugin is enabled, so the filter was previously a
no-op in the test).
