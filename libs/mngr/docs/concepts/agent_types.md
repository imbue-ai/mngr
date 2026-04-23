# Agent Types

An agent type is a named configuration that tells `mngr` how to set up and run a particular kind of [agent](./agents.md).

```bash
mngr create my-agent claude        # "claude" is the agent type
mngr create my-agent codex         # "codex" is the agent type
```

To run a literal shell command, use the built-in `command` agent type and pass the command after `--`:

```bash
mngr create my-task --type command -- python -m http.server 8080
```

Agent types are registered by [plugins](./plugins.md) or defined in your config, and can specify:

- Command to run (e.g., `claude`, `codex`)
- Environment variables (API keys, model selection, feature flags)
- Provisioning steps (install Node.js, configure auth)
- Default settings (idle timeout, activity mode)
- CLI arguments (additional flags for `mngr create`)

## Resolution

When you run `mngr create my-agent <type>` (or `mngr create my-agent --type <type>`):

1. **Custom type lookup**: If you defined `<type>` in your config, use that configuration
2. **Plugin lookup**: If a plugin registered `<type>` as an agent type, use its configuration

If `<type>` is not found in either, `mngr create` fails. Use `--type command -- <shell command>` to run an arbitrary command without registering a type.

## Custom Agent Types

You can define your own agent types in your config file without writing a plugin. Custom types inherit from an existing type and override specific settings.

Define a custom type in your config (run `mngr config edit`):

```toml
[agent_types.my_claude]
parent_type = "claude"
cli_args = "--env CLAUDE_MODEL=opus"
permissions = ["github"]  # [future] permissions field parsed but not applied
```

Then use it like any built-in type:

```bash
mngr create my-agent my_claude
```

Custom types can be scoped to a project by using `mngr config edit --scope project`. This is useful for project-specific configurations that shouldn't apply globally.

For a reusable shortcut that runs a fixed shell command (instead of repeating `--type command -- ...` each time), set `parent_type = "command"` on a custom type and pin the command in config:

```toml
[agent_types.my_server]
parent_type = "command"
command = "python -m http.server 8080"
```

```bash
mngr create my-task my_server
```

### Available Settings

- `command`: literal shell command to run as the agent. Typically set alongside `parent_type = "command"` to pin a fixed command for a reusable custom type, or alongside another `parent_type` to override the command inherited from that parent. Arguments passed after `--` at invocation time are appended to this command.
- `cli_args`: configure any option found in the [`mngr create` command](../commands/primary/create.md) by just adding the corresponding flags.
- `permissions`: an *explicit* list of permissions for the agent (overrides any permissions from the parent type). Is applied before `cli_args`.

Because it would be confusing to merge or replace `cli_args`, it is invalid to set `parent_type` to a custom type--just use the base type directly.

### When to Use Custom Types vs Plugins

Use custom types when you want to:

- Bundle commonly-used flags into a reusable shortcut
- Share configuration across machines or with teammates (via project config)
- Set up project-specific agent configurations

Use a [plugin](./plugins.md) when you need to:

- Add custom provisioning logic (beyond shell commands)
- Register hooks for lifecycle events
- Define entirely new agent commands

## Discovering Types

Agent types come from installed plugins and your config:

```bash
mngr plugin list    # Shows installed plugins (listing agent types per plugin [future])
```

Built-in plugins provide `claude`, `codex`, and `command` by default.
