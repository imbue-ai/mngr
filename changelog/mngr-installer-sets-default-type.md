`mngr create` no longer hard-codes `claude` as the default agent type. Instead, `scripts/install.sh` now prompts the user to pick a default during installation and writes it to `[commands.create] type` in their user settings. Day-to-day usage is unchanged for anyone who runs the installer.

`mngr plugin list` gains a `--kind agent-type` filter that the installer uses to enumerate installed agent-type plugins without hard-coding package names.

Migration: existing users who upgrade and have no `[commands.create] type` set will now see an error from `mngr create` until they either re-run `scripts/install.sh` or run `mngr config set commands.create.type <name> --scope user` manually. The error message includes the registered agent types and points at both remedies.
