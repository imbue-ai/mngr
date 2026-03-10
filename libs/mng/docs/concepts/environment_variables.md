# Environment Variables

## Setting Custom Variables

See the [`mng create` command](../commands/primary/create.md)  for details about adding environment variables into the env file for new hosts and agents.

See the [`mng provision` command](../commands/secondary/provision.md) for details about modifying environment variables for existing hosts and agents.

## Scope and lifecycle

Environment variables are configured for agents during provisioning and for hosts during creation.

Environment variables in mng exist in two separate contexts:

1. your local shell (where you run `mng`) and 
2. inside agent environments on hosts (where agents run).

The following environment variables are special because they control both the behavior of mng (and the behavior of any recursive calls to mng on the host):

- `MNG_ROOT_NAME` - Root name to use for complete isolation (default: `mng`). This affects:
  - **Config file locations**: Looks for `~/.{root_name}/profiles/<profile_id>/settings.toml`, `.{root_name}/settings.toml`, and `.{root_name}/settings.local.toml`
  - **Default values**: Sets `MNG_PREFIX={root_name}-` and `MNG_HOST_DIR=~/.{root_name}` when not explicitly configured
  - **Use case**: Running multiple isolated mng instances on the same machine
  - Example: `MNG_ROOT_NAME=foo` results in:
    - Config files: `~/.foo/profiles/<profile_id>/settings.toml`, `.foo/settings.toml`, `.foo/settings.local.toml`
    - Defaults: `MNG_PREFIX=foo-` and `MNG_HOST_DIR=~/.foo`
  - Explicit `MNG_PREFIX` or `MNG_HOST_DIR` values override the derived defaults
- `MNG_PREFIX` - Prefix for naming resources (default: `mng-`). Affects tmux session names, Docker container names, etc.
- `MNG_HOST_DIR` - Base directory for all mng data on a host (default: `~/.mng`)

Changing those variables after creating a host is not supported.

## Command-Specific Variables

You can override default values for CLI command parameters using environment variables with the pattern:

```
MNG_COMMANDS_<COMMANDNAME>_<PARAMNAME>=<value>
```

Where:
- `COMMANDNAME` is the command name in uppercase (e.g., `CREATE`, `LIST`)
- `PARAMNAME` is the parameter name in uppercase with underscores (e.g., `NEW_BRANCH_PREFIX`)

Examples:
- `MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/` - Sets the default branch prefix for the create command
- `MNG_COMMANDS_CREATE_CONNECT=false` - Disables auto-connect after creating an agent
- `MNG_COMMANDS_LIST_FORMAT=json` - Sets default output format for list command
- `MNG_COMMANDS_CREATE_ADD_COMMAND=` - Clears all additional commands (overrides config file defaults)

Values are stored as strings and converted to the appropriate type by click/pydantic based on the parameter's type definition.

**Clearing list/tuple parameters**: For repeatable options (like `--extra-window`), setting the environment variable to an empty string clears the list entirely. This is useful for overriding config file defaults on a per-invocation basis.

These environment variable overrides are applied after config files but before CLI arguments, following the standard precedence order:

1. Built-in defaults
2. User config (`~/.mng/profiles/<profile_id>/settings.toml`)
3. Project config (`.mng/settings.toml`)
4. Local config (`.mng/settings.local.toml`)
5. **Environment variables** (`MNG_COMMANDS_*`)
6. CLI arguments

**Important**: Command names must be single words (no spaces, hyphens, or underscores). This is required for unambiguous parsing of the environment variable names. Any future plugins that register custom commands must follow this convention.

## Agent Runtime Variables

In addition to the above, mng also automatically sets these inside agent tmux sessions:

- `MNG_AGENT_ID` - The agent's unique identifier
- `MNG_AGENT_NAME` - The agent's human-readable name
- `MNG_AGENT_STATE_DIR` - Per-agent directory for status, plugins, logs, etc. (`$MNG_HOST_DIR/agents/$MNG_AGENT_ID/`)
- `MNG_AGENT_WORK_DIR` - The directory containing your project files, where the agent starts
- `MNG_HOST_DIR` - The base directory for all mng data on the host

These variables are available inside agent sessions and can be used in scripts, hooks, and by agents themselves. See [conventions](../conventions.md) for directory layouts.
