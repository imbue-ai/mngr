# Customization

`mng` is designed to be highly customizable through configuration files, plugins, and custom agent types.

## Configuration Files

`mng` loads configuration from multiple sources with the following precedence (lowest to highest):

1. User config: `~/.mng/profiles/<profile_id>/settings.toml`
2. Project config: `.mng/settings.toml` (at git root, context dir, or `MNG_PROJECT_DIR`)
3. Local config: `.mng/settings.local.toml` (at git root, context dir, or `MNG_PROJECT_DIR`)
4. Environment variables: `MNG_PREFIX`, `MNG_HOST_DIR`, `MNG_ROOT_NAME`
5. CLI arguments (highest precedence)

### Default Subcommands

You can configure which subcommand runs when a command group is invoked with no recognized subcommand. By default, both `mng` and `mng snapshot` with no subcommand show help.

To restore the old behavior where `mng my-task` is equivalent to `mng create my-task`, set the default subcommand explicitly:

```toml
# .mng/settings.toml

# Running bare `mng` defaults to `mng create` (opt-in)
[commands.mng]
default_subcommand = "create"

# Running bare `mng` defaults to `mng list` instead
[commands.mng]
default_subcommand = "list"
```

**Disabling a configured default:**

Set `default_subcommand` to an empty string to disable defaulting entirely. When disabled (or absent), running the group with no subcommand shows help, and unrecognized arguments produce an error instead of being forwarded.

```toml
[commands.mng]
default_subcommand = ""   # explicitly disable (same as the built-in default)
```

**Notes:**

- If `default_subcommand` is absent from config, both `mng` and `mng snapshot` show help (no defaulting).
- The `default_subcommand` key can coexist with parameter defaults in the same `[commands.<name>]` section.
- Config file precedence applies as usual: local config overrides project config, which overrides user config.

### Default Command Parameters

You can override default values for CLI command parameters in your config files. This is particularly useful for setting project-specific or user-specific defaults.

**How it works:**

- Config files can define default values for any CLI parameter using `[commands.<command_name>]` sections
- These defaults only apply when the user doesn't explicitly specify a value
- User-specified values (via CLI or environment) always take precedence

**Example:**

```toml
# .mng/settings.toml

# Override defaults for the 'create' command
[commands.create]
connect = false               # Don't auto-connect after creation
ensure_clean = false          # Allow dirty working trees
name_style = "scifi"          # Use sci-fi style names by default
```

With this config:

- `mng create` → Doesn't connect, allows dirty trees
- `mng create --connect` → Connects (user override wins)

**Parameter names:**

- Use the parameter name as it appears in the CLI (after click's conversion)
- Boolean flags: use `connect = true` or `connect = false` (not `--connect`/`--no-connect`)

**Scope:**

Command defaults are particularly useful for:

- Project-specific workflows (e.g., always use docker for this project)
- Personal preferences (e.g., prefer fantasy names over english)
- Team conventions (e.g., standard provider or host settings)

**Note:** Some CLI arguments (like `--context`) affect which config file is loaded, so they are parsed before config defaults are applied. The implementation handles this correctly by loading the config first, then applying defaults only to parameters that weren't explicitly specified.

### Create Templates

Templates provide named presets of create command arguments that can be quickly applied using `--template <name>` (or `-t <name>`). This is particularly useful when working with different providers where paths, environment variables, or other settings need to be different.

**How it works:**

- Templates are defined in config files under `[create_templates.<template_name>]` sections
- When using `--template <name>` (or `-t <name>`), all template options are applied as defaults
- Multiple templates can be specified and are applied in order, stacking their values (e.g., `-t modal -t codex`)
- When templates overlap on the same parameter, later templates override earlier ones
- CLI arguments still take precedence over all template values
- Templates from multiple config files with the same name are merged (later configs override earlier ones)

**Example:**

```toml
# .mng/settings.toml

# Template for running agents in Modal
[create_templates.modal]
provider = "modal"
target_path = "/root/workspace"
idle_timeout = 3600

# Template for running agents in Docker
[create_templates.docker]
provider = "docker"
target_path = "/workspace"

# Template with extra commands for development
[create_templates.dev]
provider = "modal"
extra_window = ["server=npm run dev", "logs=tail -f /var/log/app.log"]
```

**Usage:**

```bash
# Use the modal template
mng create my-agent --template modal

# Use the docker template (short form)
mng create my-agent -t docker

# Template with CLI override
mng create my-agent --template modal --idle-timeout 7200

# Stack multiple templates (host config + agent config)
mng create my-agent -t modal -t dev
```

**Why use templates?**

Templates are useful when:

- Composing "what" to run (agent args) with "where" to run it (host config) without creating a template for every combination
- Working with multiple providers that have different path conventions
- Setting up common configurations that you use frequently
- Sharing consistent settings across a team without repeating CLI arguments
- Creating environment-specific presets (development, staging, production)

Templates differ from command defaults in that they must be explicitly selected with `--template`, while command defaults are always applied automatically.

## See Also

- [Agent Types](./concepts/agent_types.md) - Creating custom agent types and overriding defaults
- [Plugins](./concepts/plugins.md) - Extending mng with code
- [Provisioning](./concepts/provisioning.md) - Customizing agent setup
