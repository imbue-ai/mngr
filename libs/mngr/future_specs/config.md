# Config Spec

The **Config** class represents the complete configuration for mngr, loaded from TOML files at either project or user scope. It defines custom agent types, provider instances, and environment settings.

For the user-facing documentation, see [config command](../docs/commands/secondary/config.md), [agent types](../docs/concepts/agent_types.md), and [providers](../docs/concepts/providers.md).

## Design Philosophy

Configuration follows these principles:

- **Layered scoping**: Project config overrides user config, allowing per-project customization while maintaining global defaults.
- **Stateless**: Config is loaded fresh on each command invocation. There is no persistent config daemon.
- **Validation at load time**: Invalid configuration fails fast with clear error messages.
- **Extensible providers**: Provider instances accept arbitrary backend-specific options via a flexible schema.

## File Format and Locations

Configuration uses TOML format. Files are loaded from:

| Scope | Location |
|-------|----------|
| User | `~/.mngr/profiles/<profile_id>/settings.toml` |
| Project | `.mngr/settings.toml` (relative to project root) |

If both files exist, they are merged with project scope taking precedence.

### Example TOML

```toml
# Common settings
prefix = "mngr-"
default_host_dir = "~/.mngr"

# Custom agent types
[agent_types.my_claude]
parent_type = "claude"
cli_args = "--env CLAUDE_MODEL=opus --idle-timeout 3600"
permissions = ["github", "npm"]

[agent_types.fast_codex]
parent_type = "codex"
cli_args = "--memory 8g"

# Provider instances
[providers.my-aws-prod]
backend = "aws"
region = "us-east-1"
profile = "production"

[providers.remote-docker]
backend = "docker"
host = "ssh://user@server"

# Command defaults
[commands.create]
branch = ":agent/*"
connect = false
```

## Command Defaults

Command defaults allow you to set default values for CLI command parameters. These values are used when the user does not explicitly provide a value on the command line.

```toml
[commands.create]
branch = ":agent/*"
connect = false

[commands.list]
format = "json"
```

Command defaults can also be set via environment variables using the pattern `MNGR_COMMANDS_<COMMANDNAME>_<PARAMNAME>=<value>`. See [environment variables](../docs/concepts/environment_variables.md) for details.

**Important**: Command names must be single words (no spaces, hyphens, or underscores). This is required for unambiguous parsing of the `MNGR_COMMANDS_*` environment variables. Any future plugins that register custom commands must follow this convention.

## Validation Rules

Type validation is performd when configs are loaded. Plugins will try to validate data at load time, but not everything is easy to validate at load time.
