Fixed the `test_list_watch_mode` e2e tutorial test. It was marked
`@pytest.mark.modal`, but `mngr list` on a fresh environment deliberately never
contacts Modal (the Modal backend raises `ProviderEmptyError` at construction so
`mngr list` does not silently bootstrap a Modal environment), so the resource
guard failed the test with a "never invoked modal" violation. Removed the
incorrect mark and strengthened the test so that `watch -n5 mngr list` runs at
least one full refresh and the rendered agent list is asserted on.
