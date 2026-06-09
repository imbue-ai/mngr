Fixed two e2e test-fixture issues that broke the create-time env-var tutorial tests:

- The e2e fixture wrote a duplicate `type = "claude"` key into the same
  `[commands.create]` table of `settings.local.toml` (a stray line left by a
  bulk merge), which is invalid TOML and made *every* e2e command fail with
  "Cannot overwrite a value". Removed the duplicate.
- `test_create_with_env_vars` still carried a stale `@pytest.mark.modal` even
  though it creates on the default provider and never invokes Modal (its sibling
  default-provider tests had the mark removed in the same merge). The resource
  guard correctly flagged the superfluous mark; removed it.
