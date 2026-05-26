[FIX_TEST] `test_create_in_place` no longer asserts `@pytest.mark.modal` -- the
mark was spurious (the test only touches Modal via the Python SDK in a
subprocess, which the resource guard cannot observe). Added
`@pytest.mark.timeout(60)` to match `test_create_with_transfer_none`, since the
test exercises Modal env discovery via `mngr list` and can exceed the default
10-second timeout.
