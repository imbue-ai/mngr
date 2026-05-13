`mngr create` no longer hard-codes `claude` as the default agent type. The agent type must now come from a positional argument, `--type`, or `[commands.create] type` in user settings. If none of those is supplied, `mngr create` exits with a clear error listing the registered agent types and pointing at `mngr config set commands.create.type <name> --scope user`.

New subcommand `mngr extras config`: walks through user-scope config settings the installer would otherwise leave blank. Each step short-circuits if its setting is already configured, so re-running only prompts for the gaps. Today this just covers the default agent type for `mngr create`; future config-related setup will be added as additional walk steps under the same subcommand. With an interactive terminal, presents a numbered picker of every available agent type plus a "keep no default" option, and writes the selection to `[commands.create] type` in user settings. With `-y` or without a terminal, prints the suggested `mngr config set` command and lists available agent types -- writes nothing.

`mngr extras -i` now also walks through the default agent type prompt as a final step, alongside the existing plugins / completion / Claude-plugin steps. `mngr extras` (no flag) reports the current default agent type as part of the status block.

`scripts/install.sh` no longer contains custom shell logic for the default agent type -- step 5 is gone, since the default agent type prompt now runs as part of `mngr extras -i` (step 4). The new subcommand is also re-runnable via `mngr extras config` if you skip it the first time.

`mngr plugin list` gains a `--kind` filter with two values, `agent-type` and `provider`, that project the plugin list to the canonical set of agent type names or provider backend names (with version/description metadata when entry-point names match).

Migration: existing users who upgrade and have no `[commands.create] type` set will see an error from `mngr create` until they run `mngr config set commands.create.type <name> --scope user` (or pick one via `mngr extras config`). The error message includes the registered agent types so you can copy-paste a value.
