<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng connect

**Synopsis:**

```text
mng [connect|conn] [OPTIONS] [AGENT]
```

Connect to an existing agent via the terminal.

Attaches to the agent's tmux session, roughly equivalent to SSH'ing into
the agent's machine and attaching to the tmux session.

If no agent is specified, shows an interactive selector to choose from
available agents. The selector allows typeahead search to filter agents
by name.

The agent can be specified as a positional argument or via --agent:
  mng connect my-agent
  mng connect --agent my-agent

Alias: conn

**Usage:**

```text
mng connect [OPTIONS] [AGENT]
```
## Arguments

- `AGENT`: The agent (optional)

**Options:**

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | The agent to connect to (by name or ID) | None |
| `--start`, `--no-start` | boolean | Automatically start the agent if stopped | `True` |

## Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--reconnect`, `--no-reconnect` | boolean | Automatically reconnect if dropped [future] | `True` |
| `--message` | text | Initial message to send after connecting [future] | None |
| `--message-file` | path | File containing initial message to send [future] | None |
| `--ready-timeout` | float | Timeout in seconds to wait for agent readiness [future] | `10.0` |
| `--retry` | integer | Number of connection retries [future] | `3` |
| `--retry-delay` | text | Delay between retries [future] | `5s` |
| `--attach-command` | text | Command to run instead of attaching to main session [future] | None |
| `--allow-unknown-host`, `--no-allow-unknown-host` | boolean | Allow connecting to hosts without a known_hosts file (disables SSH host key verification) | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
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

- [mng create](./create.md) - Create and connect to a new agent
- [mng list](./list.md) - List available agents

## Examples

**Connect to an agent by name**

```bash
$ mng connect my-agent
```

**Connect without auto-starting if stopped**

```bash
$ mng connect my-agent --no-start
```

**Show interactive agent selector**

```bash
$ mng connect
```
