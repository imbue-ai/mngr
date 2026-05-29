Fixed the `test_create_modal_provision_append_file` e2e tutorial test, which
was failing because the isolated test profile has no default agent type
configured. The test now passes `--type command -- sleep` (the cheap, auth-free
pattern already used by other modal tests in the file) and verifies that the
`--extra-provision-command` actually ran on the remote host.
