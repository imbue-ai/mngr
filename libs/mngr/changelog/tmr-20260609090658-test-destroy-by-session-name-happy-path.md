Fixed the release e2e test `test_destroy_by_session_name_happy_path` (destroy section).

- Fixed the shared e2e fixture (`e2e/conftest.py`): the generated `settings.local.toml` had a
  duplicate `type = "claude"` key inside `[commands.create]` (introduced by a squashed
  conflict resolution), which made tomlkit reject the file with "Cannot overwrite a value" and
  broke `mngr create` for every e2e test using the fixture. Removed the duplicate line.
- Removed the stale `@pytest.mark.modal` marker from the test. It only creates a local
  `command`-type agent and destroys it by tmux session name; it never invokes the bare `modal`
  CLI (the only path the resource guard observes), so the guard failed the test with "marked
  with @pytest.mark.modal but never invoked modal". The mark has no effect on CI selection
  (release offload filters by `release`).
