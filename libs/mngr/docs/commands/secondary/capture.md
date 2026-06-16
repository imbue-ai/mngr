<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr capture

**Synopsis:**

```text
mngr capture [AGENT] [--full] [--window WINDOW] [--start/--no-start]
```

Capture and display an agent's tmux pane content.

Captures the current tmux pane content for the specified agent and
prints it to stdout. Useful for debugging agent state without connecting
to the agent's terminal.

By default, captures only the visible pane content. Use --full to capture
the entire scrollback buffer.

By default, captures the agent's primary window. Use --window to capture a
different tmux window in the agent's session, by index (e.g. 1) or name.

If no agent is specified and running interactively, shows a selector.

**Usage:**

```text
mngr capture [OPTIONS] [AGENT]
```
## Arguments

- `AGENT`: The agent (optional)

**Options:**

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start the host and agent if offline/stopped | `True` |
| `--full`, `--no-full` | boolean | Capture the full scrollback buffer instead of just the visible pane | `False` |
| `--window`, `-w` | text | tmux window (index or name) to capture, instead of the agent's primary window | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr connect](../primary/connect.md) - Connect to an agent interactively
- [mngr exec](../primary/exec.md) - Execute a shell command on an agent's host

## Examples

**Capture visible pane content**

```bash
$ mngr capture my-agent
```

**Capture full scrollback buffer**

```bash
$ mngr capture my-agent --full
```

**Capture a specific tmux window**

```bash
$ mngr capture my-agent --window 1
```

**Capture without auto-starting**

```bash
$ mngr capture my-agent --no-start
```
