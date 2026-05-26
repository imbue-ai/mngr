## mngr

- `test_create_and_rename_agent` e2e test:
  - Add `@pytest.mark.timeout(120)` so the test is not killed by the global 10s pytest timeout while creating and renaming an agent.
  - Remove `@pytest.mark.modal`; the test exercises the local provider and never invokes the modal CLI binary or the modal Python SDK in-process (subprocess SDK calls are not visible to the parent-process SDK guard), so the resource guard's "marked but never invoked" check fails.
