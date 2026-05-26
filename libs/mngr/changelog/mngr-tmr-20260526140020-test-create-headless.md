## mngr

- e2e `test_create_headless`: removed the incorrect `@pytest.mark.modal`. With `--headless --type command`, the agent is created on the local provider and nothing in the test exercises the Modal CLI, which made the resource guard report "never invoked modal".
