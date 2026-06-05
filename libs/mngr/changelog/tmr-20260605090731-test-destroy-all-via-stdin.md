Added a `@pytest.mark.timeout(120)` marker to the `test_destroy_all_via_stdin`
e2e tutorial test. The test creates two command agents plus several list/destroy
commands, which exceeds the default 10s per-test timeout (it previously timed out
during the second `mngr create`). The new marker matches the convention used by
other multi-step destroy tests (e.g. `test_create_and_destroy_agent`, which uses
`@pytest.mark.timeout(60)` for a single create+destroy).
