Tab-completion of config keys (`mngr config set/get/unset <TAB>` and `-S <TAB>`) is now schema-driven for the dict-container namespaces instead of only completing keys already present in your config:

- `commands.<command>.<param>` completes every option of the command before you have set anything under it (e.g. `commands.destroy.<TAB>`), with boolean/choice options also completing their value. Group subcommands use their `<group>_<sub>` bucket (e.g. `commands.snapshot_create.*`).

- `commands.<group>.default_subcommand` completes for each group that supports a configurable default, offering that group's subcommand names as values.

- `pre_command_scripts.<command>` completes for every command, including the `<group>_<sub>` subcommand buckets.

- `plugins.<name>.*` completes from the plugin config schema for every installed plugin, and a configured `providers.<name>.*` / `create_templates.<name>.*` now offers all settable fields from the schema, not just the ones already written.

Fixed: completion previously emitted the internal `commands.<name>.defaults.<param>` and `create_templates.<name>.options.<param>` shapes, which do not round-trip through `mngr config set` (the parameter would be read literally as `defaults`/`options` and ignored at runtime). Completion now emits the correct user-facing `commands.<name>.<param>` / `create_templates.<name>.<param>` keys.
