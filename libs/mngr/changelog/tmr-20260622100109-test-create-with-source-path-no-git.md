- Fixed the `test_create_with_source_path_no_git` e2e tutorial test so its
  agent-listing verification scopes discovery to the local provider
  (`mngr list --provider local`). The bare `mngr list` aborted with a non-zero
  exit code whenever an enabled-but-unconfigured remote provider (e.g. AWS)
  could not be reached, which is unrelated to what this test verifies. This
  matches the convention already used throughout the e2e suite.
