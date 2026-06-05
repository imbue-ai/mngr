Fixed the `test_list_watch_mode` e2e tutorial test. It was marked
`@pytest.mark.modal` even though `watch -n5 mngr list` against a fresh,
empty environment never exercises Modal (the listing pipeline skips the
Modal provider when its environment does not exist), which made the resource
guard fail the test for a superfluous mark. Removed the mark and strengthened
the assertion to verify that `watch` actually renders the wrapped `mngr list`
output.
