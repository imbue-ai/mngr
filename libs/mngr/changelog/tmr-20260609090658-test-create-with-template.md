Fixed the e2e test fixture and expanded coverage for `mngr create --template`.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made tomlkit refuse to parse the
  config ("Cannot overwrite a value") and broke every e2e test that loaded it. Removed the
  duplicate line.
- Added `test_create_with_nonexistent_template`, an unhappy-path companion to
  `test_create_with_template`, verifying that creating with an unconfigured template name
  fails with a helpful error that names the missing template and lists the available ones,
  and that no agent is created.
