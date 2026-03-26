<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng capture

**Synopsis:**

```text
mng capture [AGENT] [--full] [--start/--no-start]
```

Capture and display an agent's tmux pane content.

Captures the current tmux pane content for the specified agent and
prints it to stdout. Useful for debugging agent state without connecting
to the agent's terminal.

By default, captures only the visible pane content. Use --full to capture
the entire scrollback buffer.

If no agent is specified and running interactively, shows a selector.

**Usage:**

```text
mng capture [OPTIONS] [AGENT]
```
## Arguments

- `AGENT`: The agent (optional)

**Options:**

## General

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--start`, `--no-start` | boolean | Automatically start the host/agent if stopped | `True` |
| `--full`, `--no-full` | boolean | Capture the full scrollback buffer instead of just the visible pane | `False` |

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

- [mng connect](../primary/connect.md) - Connect to an agent interactively
- [mng exec](../primary/exec.md) - Execute a shell command on an agent's host

## Examples

**Capture visible pane content**

```bash
$ mng capture my-agent
```

**Capture full scrollback buffer**

```bash
$ mng capture my-agent --full
```

**Capture without auto-starting**

```bash
$ mng capture my-agent --no-start
```
