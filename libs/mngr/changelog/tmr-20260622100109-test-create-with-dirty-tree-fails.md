Raise the per-test timeout for the `test_create_with_dirty_tree_fails` e2e release test to 60s.

The test issues two `mngr` invocations in sequence (the rejected `create` and a follow-up `list`), and each pays a multi-second process spawn plus module-import cold-start before doing any work. On a cold or slow host that exceeds the default 10s per-test timeout even though the dirty-tree guard itself aborts almost immediately, so the test could time out despite the implementation behaving correctly. This mirrors the existing headroom already granted to `test_create_duplicate_name_fails`.

Add a happy-path companion test, `test_create_with_dirty_tree_succeeds_with_no_ensure_clean`, covering the other half of the tutorial's dirty-tree guidance: that `mngr create --no-ensure-clean` proceeds in a dirty tree, registers a live agent, and carries the uncommitted change over into the agent's work dir.
