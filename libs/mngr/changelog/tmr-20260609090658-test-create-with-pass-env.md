Fixed the e2e tutorial test fixture that wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, which produced an invalid-TOML parse error and broke every e2e tutorial test. Added a `@pytest.mark.timeout(120)` to `test_create_with_pass_env` so it matches its sibling tests and does not hit the default 10s timeout during the slow `mngr list` provider-discovery step.

Strengthened `test_create_with_pass_env` to additionally exec into the running agent and assert the forwarded `API_KEY` is visible in its live environment, not just in the on-disk env file.
