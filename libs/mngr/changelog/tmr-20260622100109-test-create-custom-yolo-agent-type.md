Fixed the custom-agent-type tutorial e2e test (`test_create_custom_yolo_agent_type`): it now defines the `yolo` agent type by writing directly to the freshly-created project config (including the `is_allowed_in_pytest` opt-in) instead of via `mngr config set`, which the pytest config guard rejected.

Added an unhappy-path test (`test_create_undefined_agent_type_fails`) that verifies creating an agent of an undefined type fails with a clear "Unknown agent type" error and leaves no dangling agent behind.
