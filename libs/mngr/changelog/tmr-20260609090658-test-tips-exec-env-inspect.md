Test-only changes (no user-visible behavior change):

- Fixed the e2e `e2e` fixture (`e2e/conftest.py`), which wrote a `[commands.create]`
  block with a duplicate `type = "claude"` key into `settings.local.toml`. TOML rejects
  duplicate keys, so every e2e test using the fixture failed at the first `mngr` command
  with "Cannot overwrite a value". Removed the duplicate line.
- Strengthened `test_tips_exec_env_inspect`: it now cross-checks that the
  `MNGR_AGENT_ID` exported into the exec'd environment matches the id mngr records for
  the agent, and verifies the `env | sort` output is actually sorted.
