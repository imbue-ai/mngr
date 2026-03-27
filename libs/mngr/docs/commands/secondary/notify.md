<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr notify

**Synopsis:**

```text
mngr notify
```

Notify when agents transition to WAITING.

Sends a desktop notification when any agent transitions from RUNNING to WAITING.

Automatically starts `mngr observe` in the background if it is not already running.

On macOS, notifications are sent via terminal-notifier (install with:
brew install terminal-notifier). On Linux, via notify-send (libnotify).

To enable click-to-connect (opens a terminal tab running mngr connect),
configure the plugin in settings.toml:

[plugins.notifications]
    terminal_app = "iTerm"

Or use a custom command (MNGR_AGENT_NAME is set in the environment):

[plugins.notifications]
    custom_terminal_command = "my-terminal -e mngr connect $MNGR_AGENT_NAME"

Press Ctrl+C to stop.

**Usage:**

```text
mngr notify [OPTIONS]
```
**Options:**

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

- [mngr observe](./observe.md) - Stream agent state changes to local event files
- [mngr list](../primary/list.md) - List agents to see their current state

## Examples

**Notify on all agents**

```bash
$ mngr notify
```
