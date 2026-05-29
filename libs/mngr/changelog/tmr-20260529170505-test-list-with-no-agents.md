Removed the superfluous `@pytest.mark.modal` from the `test_list_with_no_agents`
e2e tutorial test. In a fresh environment the Modal environment does not exist
yet, so `mngr list` skips the Modal provider (via `ProviderEmptyError`) and only
performs an in-process SDK lookup -- it never shells out to the `modal` CLI
binary that the e2e resource guard tracks. The mark therefore tripped the
guard's superfluous-mark check. This is intended behavior: `mngr list` must
never bootstrap a Modal environment.
