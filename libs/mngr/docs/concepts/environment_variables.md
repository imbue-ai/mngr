# Environment Variables

## Setting Custom Variables

See the [`mngr create` command](../commands/primary/create.md) for details about adding environment variables into the env file for new hosts and agents.

## Scope and lifecycle

Environment variables in mngr exist in two separate contexts:

1. your local shell (where you run `mngr`)
2. inside agent environments on hosts (where agents run)

## Overriding any config setting from the shell

Any field on the `MngrConfig` schema (and any sub-config registered by a plugin) can be set from the shell using the `MNGR__*` env var family:

```
MNGR__<SEGMENT>__<SEGMENT>__...=<value>
```

Each `__`-separated segment after the `MNGR__` prefix maps 1:1 to a step in the dotted config path. Segments are uppercase-only and lowercased before lookup. Values are JSON-parsed first (so `true`, `false`, numbers, arrays, and objects all work) and fall back to the raw string.

```bash
# These three are equivalent:
MNGR__COMMANDS__CREATE__CONNECT=false   mngr create my-agent
mngr --setting commands.create.connect=false create my-agent
mngr config set commands.create.connect false
mngr create my-agent

# Multi-word names work without ambiguity:
MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS='["--model","opus"]' mngr create my-agent
```

### Assigning vs extending

The bare key is **always** an assignment — the override replaces whatever the lower-precedence layers produced. To opt into additive behavior (append for lists/tuples, recursive key-merge for dicts, union for sets), add `__extend` (or `__EXTEND` for env vars) to the leaf key:

```bash
# Replace the entire cli_args list:
MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS='["--debug"]' mngr create
# -> cli_args = ["--debug"]

# Append to whatever the base config provides:
MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS__EXTEND='["--debug"]' mngr create
# -> cli_args = [<base values>..., "--debug"]
```

The same `__extend` suffix is recognised in TOML, in `--setting`, and in `mngr config set / extend` — one rule, four call sites.

Dict extension is **recursive**: a nested `key__extend` inside an `__extend` value merges deeper (extending `base[key]`), while a nested bare `key` replaces at that level (preserving its siblings). This is backward-compatible — any value that nests no inner `__extend` marker merges exactly as the previous one-level operator did (bare nested keys replace, siblings survive).

`__extend` on a scalar field raises `ConfigParseError`. A shape mismatch (e.g. a string value used to extend a list field) also raises.

### Assignment narrowing safety net

Assign-by-default makes it easy to accidentally drop earlier entries when a higher-precedence layer writes the same key. For example, with `commands.create.env = ["X=4"]` in `.mngr/settings.toml` and `commands.create.env = ["X=5"]` in `.mngr/settings.local.toml`, the merged value silently becomes `["X=5"]` — the `X=4` from the project layer is lost.

To prevent silent data loss during the migration, mngr raises `ConfigParseError` when a higher-precedence settings layer would assign over a non-empty list/tuple/dict/set value with anything that doesn't preserve every prior entry. The error tells you how to fix it:

1. Switch the specific key to `__extend` to keep the additive behavior (e.g. `env__extend = ["X=5"]` in the local layer).
2. Set `allow_settings_key_assignment_narrowing = true` (a top-level field on `MngrConfig`) to opt into the assign-by-default behavior globally.

The only assignments that pass without flagging are no-ops (the override value equals the base value) and supersets (every base entry survives, e.g. an `__extend` result or an explicit assign that happens to include every prior value). Clearing (`env = []` over a non-empty base) and any other partial drop are flagged — clearing is the most extreme form of data loss, so the user must explicitly opt in.

Layers that don't write the field at all are never flagged: `parse_config` leaves unset fields as `None`, and the merge falls back to the lower-precedence value without involving the narrowing guard. So defaults from one layer can't silently wipe entries that an earlier layer set.

The same rules apply uniformly to fields inside per-key container entries:
- `agent_types.<name>.<field>` (e.g. `cli_args`, `env`, `extra_provision_command`)
- `providers.<name>.<field>` (including backend-specific subclass fields)
- `create_templates.<name>.<field>` (resolved through the wrapper's `options` map)
- `plugins.<name>.<field>` (including plugin-defined subclass fields)
- `commands.<name>.<field>` (resolved through the wrapper's `defaults` map)

Create templates (applied at command time via `--template <name>`) also follow these rules. A template option like `[create_templates.dev] env = ["X=3"]` assign-by-defaults against the existing param value (from config defaults plus any earlier templates in the same `--template a --template b` chain); writing `env__extend = ["X=3"]` instead opts that template into additive behavior, appending to whatever the prior layers produced. The narrowing guard fires when a template would silently drop entries, using the same `allow_settings_key_assignment_narrowing` flag.

Adding a brand-new entry under any of these (e.g. defining `[agent_types.my_other_claude]` in `settings.local.toml` when only `[agent_types.my_claude]` was in `settings.toml`) is a pure addition and never narrows; the container-level merge is per-key additive by design.

Besides setting `allow_settings_key_assignment_narrowing = true` to allow assign-by-default narrowing globally, you can opt a specific key out per layer: `key__extend` keeps the additive (merge) behavior, and `key__assign` replaces the value without the narrowing error.

CLI flags that supply tuple/list values (e.g. `--env X=6`) always extend the merged settings value rather than replace it — the result is `<config values> + <template result> + <CLI values>` for the `create` command (CLI extends after templates apply), and `<config values> + <CLI values>` for everything else. The safety net only applies to the settings-file / env-var / `--setting` / template merge, not to CLI flag merging — the CLI is always the user's final, additive word.

### Precedence

Lowest to highest:

1. Built-in defaults
2. User config (`~/.mngr/profiles/<profile_id>/settings.toml`)
3. Project config (`.mngr/settings.toml`)
4. Local config (`.mngr/settings.local.toml`)
5. `MNGR__*` env vars (and the preserved-alias env vars below)
6. `--setting` CLI arguments
7. CLI arguments

### Strictness

Unknown `MNGR__*` keys raise `ConfigParseError` in strict mode (the default). Set `MNGR_ALLOW_UNKNOWN_CONFIG=1` to downgrade unknown-field errors to warnings — symmetric with how TOML parsing handles unknown fields.

### Discovering settable keys

Use `mngr config schema` (or `mngr config list --all`) to print every settable key path with its declared type and current effective value.

## Preserved old-style env vars

A small set of `MNGR_*` env vars (without the double underscore) is kept verbatim because they affect *which* config gets loaded or are otherwise process-level:

- `MNGR_ROOT_NAME` — root name used for complete isolation (default: `mngr`). Affects config file paths (`~/.{root_name}/profiles/.../settings.toml`, `.{root_name}/settings.toml`, `.{root_name}/settings.local.toml`) and the derived defaults for `MNGR_PREFIX` and `MNGR_HOST_DIR`. Used to run multiple isolated mngr instances on the same machine.
- `MNGR_PREFIX` — prefix for naming resources (default: `{root_name}-`). Affects tmux session names, Docker container names, etc. Alias for `MNGR__PREFIX`.
- `MNGR_HOST_DIR` — base directory for all mngr data on a host (default: `~/.{root_name}`). Alias for `MNGR__DEFAULT_HOST_DIR`.
- `MNGR_HEADLESS` — disables all interactive behavior. Alias for `MNGR__HEADLESS`.

Changing `MNGR_ROOT_NAME`, `MNGR_PREFIX`, or `MNGR_HOST_DIR` after a host has been created is not supported.

If both an alias and its canonical `MNGR__*` form are set with **different** values, mngr raises `ConfigParseError` at startup. Same value on both forms is fine.

Additionally, the following non-config env vars are recognized:

- `MNGR_PROJECT_CONFIG_DIR` — directory containing project-level config files. When set, overrides the default `.{root_name}/` directory at the git root. Affects only where project settings are loaded from.
- `MNGR_ALLOW_UNKNOWN_CONFIG` — when truthy, unknown TOML / env-var / `--setting` keys produce warnings instead of errors.
- `MNGR_LOAD_ALL_PLUGINS` — when truthy, bypasses normal plugin filtering (used by tooling such as doc generation).
- `MNGR_USER_ID` — explicit user id (overrides the persisted value in `~/.mngr/profiles/<profile_id>/user_id`).
- `MNGR_TEST_VERBOSE` — increases test-only logging output.
- `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` — debug flag that keeps the host lock file in place after a failed create so the host doesn't idle-shut-down before you can investigate.

## Agent Runtime Variables

mngr automatically sets these inside agent tmux sessions:

- `MNGR_AGENT_ID` — the agent's unique identifier
- `MNGR_AGENT_NAME` — the agent's human-readable name
- `MNGR_AGENT_STATE_DIR` — per-agent directory for status, plugins, logs, etc. (`$MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/`)
- `MNGR_AGENT_WORK_DIR` — the directory containing your project files, where the agent starts
- `MNGR_HOST_DIR` — the base directory for all mngr data on the host
- `MNGR_GIT_BASE_BRANCH` — the git base branch from which the agent's worktree was created (when applicable)

These variables are available inside agent sessions and can be used in scripts, hooks, and by agents themselves. See [conventions](../conventions.md) for directory layouts.
