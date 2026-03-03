<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng destroy

**Synopsis:**

```text
mng [destroy|rm] [AGENTS...] [--agent <AGENT>] [--all] [--session <SESSION>] [-f|--force] [--dry-run] [-b|--remove-created-branch]
```

Destroy agent(s) and clean up resources.

When the last agent on a host is destroyed, the host itself is also destroyed
(including containers, volumes, snapshots, and any remote infrastructure).

Use with caution! This operation is irreversible.

By default, running agents cannot be destroyed. Use --force to stop and destroy
running agents. The command will prompt for confirmation before destroying
agents unless --force is specified.

Supports custom format templates via --format. Available fields: name.

Alias: rm

**Usage:**

```text
mng destroy [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: The agents (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to destroy (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Destroy all agents | `False` |
| `--session` | text | Tmux session name to destroy (can be specified multiple times). The agent name is extracted by stripping the configured prefix from the session name. | None |
| `--include` | text | Filter agents to destroy by CEL expression (repeatable). [future] | None |
| `--exclude` | text | Exclude agents matching CEL expression from destruction (repeatable). [future] | None |
| `--stdin` | boolean | Read agent names/IDs from stdin, one per line. [future] | `False` |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force` | boolean | Skip confirmation prompts and force destroy running agents | `False` |
| `--dry-run` | boolean | Show what would be destroyed without actually destroying | `False` |
| `--gc`, `--no-gc` | boolean | Run garbage collection after destroying agents to clean up orphaned resources (default: enabled) | `True` |
| `-b`, `--remove-created-branch` | boolean | Delete the git branch that mng created for the agent's work directory | `False` |
| `--allow-worktree-removal`, `--no-allow-worktree-removal` | boolean | Allow removal of the git worktree directory (default: enabled) | `True` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Related Documentation

- [Resource Cleanup Options](../generic/resource_cleanup.md) - Control which associated resources are destroyed
- [Multi-target Options](../generic/multi_target.md) - Behavior when targeting multiple agents

## See Also

- [mng create](./create.md) - Create a new agent
- [mng list](./list.md) - List existing agents
- [mng gc](../secondary/gc.md) - Garbage collect orphaned resources

## Examples

**Destroy an agent by name**

```bash
$ mng destroy my-agent
```

**Destroy multiple agents**

```bash
$ mng destroy agent1 agent2 agent3
```

**Destroy all agents**

```bash
$ mng destroy --all --force
```

**Preview what would be destroyed**

```bash
$ mng destroy my-agent --dry-run
```

**Destroy using --agent flag (repeatable)**

```bash
$ mng destroy --agent my-agent --agent another-agent
```

**Destroy by tmux session name**

```bash
$ mng destroy --session mng-my-agent
```

**Custom format template output**

```bash
$ mng destroy --all --force --format '{name}'
```
