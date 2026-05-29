Fixed the `test_create_codex_agent` e2e tutorial test (BASIC CREATION section):

- The e2e test fixture now pre-seeds the project `settings.toml` with
  `is_allowed_in_pytest = true`, so a project-scope `mngr config set` (the default
  scope) produces a config file that opts into the per-file pytest guard. Without
  this, any e2e test that ran `mngr config set` and then loaded config failed.
- Added a `@pytest.mark.timeout(120)` to the test, which runs three full mngr
  invocations and does not fit in the default 10s function timeout.
- Removed the superfluous `@pytest.mark.modal` from the test: it creates a local
  codex agent and never invokes Modal, which the resource guard rejects.
