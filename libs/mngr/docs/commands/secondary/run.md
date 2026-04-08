<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr run

**Synopsis:**

```text
mngr run AGENT_TYPE [-c COMMAND] [-- AGENT_ARGS...]
```

Run a headless agent and stream its output.

Run a headless agent of any type that supports streaming output,
stream the output to stdout, and destroy the agent when done.

Use --command (-c) to specify the shell command for headless_command agents.
Use -- to pass additional arguments directly to the agent.

**Usage:**

```text
mngr run [OPTIONS] AGENT_TYPE [AGENT_ARGS]...
```
## Arguments

AGENT_TYPE is the headless agent type to run (e.g. headless_command, headless_claude).
AGENT_ARGS are additional arguments passed through to the agent after --.

**Options:**

## Execution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--command`, `-c` | text | Shell command for the agent to run (used by headless_command agent type) | None |

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

- [mngr ask](./ask.md) - Ask mngr for help (headless_claude with built-in system prompt)
- [mngr create](../primary/create.md) - Create a persistent agent
- [mngr exec](../primary/exec.md) - Execute a command on existing agents

## Examples

**Run a shell command**

```bash
$ mngr run headless_command -c "echo hello world"
```

**Run headless claude**

```bash
$ mngr run headless_claude -- "what is 2+2"
```
