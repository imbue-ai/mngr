<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr autocompact

**Synopsis:**

```text
mngr autocompact (check|run) [TARGET] [OPTIONS]
```

Automatic context compaction commands for conversational agents.

Manage automatic context compaction for conversational agents that support context compaction (such as Claude Code agents).

Compaction checks evaluate agent staleness based on prompt cache TTL and context token thresholds. When an agent is running, idle, and near or past cache expiration, context compaction is triggered to reduce prompt token usage and turn latency.

**Usage:**

```text
mngr autocompact [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mngr autocompact check

Check agent(s) and report which would be compacted if idle past cache TTL.

Evaluate running conversational agents and report which agents are idle past cache TTL and would be compacted.

Either a specific agent target or the --all flag must be provided.

**Usage:**

```text
mngr autocompact check [OPTIONS] [TARGET]
```
**Options:**

## Check options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--all` | boolean | Check all running agents across all hosts. | `False` |

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

**Check a specific agent**

```bash
$ mngr autocompact check my-agent
```

**Check all running agents across all online hosts**

```bash
$ mngr autocompact check --all
```

**Output check results in JSON format**

```bash
$ mngr autocompact check --all --format json
```

## mngr autocompact run

Evaluate agent(s) and trigger context compaction if idle past cache TTL.

Evaluate running conversational agents and trigger context compaction if idle past cache TTL.

Either a specific agent target or the --all flag must be provided.

**Usage:**

```text
mngr autocompact run [OPTIONS] [TARGET]
```
**Options:**

## Run options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--all` | boolean | Run compaction on all eligible running agents across all hosts. | `False` |

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

**Compact a specific agent if stale**

```bash
$ mngr autocompact run my-agent
```

**Compact all running agents across all online hosts if stale**

```bash
$ mngr autocompact run --all
```

**Output results in JSON format**

```bash
$ mngr autocompact run --all --format json
```

## See Also

- [mngr message](./message.md) - Send a message or prompt to an agent
- [mngr config](./config.md) - View or set autocompact configuration options

## Examples

**Check which agents would be compacted**

```bash
$ mngr autocompact check --all
```

**Run compaction on all stale agents**

```bash
$ mngr autocompact run --all
```

**Check a specific agent**

```bash
$ mngr autocompact check my-agent
```

**Run compaction on a specific agent**

```bash
$ mngr autocompact run my-agent
```

**Output results in JSON format**

```bash
$ mngr autocompact run --all --format json
```
