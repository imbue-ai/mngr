Fixed and tightened the `test_archive_command` e2e tutorial test so it reliably verifies the documented scope of `mngr archive` on a stopped agent:

- Added a `timeout(120)` mark (the default 10s was too short for the create+stop+archive round-trip) and dropped the superfluous `rsync` mark (the test creates a local command agent and never invokes rsync).

- Scoped the archived-listing verification to `--provider local` (matching the sibling `test_stop_archive`), since `my-task` is a local command agent and the test's scope is local-only. This removes an unnecessary remote-provider enumeration, so the `modal` mark is no longer needed.
