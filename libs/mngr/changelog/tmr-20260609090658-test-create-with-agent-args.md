Fixed the e2e test fixture so the release tests in `test_create_basic.py` run again.

- The shared e2e `settings.local.toml` fixture (in `e2e/conftest.py`) wrote `type = "claude"`
  twice under `[commands.create]`, producing invalid TOML. Every `mngr` invocation in these
  tests aborted with "Cannot overwrite a value" before doing any work. Removed the duplicate
  key.
- Added `@pytest.mark.timeout(120)` to `test_create_with_agent_args`, which runs two sequential
  `mngr` operations (create, list) each performing full provider discovery and so exceeds the
  default 10s pytest-timeout.
