<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr usage

**Synopsis:**

```text
mngr usage [OPTIONS] [COMMAND]
```

Show rolling-window usage / quota data from agent statusline events.

Reports rolling-window usage / quota data captured by an agent's
statusline.

Agent-agnostic and host-agnostic: enumerates matching agents via
``list_agents`` (same machinery and filter vocabulary as ``mngr list``) and
reads each agent's ``events/<source>/usage/events.jsonl`` via the
events API. Local and remote agents are read uniformly; the writer plugin
chooses the ``<source>`` segment.

Per-source aggregation:

- Rate-limit windows track an account-level counter, so the freshest reading
  across all agents wins.
- Cost is per-session and resets when a new session starts, so we keep one
  record per ``session_id`` and aggregate across sessions within the
  ``--since`` recency window (default 24h).
- ``current_session`` is the most-recently-updated session in that window.

**Usage:**

```text
mngr usage [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--running` | boolean | Show only running agents (alias for --include 'state == "RUNNING"') | `False` |
| `--stopped` | boolean | Show only stopped agents (alias for --include 'state == "STOPPED"') | `False` |
| `--archived` | boolean | Show only archived agents (alias for --include 'has(labels.archived_at)') | `False` |
| `--active` | boolean | Show only active agents (anything not archived/destroyed/crashed/failed) | `False` |
| `--local` | boolean | Show only local agents (alias for --include 'host.provider == "local"') | `False` |
| `--remote` | boolean | Show only remote agents (alias for --exclude 'host.provider == "local"') | `False` |
| `--project` | text | Show only agents with this project label (repeatable; '.' expands to the current project) | None |
| `--label` | text | Show only agents with this label (format: KEY=VALUE, repeatable) [experimental] | None |
| `--host-label` | text | Show only agents on hosts with this host label (format: KEY=VALUE, repeatable) | None |
| `--provider` | text | Show only agents from the given provider(s) (repeatable, e.g. --provider local) | None |

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
| `--since` | text | Recency window for per-session cost aggregation (e.g. '24h', '7d'). Sessions whose last event is older are dropped from all per-session surfaces (sessions[], current_session, session_count) and from the aggregate cost computed off them. Default: from plugin config (24h). | None |

## mngr usage wait

Block until a usage snapshot matches a CEL predicate.

Polls ``mngr usage`` snapshots until at least one source's CEL
context satisfies every ``--until`` expression. Composable with shell:

mngr usage wait --until 'five_hour.used_percentage < 50 && five_hour.elapsed_percentage > 75' \
      && mngr message my-agent "ok, kick off the next batch"

The CEL context per source mirrors one entry of ``mngr usage --format
json``'s ``sources`` array. Window fields (under each window key, e.g.
``five_hour``):

- ``used_percentage``: from the writer.
- ``resets_at`` / ``seconds_until_reset``: when the window resets.
- ``window_seconds``: window duration (writer-provided; absent for
  variable-duration windows like Claude's overage).
- ``elapsed_seconds`` / ``elapsed_percentage``: derived from
  ``window_seconds`` and ``seconds_until_reset``; absent when
  ``window_seconds`` isn't emitted.

Source-level fields:

- ``cost.total_cost_usd`` / ``cost.total_duration_ms`` / ... : aggregate
  across the recency window (sum across all sessions in the last
  ``--since`` duration).
- ``current_session.session_id``: most recently-active session's UUID.
- ``current_session.cost.total_cost_usd`` / ... : the current session's
  cost reading (for "wait until *this session* crosses $5").
- ``session_count``: number of recent sessions contributing to the cost
  aggregate.
- ``sessions``: full list of session-cost records in the recency window.

Exit codes:
  0 - A source matched all --until filters.
  1 - Error (invalid CEL, interrupt).
  2 - Timed out.

**Usage:**

```text
mngr usage wait [OPTIONS]
```
**Options:**

## Predicate

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--until` | text | CEL expression that must evaluate true for some source to win the wait [repeatable, all must match]. The CEL context is the per-source dict from `mngr usage --format json` (see help description for shape). | None |

## Wait options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--timeout` | text | Maximum time to wait (e.g. '30s', '5m', '1h'). Default: wait forever. | None |
| `--interval` | text | Poll interval (e.g. '15s', '1m'). The usage snapshot is rebuilt every interval. Default of 30s suits multi-hour windows; tighten for short-window predicates. | `30s` |
| `--since` | text | Recency window for per-session cost aggregation (e.g. '24h', '7d'). Affects every per-session surface in the CEL context: `cost.*` (aggregate across recent sessions), `current_session.*` (latest in-window session), `sessions[]`, and `session_count`. Default: from plugin config (24h). | None |

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--running` | boolean | Show only running agents (alias for --include 'state == "RUNNING"') | `False` |
| `--stopped` | boolean | Show only stopped agents (alias for --include 'state == "STOPPED"') | `False` |
| `--archived` | boolean | Show only archived agents (alias for --include 'has(labels.archived_at)') | `False` |
| `--active` | boolean | Show only active agents (anything not archived/destroyed/crashed/failed) | `False` |
| `--local` | boolean | Show only local agents (alias for --include 'host.provider == "local"') | `False` |
| `--remote` | boolean | Show only remote agents (alias for --exclude 'host.provider == "local"') | `False` |
| `--project` | text | Show only agents with this project label (repeatable; '.' expands to the current project) | None |
| `--label` | text | Show only agents with this label (format: KEY=VALUE, repeatable) [experimental] | None |
| `--host-label` | text | Show only agents on hosts with this host label (format: KEY=VALUE, repeatable) | None |
| `--provider` | text | Restrict to agents from the given provider(s) (repeatable, e.g. --provider local). | None |

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


## Examples

**Wait for 75% of the 5h window to elapse while at most 50% of the limit is used**

```bash
$ mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50'
```

**Restrict to Claude usage only (via CEL)**

```bash
$ mngr usage wait --until 'source == "claude" && five_hour.used_percentage < 25'
```

**Bail out after an hour**

```bash
$ mngr usage wait --until 'seven_day.used_percentage < 30' --timeout 1h
```

**Tighter poll for short-window predicates**

```bash
$ mngr usage wait --until 'overage.is_using_overage == false' --interval 10s
```

**Wait until cumulative spend over the last 24h crosses $20**

```bash
$ mngr usage wait --until 'cost.total_cost_usd > 20.0'
```

**Wait until the current session crosses $5**

```bash
$ mngr usage wait --until 'current_session.cost.total_cost_usd > 5.0'
```

**Aggregate cost over the last week instead of 24h**

```bash
$ mngr usage wait --until 'cost.total_cost_usd > 100.0' --since 7d
```

## Examples

**Show current usage**

```bash
$ mngr usage
```

**Local agents only**

```bash
$ mngr usage --local
```

**Specific providers**

```bash
$ mngr usage --provider local --provider modal
```

**Aggregate cost across the last week**

```bash
$ mngr usage --since 7d
```

**Treat the snapshot as stale after 60s (warning only)**

```bash
$ mngr usage --max-age 60
```

**Machine-readable output**

```bash
$ mngr usage --format json
```

**Custom format template (aggregate cost + current session)**

```bash
$ mngr usage --format '{cost.total_cost_usd} ({current_session.session_id})'
```
