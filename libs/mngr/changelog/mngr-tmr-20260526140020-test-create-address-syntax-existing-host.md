Fix and improve the `test_create_address_syntax_existing_host` e2e release
test. The test now passes an explicit `--type command` so it reaches the
host-lookup step it intends to exercise (the e2e fixture does not configure a
default `[commands.create] type`), and it no longer carries `@pytest.mark.modal`
since the host-not-found path no longer invokes the modal CLI. The test also
verifies that the failing host name appears in the error and that no agent
state is actually written when creation aborts.
