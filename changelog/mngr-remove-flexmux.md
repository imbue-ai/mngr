Removed the unused `libs/flexmux/` project and all references to it (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions in `test_meta_ratchets.py` and `scripts/sync_common_ratchets.py`, and the `uv.lock` workspace member).

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in `libs/mngr/imbue/mngr/resources/Dockerfile` and the `.github/workflows/{ci,tmr}.yml` install steps, matching the corresponding bump to `[agent_types.claude].version` in `forever-claude-template/.mngr/settings.toml`.
