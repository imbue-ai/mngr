<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr destroy

**Synopsis:**

```text
mngr [destroy|rm] [AGENTS...|-] [--agent <AGENT>] [--session <SESSION>] [-f|--force] [-b|--remove-created-branch]
```

Destroy agent(s) and clean up resources.

When the last agent on a host is destroyed, the host itself is also destroyed
(including containers, volumes, snapshots, and any remote infrastructure).

Use with caution! This operation is irreversible.

By default, running agents cannot be destroyed. Use --force to stop and destroy
running agents. The command will prompt for confirmation before destroying
agents unless --force is specified.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: name.

Alias: rm

**Usage:**

```text
mngr destroy [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: The agents (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to destroy (can be specified multiple times) | None |
| `--session` | text | Tmux session name to destroy (can be specified multiple times). The agent name is extracted by stripping the configured prefix from the session name. | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force` | boolean | Skip confirmation prompts and force destroy running agents | `False` |
| `--gc`, `--no-gc` | boolean | Run garbage collection after destroying agents to clean up orphaned resources (default: enabled) | `True` |
| `-b`, `--remove-created-branch` | boolean | Delete the git branch that mngr created for the agent's work directory | `False` |
| `--allow-worktree-removal`, `--no-allow-worktree-removal` | boolean | Allow removal of the git worktree directory (default: enabled) | `True` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Related Documentation

- [Resource Cleanup Options](../generic/resource_cleanup.md) - Control which associated resources are destroyed
- [Multi-target Options](../generic/multi_target.md) - Behavior when targeting multiple agents

## See Also

- [mngr create](./create.md) - Create a new agent
- [mngr list](./list.md) - List existing agents
- [mngr gc](../secondary/gc.md) - Garbage collect orphaned resources

## Examples

**Destroy an agent by name**

```bash
$ mngr destroy my-agent
```

**Destroy multiple agents**

```bash
$ mngr destroy agent1 agent2 agent3
```

**Destroy all agents**

```bash
$ mngr list --ids | mngr destroy - --force
```

**Destroy using --agent flag (repeatable)**

```bash
$ mngr destroy --agent my-agent --agent another-agent
```

**Destroy by tmux session name**

```bash
$ mngr destroy --session mngr-my-agent
```

**Pipe agent names from list**

```bash
$ mngr list --ids | mngr destroy - --force
```

**Custom format template output**

```bash
$ mngr destroy my-agent --force --format '{name}'
```
