Removed the spurious `@pytest.mark.modal` mark from the `mngr list --provider`
e2e tutorial test. The mark is unsatisfiable for these subprocess-based e2e
tests (the Modal SDK guard only observes in-process calls, and `mngr list`
skips the Modal backend entirely when its per-user environment does not exist
yet), so the resource guard failed them with "marked modal but never invoked".
Also strengthened the test to assert the empty-listing output and added an
unhappy-path test covering an unknown provider name.
