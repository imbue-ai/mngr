## libs/mngr

- E2E test `test_list_json_with_no_agents` no longer carries `@pytest.mark.modal`: listing an empty environment does not actually invoke the modal CLI binary (modal Python SDK calls happen in the `mngr` subprocess and are not seen by the in-process SDK monkeypatch), so the resource guard's `NEVER_INVOKED` check failed. Also tightened the assertion to compare the full `{"agents": [], "errors": []}` payload and added a tutorial block matching `mngr list --format json`.
