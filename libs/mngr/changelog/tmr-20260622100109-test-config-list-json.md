Strengthened the `mngr config list --format json` release test and added an edge-case test for its scoped variant.

The merged-view JSON test now asserts the full document shape (only a top-level `config` object, no `scope`/`path` keys) rather than just one key, and confirms the persisted value round-trips as a real boolean.

A new test covers `mngr config list --scope local --format json`, verifying the scoped JSON output carries the `scope` and `path` metadata and that the reported path is the file actually read.
