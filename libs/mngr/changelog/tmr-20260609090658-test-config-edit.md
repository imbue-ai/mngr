Fixed the e2e tutorial test fixture and the `test_config_edit` release test.

- The shared e2e subprocess fixture wrote a `settings.local.toml` containing a
  duplicate `type = "claude"` key under `[commands.create]`, which made tomlkit
  reject the file ("Cannot overwrite a value") and broke every e2e tutorial
  test. Removed the duplicate key.
- Adapted `test_config_edit` to the fixture's seeded project `settings.toml`:
  the project-scope config file already exists, so the test now verifies that
  `config edit` opens that exact file in `$EDITOR` and that the editor's marker
  persists into it, instead of asserting the file was created from scratch.
- Added `@pytest.mark.timeout(60)` to `test_config_edit`, which now runs two
  mngr subprocesses and exceeded the default 10s function timeout.
