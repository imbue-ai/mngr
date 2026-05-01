<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr usage

**Synopsis:**

```text
mngr usage [OPTIONS]
```

Show Claude Code rolling-window quota usage (5h, 7d, overage).

Reports Claude Code's rolling 5-hour, 7-day, and overage quota windows.

The data is sourced from response headers on every Claude Code API call and
captured into a shared cache by per-agent statusline shims. When the cache is
stale, `mngr usage` (by default) spawns a brief `claude -p` call to refresh it
(approx $0.005 per refresh).

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
| `--refresh` | boolean | Force a refresh probe even if the cache is fresh. Spawns `claude -p` (~$0.005). | `False` |
| `--max-age` | text | Override the freshness threshold (e.g. '300', '5m', '2h'). Default: from plugin config. | None |

## Examples

**Show current usage**

```bash
$ mngr usage
```

**Force a refresh**

```bash
$ mngr usage --refresh
```

**Treat the cache as stale after 60s**

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
