<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr cleanup

**Synopsis:**

```text
mngr [cleanup|clean] [--destroy|--stop] [--older-than DURATION] [--idle-for DURATION] [--provider PROVIDER] [--type TYPE] [--host-label KEY=VALUE] [-f|--force|--yes] [--dry-run]
```

Destroy or stop agents and hosts to free up resources [experimental].

When running in a pty, defaults to providing an interactive interface for
reviewing running agents and hosts and selecting which ones to destroy or stop.

When running in a non-interactive setting (or if --yes is provided), will
destroy all selected agents/hosts without prompting.

Convenience filters like --older-than and --idle-for are translated into CEL
expressions internally, so they can be combined with --include and --exclude
for precise control.

For automatic garbage collection of unused resources without interaction,
see `mngr gc`.

Alias: clean

**Usage:**

```text
mngr cleanup [OPTIONS]
```
**Options:**

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force`, `--yes` | boolean | Skip confirmation prompts | `False` |
| `--dry-run` | boolean | Show what would be destroyed or stopped without executing | `False` |

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include only agents matching this CEL filter (repeatable) | None |
| `--exclude` | text | Exclude agents matching this CEL filter (repeatable) | None |
| `--older-than` | text | Select agents older than specified duration (e.g., 7d, 24h) | None |
| `--idle-for` | text | Select agents idle for at least this duration (e.g., 1h, 30m) | None |
| `--host-label` | text | Select agents/hosts with this host label (repeatable) | None |
| `--provider` | text | Select hosts from this provider (repeatable) | None |
| `--type` | text | Select this agent type, e.g., claude, codex (repeatable) | None |

## Actions

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--action` | choice (`destroy` &#x7C; `stop`) | Action to perform on selected agents | `destroy` |
| `--destroy` | boolean | Destroy selected agents/hosts (default) | None |
| `--stop` | boolean | Stop selected agents instead of destroying | None |
| `--snapshot-before` | boolean | Create snapshots before destroying or stopping [future] | `False` |

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

- [mngr destroy](../primary/destroy.md) - Destroy specific agents by name
- [mngr stop](../primary/stop.md) - Stop specific agents by name
- [mngr gc](./gc.md) - Garbage collect orphaned resources
- [mngr list](../primary/list.md) - List agents with filtering

## Examples

**Interactive cleanup (default)**

```bash
$ mngr cleanup
```

**Preview what would be destroyed**

```bash
$ mngr cleanup --dry-run --yes
```

**Destroy agents older than 7 days**

```bash
$ mngr cleanup --older-than 7d --yes
```

**Stop idle agents**

```bash
$ mngr cleanup --stop --idle-for 1h --yes
```

**Destroy Docker agents only**

```bash
$ mngr cleanup --provider docker --yes
```

**Destroy by agent type**

```bash
$ mngr cleanup --type codex --yes
```
