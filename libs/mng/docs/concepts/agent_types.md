# Agent Types

An agent type is a named configuration that tells `mng` how to set up and run a particular kind of [agent](./agents.md).

```bash
mng create my-agent claude        # "claude" is the agent type
mng create my-agent codex         # "codex" is the agent type
mng create my-agent ./my-script   # any command can be an agent type
```

Alternatively, you can use `--command` to run a literal command directly without specifying an agent type:

```bash
mng create my-agent --command "sleep 1000"   # run a literal command
```

Using `--command` implicitly uses the "generic" agent type, which simply runs the provided command. This means `--command` and `--type` are mutually exclusive.

Agent types include any program in your `PATH`, as well as types registered by [plugins](./plugins.md), which can also specify:

- Command to run (e.g., `claude`, `codex`)
- Environment variables (API keys, model selection, feature flags)
- Provisioning steps (install Node.js, configure auth)
- Default settings (idle timeout, activity mode)
- CLI arguments (additional flags for `mng create`)

## Resolution

When you run `mng create my-agent <type>` (or `mng create my-agent --type <type>`):

1. **Custom type lookup**: If you defined `<type>` in your config, use that configuration
2. **Plugin lookup**: If a plugin registered `<type>` as an agent type, use its configuration
3. **Direct command**: Otherwise, treat `<type>` as a command to run

This fallback lets you run any program as an agent without needing a plugin or custom type.

## Custom Agent Types

You can define your own agent types in your config file without writing a plugin. Custom types inherit from an existing type and override specific settings.

Define a custom type in your config (run `mng config edit`):

```toml
[agent_types.my_claude]
parent_type = "claude"
cli_args = "--env CLAUDE_MODEL=opus"
permissions = ["github"]  # [future] permissions field parsed but not applied
```

Then use it like any built-in type:

```bash
mng create my-agent my_claude
```

Custom types can be scoped to a project by using `mng config edit --scope project`. This is useful for project-specific configurations that shouldn't apply globally.

### Available Settings

- `cli_args`: configure any option found in the [`mng create` command](../commands/primary/create.md) by just adding the corresponding flags.
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
mng plugin list    # Shows installed plugins (listing agent types per plugin [future])
```

Built-in plugins provide `claude` and `codex` by default.
