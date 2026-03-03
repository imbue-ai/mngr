<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng rename

**Synopsis:**

```text
mng [rename|mv] <CURRENT> <NEW-NAME> [--dry-run] [--host]
```

Rename an agent or host [experimental].

Updates the agent's name in its data.json and renames the tmux session
if the agent is currently running. Git branch names are not renamed.

If a previous rename was interrupted (e.g., the tmux session was renamed
but data.json was not updated), re-running the command will attempt
to complete it.

Alias: mv

**Usage:**

```text
mng rename [OPTIONS] CURRENT NEW-NAME
```
## Arguments

- `CURRENT`: Current name or ID of the agent to rename
- `NEW-NAME`: New name for the agent

**Options:**

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be renamed without actually renaming | `False` |
| `--host` | boolean | Rename a host instead of an agent [future] | `False` |

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

## See Also

- [mng list](./list.md) - List existing agents
- [mng create](./create.md) - Create a new agent
- [mng destroy](./destroy.md) - Destroy an agent

## Examples

**Rename an agent**

```bash
$ mng rename my-agent new-name
```

**Preview what would be renamed**

```bash
$ mng rename my-agent new-name --dry-run
```

**Use the alias**

```bash
$ mng mv my-agent new-name
```
