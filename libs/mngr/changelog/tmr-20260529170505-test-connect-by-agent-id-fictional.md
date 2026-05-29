Fixed the `test_connect_by_agent_id_fictional` e2e tutorial test. The
`mngr connect <agent-id>` discovery path is read-only and never shells out to
the `modal` CLI, so the test could not satisfy `@pytest.mark.modal` (the
resource guard's "marked but never invoked" check failed it). Removed the
spurious `modal` mark and gave the test enough timeout headroom for the
full-provider discovery scan, which sits just above the default 10s per-test
timeout. Also strengthened the assertions to verify the real "Agent not found"
error and that the supplied id is echoed back.
