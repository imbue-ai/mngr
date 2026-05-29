Fixed the `test_list_active_filter` e2e tutorial test, which was failing its
resource guard: it carried `@pytest.mark.modal` but `mngr list --active` never
invokes the modal CLI/SDK in a way the guard can observe from the subprocess.
Dropped the superfluous modal mark and extended the test to create a real
(local) running agent and assert that it shows up in `mngr list --active`,
so the filter is exercised against a live agent instead of an empty list.
