<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr exec

**Synopsis:**

```text
mngr [exec|x] [AGENTS...|-] COMMAND [--agent <AGENT>] [--user <USER>] [--cwd <DIR>] [--timeout <SECONDS>] [--on-error <MODE>]
```

Execute a shell command on one or more agents' hosts.

The command runs in each agent's work_dir by default. Use --cwd to override
the working directory.

The command's stdout is printed to stdout and stderr to stderr. The exit
code is 0 if all commands succeeded, 1 if any failed.

Use '-' in place of agent names to read them from stdin, one per line.

Supports custom format templates via --format. Available fields: agent, stdout, stderr, success.

Alias: x

**Usage:**

```text
mngr exec [OPTIONS] [AGENTS]... COMMAND
```
## Arguments

- `AGENTS`: Name(s) or ID(s) of the agent(s) whose host will run the command
- `COMMAND`: Shell command to execute on the agent's host

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to exec on (can be specified multiple times) | None |

## Execution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--user` | text | User to run the command as | None |
| `--cwd` | text | Working directory for the command (default: agent's work_dir) | None |
| `--timeout` | float | Timeout in seconds for the command | None |

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start the host/agent if stopped | `True` |

## Error Handling

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `continue` |

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

- [Multi-target Options](../generic/multi_target.md) - Behavior when targeting multiple agents

## See Also

- [mngr connect](./connect.md) - Connect to an agent interactively
- [mngr message](../secondary/message.md) - Send a message to an agent
- [mngr list](./list.md) - List available agents

## Examples

**Run a command on an agent**

```bash
$ mngr exec my-agent "echo hello"
```

**Run on multiple agents**

```bash
$ mngr exec agent1 agent2 "echo hello"
```

**Run on all agents**

```bash
$ mngr list --ids | mngr exec - "echo hello"
```

**Run with a custom working directory**

```bash
$ mngr exec my-agent "ls -la" --cwd /tmp
```

**Run as a different user**

```bash
$ mngr exec my-agent "whoami" --user root
```

**Run with a timeout**

```bash
$ mngr exec my-agent "sleep 100" --timeout 5
```

**Use --agent flag (repeatable)**

```bash
$ mngr exec --agent my-agent --agent another-agent "echo hello"
```

**Custom format template output**

```bash
$ mngr exec my-agent "hostname" --format '{agent}\t{stdout}'
```
