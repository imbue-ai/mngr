Fixed the `test_create_and_destroy_agent` e2e tutorial test, which exercises
`mngr destroy my-task --force`. The test created a local command agent and never
contacted Modal in a way the resource guard can track (the only cross-process
modal signal is the `modal` CLI binary, invoked only during modal host creation),
so the erroneous `@pytest.mark.modal` mark failed the guard's "marked but never
invoked" check. Removed the mark. Also strengthened the test to confirm the agent
appears in `mngr list` before destroy, so the post-destroy absence check
demonstrably proves destroy removed it.
