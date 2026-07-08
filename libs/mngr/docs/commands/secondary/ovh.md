<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr ovh
**Usage:**

```text
mngr ovh [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr ovh list

**Usage:**

```text
mngr ovh list [OPTIONS]
```
**Options:**

## Provider

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Name of the [providers.NAME] block in settings.toml to read defaults from (endpoint, credentials, ovh_subsidiary, project_id). When the block does not exist, OvhProviderConfig class defaults are used as the fallback; credentials still fall back to env / ~/.ovh.conf when unset. | `ovh` |
| `--all` | boolean | List every VPS the account owns, not just those tagged for mngr. By default, only VPSes tagged with `mngr-provider` are shown. | `False` |

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
