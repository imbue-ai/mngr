# Environment Variables

## Setting Custom Variables

See the [`mngr create` command](../commands/primary/create.md)  for details about adding environment variables into the env file for new hosts and agents.

See the [`mngr provision` command](../commands/secondary/provision.md) for details about modifying environment variables for existing hosts and agents.

## Scope and lifecycle

Environment variables are configured for agents during provisioning and for hosts during creation.

Environment variables in mngr exist in two separate contexts:

1. your local shell (where you run `mngr`) and 
2. inside agent environments on hosts (where agents run).

The following environment variables are special because they control both the behavior of mngr (and the behavior of any recursive calls to mngr on the host):

- `MNGR_ROOT_NAME` - Root name to use for complete isolation (default: `mngr`). This affects:
  - **Config file locations**: Looks for `~/.{root_name}/profiles/<profile_id>/settings.toml`, `.{root_name}/settings.toml`, and `.{root_name}/settings.local.toml`
  - **Default values**: Sets `MNGR_PREFIX={root_name}-` and `MNGR_HOST_DIR=~/.{root_name}` when not explicitly configured
  - **Use case**: Running multiple isolated mngr instances on the same machine
  - Example: `MNGR_ROOT_NAME=foo` results in:
    - Config files: `~/.foo/profiles/<profile_id>/settings.toml`, `.foo/settings.toml`, `.foo/settings.local.toml`
    - Defaults: `MNGR_PREFIX=foo-` and `MNGR_HOST_DIR=~/.foo`
  - Explicit `MNGR_PREFIX` or `MNGR_HOST_DIR` values override the derived defaults
- `MNGR_PREFIX` - Prefix for naming resources (default: `mngr-`). Affects tmux session names, Docker container names, etc.
- `MNGR_HOST_DIR` - Base directory for all mngr data on a host (default: `~/.mngr`)

Changing those variables after creating a host is not supported.

Additionally, the following variable controls where project-level config files are loaded from:

- `MNGR_PROJECT_DIR` - Directory containing project-level config files (`settings.toml` and `settings.local.toml`). When set, overrides the default `.{root_name}/` directory at the git root. This only affects where project settings are loaded from; it does not affect `MNGR_HOST_DIR`. Unlike the variables above, this can be changed freely at any time.

## Command-Specific Variables

You can override default values for CLI command parameters using environment variables with the pattern:

```
MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME>=<value>
```

Where:
- `COMMANDNAME` is the command name in uppercase (e.g., `CREATE`, `LIST`)
- `PARAMNAME` is the parameter name in uppercase with underscores (e.g., `NEW_BRANCH_PREFIX`)

Examples:
- `MNGR_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/` - Sets the default branch prefix for the create command
- `MNGR_COMMANDS_CREATE_CONNECT=false` - Disables auto-connect after creating an agent
- `MNGR_COMMANDS_LIST_FORMAT=json` - Sets default output format for list command
- `MNGR_COMMANDS_CREATE_ADD_COMMAND=` - Clears all additional commands (overrides config file defaults)

Values are stored as strings and converted to the appropriate type by click/pydantic based on the parameter's type definition.

**Clearing list/tuple parameters**: For repeatable options (like `--extra-window`), setting the environment variable to an empty string clears the list entirely. This is useful for overriding config file defaults on a per-invocation basis.

These environment variable overrides are applied after config files but before CLI arguments, following the standard precedence order:

1. Built-in defaults
2. User config (`~/.mngr/profiles/<profile_id>/settings.toml`)
3. Project config (`.mngr/settings.toml`)
4. Local config (`.mngr/settings.local.toml`)
5. **Environment variables** (`MNGR_COMMANDS_*`)
6. CLI arguments

**Important**: Command names must be single words (no spaces, hyphens, or underscores). This is required for unambiguous parsing of the environment variable names. Any future plugins that register custom commands must follow this convention.

## Agent Runtime Variables

In addition to the above, mngr also automatically sets these inside agent tmux sessions:

- `MNGR_AGENT_ID` - The agent's unique identifier
- `MNGR_AGENT_NAME` - The agent's human-readable name
- `MNGR_AGENT_STATE_DIR` - Per-agent directory for status, plugins, logs, etc. (`$MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/`)
- `MNGR_AGENT_WORK_DIR` - The directory containing your project files, where the agent starts
- `MNGR_HOST_DIR` - The base directory for all mngr data on the host

These variables are available inside agent sessions and can be used in scripts, hooks, and by agents themselves. See [conventions](../conventions.md) for directory layouts.
