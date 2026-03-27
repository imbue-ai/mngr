<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr label

**Synopsis:**

```text
mngr label [AGENTS...|-] [--agent <AGENT>] [--all] -l KEY=VALUE [-l KEY=VALUE ...]
```

Set labels on agents.

Labels are key-value pairs attached to agents. They are stored in the
agent's certified data and persist across restarts.

Labels are merged with existing labels: new keys are added and existing
keys are updated. To see current labels, use 'mngr list'.

Works with both online and offline agents. For offline hosts, labels
are updated directly in the provider's persisted data without requiring
the host to be started.

**Usage:**

```text
mngr label [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: Agent name(s) or ID(s) to label. Use '-' to read from stdin (one per line).

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to label (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Apply labels to all agents | `False` |

## Labels

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-l`, `--label` | text | Label in KEY=VALUE format (repeatable) | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be labeled without actually labeling | `False` |

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
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr list](../primary/list.md) - List agents and their labels
- [mngr create](../primary/create.md) - Create an agent with labels

## Examples

**Set a label on an agent**

```bash
$ mngr label my-agent --label archived_at=2026-03-15
```

**Set multiple labels on multiple agents**

```bash
$ mngr label agent1 agent2 -l env=prod -l team=backend
```

**Label all agents**

```bash
$ mngr label --all --label project=myproject
```

**Read agent names from stdin**

```bash
$ mngr list --format '{name}' | mngr label - -l reviewed=true
```

**Preview changes**

```bash
$ mngr label my-agent --label status=done --dry-run
```
