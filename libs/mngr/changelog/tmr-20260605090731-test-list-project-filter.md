Removed the superfluous `@pytest.mark.modal` from the `test_list_project_filter`
e2e tutorial test. A read-only `mngr list --project ...` against a fresh
environment never invokes the `modal` CLI binary (the only Modal usage trackable
from a spawned subprocess) and never creates the Modal environment, so the
resource guard's "marked modal but never invoked modal" check failed the test.

Also strengthened the test to assert the filtered listing renders the expected
empty result ("No agents found") instead of only checking the exit code, so a
command that errors but still exits 0 would be caught.
