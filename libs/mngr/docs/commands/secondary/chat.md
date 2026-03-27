<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr chat

**Synopsis:**

```text
mngr chat [OPTIONS] [AGENT]
```

Chat with a mind agent.

Opens an interactive chat session with a mind agent's conversation
system. This connects to the agent's chat.sh script, which manages
conversations backed by the llm CLI tool.

If no agent is specified, shows an interactive selector to choose from
available agents.

If no conversation option is specified (--new, --last, or --conversation),
shows an interactive selector to choose from existing conversations or
start a new one.

The agent can be specified as a positional argument or via --agent:
  mngr chat my-agent
  mngr chat --agent my-agent

**Usage:**

```text
mngr chat [OPTIONS] [AGENT]
```
## Arguments

- `AGENT`: The agent (optional)

**Options:**

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | The agent to chat with (by name or ID) | None |
| `--start`, `--no-start` | boolean | Automatically start the agent if stopped | `True` |

## Chat Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--new` | boolean | Start a new conversation | `False` |
| `--last` | boolean | Resume the most recently updated conversation | `False` |
| `--conversation` | text | Resume a specific conversation by ID | None |
| `--name` | text | Name for the conversation (used with --new) | None |

## SSH Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--allow-unknown-host`, `--no-allow-unknown-host` | boolean | Allow connecting to hosts without a known_hosts file (disables SSH host key verification) | `False` |

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

- [mngr connect](../primary/connect.md) - Connect to an agent's tmux session
- [mngr message](./message.md) - Send a message to an agent
- [mngr exec](../primary/exec.md) - Execute a command on an agent's host

## Examples

**Start a new named conversation**

```bash
$ mngr chat my-agent --new --name "Bug triage"
```

**Resume the most recent conversation**

```bash
$ mngr chat my-agent --last
```

**Resume a specific conversation**

```bash
$ mngr chat my-agent --conversation conv-1234567890-abcdef
```

**Show interactive agent selector**

```bash
$ mngr chat
```

**Show interactive conversation selector**

```bash
$ mngr chat my-agent
```
