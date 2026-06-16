Fixed the `test_create_custom_yolo_agent_type` tutorial e2e test, which exercises defining a custom agent type in the project config and creating an agent of that type.

The test previously ran `mngr config edit --scope project` (which creates the project `settings.toml` from a template) and then `mngr config set ...`. Under pytest the config guard refuses to load any config file that does not set `is_allowed_in_pytest = true`, so the freshly created `settings.toml` broke every subsequent config-loading command. The test now simulates the tutorial's manual editing step with a scripted `$EDITOR` that appends the `[agent_types.yolo]` block (and the test-only opt-in) when `mngr config edit` opens the file, then creates and verifies the agent.

Also added an unhappy-path test covering the same tutorial block: creating an agent whose type was never defined in config is rejected with a clear "Unknown agent type" error and leaves no agent behind.
