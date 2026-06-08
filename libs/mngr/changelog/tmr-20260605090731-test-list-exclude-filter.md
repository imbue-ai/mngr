Fixed the `test_list_exclude_filter` e2e tutorial test. It carried a
`@pytest.mark.modal` mark, but the documented `mngr list --exclude ...` command
never shells out to the `modal` CLI (it only performs in-process SDK discovery,
which the resource guard cannot observe across the subprocess boundary), so the
guard failed the test with "marked with @pytest.mark.modal but never invoked
modal". Removed the mark and rewrote the test to create two labeled command
agents and assert that the exclusion filter actually drops the matching agent
while keeping the others.
