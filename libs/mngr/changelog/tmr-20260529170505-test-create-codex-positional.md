Fixed the `test_create_codex_positional` e2e tutorial test so it passes:

- Configure the codex command via `mngr config set --scope local` so the setting
  lands in the pytest-opted-in `settings.local.toml` instead of a fresh
  project-scope `settings.toml` (which lacks `is_allowed_in_pytest = true` and
  caused `mngr create` to reject the run).
- Removed the superfluous `@pytest.mark.modal`: creating a local codex agent and
  listing it never reaches the guarded Modal SDK chokepoint, so the mark was
  flagged as never-invoked.
- Strengthened the test to verify the created agent's type is actually `codex`
  (via `mngr list --format '{name} {type}'`), not merely that the command exited 0.
