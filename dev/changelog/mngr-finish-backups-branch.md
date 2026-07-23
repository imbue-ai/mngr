ci.yml's manual dispatch gained a `minds_snapshot_test_filter` input: when set, the minds snapshot-resume offload run is scoped to tests matching that offload `--filter` expression (e.g. `test_sync_e2e`) instead of the whole suite.

The snapshot offload config's `max_parallel` was raised from 16 to 20: the `minds_snapshot_resume` suite grew to 14 tests (including the new in-place restore e2e), and sandbox reuse breaks the order-dependent stopped-container assertions, so the cap must stay comfortably above the test count.
