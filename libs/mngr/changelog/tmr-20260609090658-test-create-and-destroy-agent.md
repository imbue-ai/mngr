Fixed the e2e test fixture so tutorial tests can run again, and strengthened the
create-and-destroy tutorial test.

- The e2e conftest fixture wrote a `settings.local.toml` with a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). tomlkit
  rejects duplicate keys, so every e2e tutorial command failed up front with
  "Cannot overwrite a value". Removed the duplicate line.
- `test_create_and_destroy_agent` now asserts the agent appears in `mngr list`
  before it is destroyed, making the post-destroy absence check a real
  before/after contrast.
