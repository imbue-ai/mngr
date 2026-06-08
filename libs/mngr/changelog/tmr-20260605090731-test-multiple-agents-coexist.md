Fixed the multi-agent e2e release tests (`test_multiple_agents_coexist`,
`test_list_filter_by_state`) so they pass outside of offload: added explicit
`@pytest.mark.timeout` markers (the global 10s default killed them in plain
`pytest` runs) and removed the spurious `@pytest.mark.modal` mark. These tests
create only local-provider command agents and never invoke Modal through a
tracked code path, so the Modal resource guard failed them with "marked with
@pytest.mark.modal but never invoked modal".
