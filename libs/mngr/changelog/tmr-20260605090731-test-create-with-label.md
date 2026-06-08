Fixed the `test_create_with_label` e2e release test. It now raises the pytest
timeout (the local `mngr create --type command` routinely exceeds the global
10s default) and drops the superfluous `@pytest.mark.modal` mark, since the
test performs a purely local create whose Modal discovery never reaches the
resource guard's subprocess-tracked `modal` CLI path.
