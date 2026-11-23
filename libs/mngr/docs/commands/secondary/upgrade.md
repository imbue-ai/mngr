<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr upgrade

**Synopsis:**

```text
mngr upgrade [AGENTS...|-] [--agent <AGENT>] [-f|--force] [--dry-run] [--on-error <MODE>]
```

Upgrade existing agents to match the version of mngr running this 'mngr upgrade' command.

When mngr creates an agent, it writes scripts and hooks into the agent.
Those files stay at the version of mngr that created the agent, while the
mngr you run later reads what they record. `mngr upgrade` rewrites them
to match the version of mngr running the command, so an agent created by
an older mngr works the way the newer one expects, without being
recreated. Only those files change: the agent keeps its options, branch
and conversation.

An idle agent is restarted (closing any attached terminal) and resumes
its conversation. A stopped agent gets the new files and stays stopped.
An agent that is busy, or whose program has exited, is skipped unless
--force.

Each agent is reported as:
- upgraded: its files were rewritten.
- current: nothing to change, so re-running is safe.
- skipped: left as it was; the line says why.
- failed: the line names the step that failed.

Exit codes:
- 0: every agent was upgraded or is current.
- 9: at least one was skipped, and none failed.
- 1: at least one failed.

**Usage:**

```text
mngr upgrade [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: Agent name(s) or ID(s) to upgrade, or `-` to read them from stdin.

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | agent_address | Agent address (NAME[@HOST[.PROVIDER]]) to upgrade (can be specified multiple times) | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force` | boolean | Also upgrade busy agents (mid-turn, on a dialog, running background work, or with another program in their terminal) and agents whose program has exited, restarting them | `False` |
| `--dry-run` | boolean | Show what would be upgraded without changing anything | `False` |

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
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Examples

**Upgrade one agent**

```bash
$ mngr upgrade my-agent
```

**See what it would change, changing nothing**

```bash
$ mngr upgrade my-agent --dry-run
```

**Preview upgrading every local agent**

```bash
$ mngr list --local --ids | mngr upgrade - --dry-run
```

**Upgrade every local agent**

```bash
$ mngr list --local --ids | mngr upgrade -
```

**Upgrade a busy agent, interrupting it**

```bash
$ mngr upgrade my-agent --force
```
