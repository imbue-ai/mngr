Bump the pinned Claude Code version in `libs/mngr/imbue/mngr/resources/Dockerfile` from 2.1.269 to 2.1.280.

2.1.280 is the first pin whose binary carries the `claude-opus-5-5` model id, and its `opus` / `opus[1m]` aliases resolve to Claude Opus 5.5, so on the old pin neither the release tests nor a dev agent created from this repo's templates could select Opus 5.5. The release test `test_claude_code_version_matches_default_workspace_template_pin` requires this literal to equal default-workspace-template's `[agent_types.claude].version`, so it lands together with the matching bump there.
