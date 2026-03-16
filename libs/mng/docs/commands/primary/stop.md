<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng stop

**Synopsis:**

```text
mng [stop|s] [AGENTS...] [--agent <AGENT>] [--all] [--session <SESSION>] [--archive] [--dry-run] [--snapshot-mode <MODE>] [--graceful/--no-graceful]
```

Stop running agent(s).

For remote hosts, this stops the agent's tmux session. The host remains
running unless idle detection stops it automatically.

For local agents, this stops the agent's tmux session. The local host
itself cannot be stopped (if you want that, shut down your computer).

Use --archive to also set an 'archived_at' label on each stopped agent.
This marks the agent as archived without destroying it, allowing it to
be filtered out of listings while preserving its state. The 'mng archive'
command is a shorthand for 'mng stop --archive'.

Supports custom format templates via --format. Available fields: name.

Alias: s

**Usage:**

```text
mng stop [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: The agents (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to stop (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Stop all running agents | `False` |
| `--session` | text | Tmux session name to stop (can be specified multiple times). The agent name is extracted by stripping the configured prefix from the session name. | None |
| `--include` | text | Filter agents to stop by CEL expression (repeatable) [future] | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) [future] | None |
| `--stdin` | boolean | Read agent and host names/IDs from stdin, one per line [future] | `False` |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--archive` | boolean | Set an 'archived_at' label on each stopped agent (marks it as archived) | `False` |
| `--dry-run` | boolean | Show what would be stopped without actually stopping | `False` |
| `--snapshot-mode` | choice (`auto` &#x7C; `always` &#x7C; `never`) | Control snapshot creation when stopping: auto (snapshot if needed), always, or never [future] | None |
| `--graceful`, `--no-graceful` | boolean | Wait for agent to reach a clean state before stopping [future] | `True` |
| `--graceful-timeout` | text | Timeout for graceful stop (e.g., 30s, 5m) [future] | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mng start](./start.md) - Start stopped agents
- [mng connect](./connect.md) - Connect to an agent
- [mng list](./list.md) - List existing agents
- [mng archive](../aliases/archive.md) - Stop and archive agents (shorthand for stop --archive)

## Examples

**Stop an agent by name**

```bash
$ mng stop my-agent
```

**Stop multiple agents**

```bash
$ mng stop agent1 agent2
```

**Stop all running agents**

```bash
$ mng stop --all
```

**Stop and archive an agent**

```bash
$ mng stop my-agent --archive
```

**Stop by tmux session name**

```bash
$ mng stop --session mng-my-agent
```

**Preview what would be stopped**

```bash
$ mng stop --all --dry-run
```

**Custom format template output**

```bash
$ mng stop --all --format '{name}'
```
