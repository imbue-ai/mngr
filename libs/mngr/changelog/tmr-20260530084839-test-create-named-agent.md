Fixed the `test_create_named_agent` e2e release test (in `test_basic.py`): removed the
incorrect `@pytest.mark.modal` marker. The test creates a local `command`-type agent and
never invokes the Modal CLI, so the resource guard failed it with "marked with
@pytest.mark.modal but never invoked modal". The marker is only appropriate for tests that
exercise the Modal CLI (e.g. `--provider modal`, which calls `environment_create`).

Also strengthened the test to verify the named agent is actually running (via `mngr exec
my-task pwd`), not just that it appears in `mngr list`.
