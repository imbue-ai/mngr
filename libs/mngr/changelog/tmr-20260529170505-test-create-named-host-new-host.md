Fixed the `test_create_named_host_new_host` Modal e2e tutorial test: it now
passes an explicit `--type command -- sleep N` agent type (the isolated e2e
profile configures no default agent type) and asserts that the named host
`my-modal-box` actually appears in `mngr list` output.
