Test maintenance for the tutorial e2e suite (no user-facing behavior change):

- Fixed the e2e test fixture so it no longer writes an invalid `settings.local.toml`. The
  `[commands.create]` section had a duplicate `type = "claude"` key, which made every `mngr`
  command in an e2e tutorial test fail with "Cannot overwrite a value" while parsing the config.
- Strengthened `test_create_git_mirror_with_existing_branch`: it now also verifies that the
  agent's git mirror is checked out at the same commit the existing branch points to in the
  source repo (not merely that a same-named branch exists). Hardened its `mngr exec` verification
  calls with a longer timeout to absorb agent/provider-discovery latency under local load.
