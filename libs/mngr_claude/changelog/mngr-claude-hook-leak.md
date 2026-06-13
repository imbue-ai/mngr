Fixed mngr's Claude hooks leaking into "normal" (non-mngr) Claude config. mngr previously wrote its readiness/credential/permission hooks into the project's `.claude/settings.local.json`, which plain `claude` runs in that directory also read -- so the hooks (e.g. an activity-event `mkdir`) fired outside mngr where `$MNGR_AGENT_STATE_DIR`/`$MNGR_HOST_DIR` are unset, producing errors like `mkdir: cannot create directory '/events': Permission denied`.

mngr now bakes all of its own hooks into the per-agent config-dir `settings.json` (`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/settings.json`) -- the "user" settings layer Claude reads from `$CLAUDE_CONFIG_DIR`, which a plain `claude` run in the work dir never reads (it reads `~/.claude`). The hooks are built fresh on every provision, so there is no cross-version accumulation and nothing lands in a file plain `claude` reads.

`settings_overrides` is now **deep-merged** into that `settings.json` (previously a shallow `dict.update`). Nested keys preserve sibling values from the home base and lists concatenate, so e.g. a `permissions.allow` override no longer wipes a `permissions.defaultMode` from the home settings (#1647).

A user-supplied `--settings` (in an agent type's `cli_args` or passed through on the `mngr create` command line) now passes through to `claude` verbatim. mngr injects no `--settings` of its own, and Claude natively layers the user's `--settings` over the config-dir `settings.json` (deep-merging dicts, concatenating same-event hooks), so the user's hooks and mngr's both fire with no mngr merge code and nothing to collide.

Reduced-support limitation in `use_env_config_dir` mode: there is no per-agent config dir to bake hooks into, so mngr keeps loading its hooks from the private managed `--settings` file (`$MNGR_AGENT_STATE_DIR/plugin/claude/mngr_managed_settings.json`). In this mode `settings_overrides` is not applied, and a user `--settings` collides with mngr's (Claude is last-wins). This mode is not yet used in production.

Note: this stops *new* leaks; it does not remove hooks already written into existing `settings.local.json` files by a prior mngr -- clean those up manually if present.

`mngr create` no longer requires the project's `.claude/settings.local.json` to be gitignored across the board. mngr writes its own hooks to the per-agent config dir, so that requirement now applies only when the `claude_subagent_proxy` plugin (PROXY mode) actually needs to rewrite user-defined Stop hooks in `settings.local.json` -- enforced by that plugin, at the point it writes.
