Fixed the `test_create_with_dirty_tree_fails` e2e release test so it exercises the
intended behavior. Previously the test ran `mngr create my-task` with no agent
type, so the command aborted on "No agent type provided" before ever reaching the
ensure-clean check -- the test passed for the wrong reason. It now passes
`--type command --no-connect -- true` and asserts the error mentions
"uncommitted changes", confirming the dirty working tree is what causes the
failure.
