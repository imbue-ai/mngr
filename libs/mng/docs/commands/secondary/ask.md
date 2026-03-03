<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng ask

**Synopsis:**

```text
mng ask [--execute] QUERY...
```

Chat with mng for help [experimental].

Ask a question and mng will generate the appropriate CLI command.
If no query is provided, shows general help about available commands
and common workflows.

When --execute is specified, the generated CLI command is executed
directly instead of being printed.

**Usage:**

```text
mng ask [OPTIONS] [QUERY]...
```
## Arguments

- `QUERY`: The query (optional)

**Options:**

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--execute` | boolean | Execute the generated CLI command instead of just printing it | `False` |

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

- [mng create](../primary/create.md) - Create an agent
- [mng list](../primary/list.md) - List existing agents
- [mng connect](../primary/connect.md) - Connect to an agent

## Examples

**Ask a question**

```bash
$ mng ask "how do I create an agent?"
```

**Ask without quotes**

```bash
$ mng ask start a container with claude code
```

**Execute the generated command**

```bash
$ mng ask --execute forward port 8080 to the public internet
```
