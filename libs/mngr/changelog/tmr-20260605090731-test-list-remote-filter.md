Fixed the `test_list_remote_filter` e2e tutorial test. It was marked
`@pytest.mark.modal`, but `mngr list --remote` only performs read-only Modal SDK
discovery (which short-circuits when no Modal environment exists) and never
invokes the Modal CLI, so the resource guard correctly reported that the test
never exercised Modal. Removed the unsatisfiable mark and strengthened the test
to actually verify the `--remote` filter: it now creates a local agent and
asserts that the agent is excluded from `mngr list --remote` while remaining
visible under `mngr list --local`.
