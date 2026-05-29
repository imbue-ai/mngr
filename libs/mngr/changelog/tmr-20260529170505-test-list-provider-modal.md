Fixed the `test_list_provider_modal` tutorial e2e test (and unblocked the other
`mngr create --provider modal` tutorial tests). The e2e test profile now
configures a default agent type (`[commands.create] type = "claude"`), since
`mngr create` no longer falls back to a built-in default and instead errors with
"No agent type provided" when none is configured. The list test now creates a
Modal agent before listing so that `mngr list --provider modal` actually queries
Modal (satisfying the `@pytest.mark.modal` resource guard) and asserts the
created agent appears in the listing.
