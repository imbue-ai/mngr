<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr start

**Synopsis:**

```text
mngr start [AGENTS...|-] [--agent <AGENT>] [--all] [--host <HOST>] [--connect] [--dry-run]
```

Start stopped agent(s).

For remote hosts, this restores from the most recent snapshot and starts
the container/instance. For local agents, this starts the agent's tmux
session.

If multiple agents share a host, they will all be started together when
the host starts.

Supports custom format templates via --format. Available fields: name.

**Usage:**

```text
mngr start [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: The agents (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to start (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Start all stopped agents | `False` |
| `--host` | text | Host(s) to start all stopped agents on [repeatable] [future] | None |
| `--include` | text | Filter agents and hosts to start by CEL expression (repeatable) [future] | None |
| `--exclude` | text | Exclude agents and hosts matching CEL expression (repeatable) [future] | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--dry-run` | boolean | Show what would be started without actually starting | `False` |
| `--connect`, `--no-connect` | boolean | Connect to the agent after starting (only valid for single agent) | `False` |
| `--connect-command` | text | Command to run instead of the builtin connect. MNGR_AGENT_NAME and MNGR_SESSION_NAME env vars are set. | None |

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

## See Also

- [mngr stop](./stop.md) - Stop running agents
- [mngr connect](./connect.md) - Connect to an agent
- [mngr list](./list.md) - List existing agents

## Examples

**Start an agent by name**

```bash
$ mngr start my-agent
```

**Start multiple agents**

```bash
$ mngr start agent1 agent2
```

**Start and connect**

```bash
$ mngr start my-agent --connect
```

**Start all stopped agents**

```bash
$ mngr start --all
```

**Preview what would be started**

```bash
$ mngr start --all --dry-run
```

**Custom format template output**

```bash
$ mngr start --all --format '{name}'
```
