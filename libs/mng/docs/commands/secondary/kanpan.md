<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng kanpan

**Synopsis:**

```text
mng kanpan [OPTIONS]
```

TUI board showing agents grouped by lifecycle state with PR status.

Launches a terminal UI that displays all mng agents organized by their
lifecycle state (RUNNING, WAITING, STOPPED, DONE, REPLACED).

Each agent shows its name, current state, and associated GitHub PR information
including PR number, state (open/closed/merged), and CI check status.

The display auto-refreshes every 10 minutes. Press 'r' to refresh manually,
or 'q' to quit.

Supports CEL filtering via --include/--exclude and a --project convenience flag.

Requires the gh CLI to be installed and authenticated for GitHub PR information.

**Usage:**

```text
mng kanpan [OPTIONS]
```
**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--project` | text | Show only agents with this project label (repeatable) | None |

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

- [mng list](../primary/list.md) - List agents

## Examples

**Launch the kanpan board**

```bash
$ mng kanpan
```

**Show only agents for a specific project**

```bash
$ mng kanpan --project mng
```

**Show only running agents**

```bash
$ mng kanpan --include 'state == "RUNNING"'
```
