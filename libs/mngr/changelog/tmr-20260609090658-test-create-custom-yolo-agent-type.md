Fixed the `test_create_custom_yolo_agent_type` e2e tutorial test. The shared e2e
fixture wrote a duplicate `type = "claude"` key into `settings.local.toml`,
producing invalid TOML that made `mngr config edit` fail to parse the config. The
test also configured the custom `yolo` agent type with only a `command` (no
`parent_type`), so the type could not resolve to a concrete agent class. The test
now points `yolo` at the built-in `command` parent, scopes its verification
`mngr list` to the local provider, and drops the spurious `@pytest.mark.modal`
mark (the test only exercises the local provider).
