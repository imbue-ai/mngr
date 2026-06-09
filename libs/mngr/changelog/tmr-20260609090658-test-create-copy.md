Fixed the e2e tutorial test fixture and the `test_create_copy` release test.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made TOML parsing fail for
  every `mngr` command in the e2e tutorial suite. The duplicate has been removed.
- `test_create_copy` carried a spurious `@pytest.mark.modal` mark even though it only
  creates a local agent with a local git-mirror transfer; the resource guard's
  NEVER_INVOKED check failed the test. The mark has been removed.
- Strengthened `test_create_copy` to verify the git-mirror copy is a functional
  repository that carries over the source repo's commit history (via `git log`), not
  merely a directory containing a `.git` folder.
