`mngr create` no longer hard-codes `claude` as the default agent type. The agent type must now come from a positional argument, `--type`, or `[commands.create] type` in user settings. If none of those is supplied, `mngr create` exits with a clear error listing the registered agent types and pointing at `mngr config set commands.create.type <name> --scope user`.

`scripts/install.sh` step 5 prints that same suggested `mngr config set` command and lists installed agent-type plugins (via `mngr plugin list --kind agent-type --active`). It does NOT write the setting for you -- you still need to run the suggested command yourself to set a default.

`mngr plugin list` gains a `--kind` filter with two values, `agent-type` and `provider`, that project the plugin list to the canonical set of agent type names or provider backend names (with version/description metadata when entry-point names match).

Migration: existing users who upgrade and have no `[commands.create] type` set will see an error from `mngr create` until they run `mngr config set commands.create.type <name> --scope user`. The error message includes the registered agent types so you can copy-paste a value.
