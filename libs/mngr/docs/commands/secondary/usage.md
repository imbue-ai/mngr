<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr usage

**Synopsis:**

```text
mngr usage [OPTIONS]
```

Show rolling-window usage / quota data from agent statusline events.

Reports rolling-window usage / quota data captured by an agent's
statusline.

This command is agent-agnostic: it walks ``<host_dir>/agents/*/events/<source>/
rate_limits/events.jsonl`` and renders the most recent event. The pattern
mirrors how ``mngr transcript`` discovers ``common_transcript`` events --
writer plugins emit events to the conventional path; ``mngr usage`` discovers
them automatically without any agent-specific knowledge.

**Usage:**

```text
mngr usage [OPTIONS]
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
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--max-age` | text | Stale-warning threshold (e.g. '300', '5m', '2h'). Default: from plugin config. | None |

## Examples

**Show current usage**

```bash
$ mngr usage
```

**Treat the snapshot as stale after 60s (warning only)**

```bash
$ mngr usage --max-age 60
```

**Machine-readable output**

```bash
$ mngr usage --format json
```

**Custom format template**

```bash
$ mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'
```
