- Fixed the `test_command_agent_data_pipeline` e2e tutorial release test: it now
  scopes its verification listing to the modal provider (`mngr list --provider
  modal --format json`) instead of an unscoped `mngr list`, which failed in the
  test environment because discovery reached out to the unconfigured `aws`
  provider. Also added an assertion that the agent's host runs on the modal
  provider.
