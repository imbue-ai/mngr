- Strengthened the `test_create_provider_modal` tutorial e2e test: it now ties
  the created agent to its host in the `mngr list --provider modal` output and
  asserts the host is the modal provider and is `RUNNING`, rather than only
  checking that the agent name appears in the filtered listing.
