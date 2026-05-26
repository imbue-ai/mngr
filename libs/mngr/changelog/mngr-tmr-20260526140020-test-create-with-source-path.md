## libs/mngr

- e2e test `test_create_with_source_path` now sets `@pytest.mark.timeout(120)` so the test body has enough budget under the default 10s pytest-timeout, and drops the misplaced `@pytest.mark.modal` mark (the test creates a local-provider agent and never invokes the modal CLI binary via PATH, so the resource guard's "marked but never invoked" check fires after the test body otherwise passes).
