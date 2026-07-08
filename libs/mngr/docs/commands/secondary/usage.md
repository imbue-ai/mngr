<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr usage

**Synopsis:**

```text
mngr usage [--stale-after DURATION] [--detail] [--since DURATION] [--no-preserved] [COMMAND]
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
- Cost is process-cumulative as emitted (Claude Code's ``total_cost_usd``
  grows across session boundaries and only resets when the Claude Code
  process itself is relaunched; ``/clear`` does NOT reset it). The reader
  detects process boundaries via cost-drop signals within each agent's
  event stream and stores each session's *own contribution* (delta from
  the prior session's cumulative reading in the same process). Records
  are summed across all (agent, process, session) tuples within the
  ``--since`` recency window (default 24h).
- Cost is split by auth mode: ``subscription_cost`` aggregates sessions
  whose Claude Code process was on a Claude.ai Pro/Max subscription
  (numbers are imputed by Claude Code), and ``api_cost`` aggregates
  sessions whose process was on a direct ANTHROPIC_API_KEY (numbers are
  real billable spend). Mode is detected per process from whether any
  event in it carried a ``rate_limits`` payload (subscription auth) or
  not (api-key auth). The two are never lumped into a single ``cost``
  field -- conflating imputed estimates with real spend would be
  misleading.
- The JSON output's ``sessions[]`` array is ordered newest-first; consumers
  that want a specific session's reading can index ``sessions[0]``. Each
  session carries a ``cost_mode`` field ("SUBSCRIPTION" or "API_KEY").

**Usage:**

```text
mngr usage [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--stale-after` | text | Warn when the snapshot file is older than this (e.g. '300', '5m', '2h'). Display warning only -- it does not change which events are aggregated (use --since for that). Default: from plugin config. | None |
| `--detail` | boolean | Expand summary view: show per-session breakdown lines under each source's cost lines (human, tagged with `[sub]` or `[api]`), and include the `sessions[]` array under each source (JSON, each session carrying `cost_mode`). Default omits the per-session breakdown for terseness; the per-mode cost lines and window lines are unchanged. | `False` |

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
| `--since` | text | Recency window for per-session cost aggregation (e.g. '24h', '7d'). Sessions whose last event is older are dropped from `sessions[]` and from the per-mode aggregates (`subscription_cost.*` / `api_cost.*`) computed off them. Default: from plugin config (24h). | None |
| `--preserved`, `--no-preserved` | boolean | Include usage preserved from destroyed agents (under <local_host_dir>/preserved/). On by default so destroyed agents' spend still counts; pass --no-preserved to show only live agents. Preserved agents honor the same --provider/--project/--local/label filters. | `True` |

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

- ``subscription_cost.total_cost_usd`` / ``subscription_cost.total_duration_ms`` / ... :
  aggregate across the recency window of sessions whose Claude Code process
  was on a Claude.ai Pro/Max subscription. Cost is **imputed** by Claude Code
  (what the usage would have cost on the metered API) and is informational --
  the user actually pays a flat subscription. Never lumped with ``api_cost``.
- ``api_cost.total_cost_usd`` / ``api_cost.total_duration_ms`` / ... :
  aggregate across the recency window of sessions whose Claude Code process
  was on a direct ANTHROPIC_API_KEY. Cost is **real** billable spend.
- ``subscription_session_count`` / ``api_session_count``: number of sessions
  in each mode contributing to the corresponding aggregate. ``session_count``
  is the total across both modes.
- ``sessions``: list of session-cost records, newest-first. Each entry
  carries a ``cost_mode`` ("SUBSCRIPTION" or "API_KEY") tag.

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
| `--since` | text | Recency window for per-session cost aggregation (e.g. '24h', '7d'). Affects the per-session surfaces in the CEL context: `subscription_cost.*` / `api_cost.*` (per-mode aggregates), `sessions[]`, and the `*_session_count` fields. Default: from plugin config (24h). | None |

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
| `--preserved`, `--no-preserved` | boolean | Include usage preserved from destroyed agents when evaluating the predicate. On by default; pass --no-preserved to consider only live agents. | `True` |

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

**Wait until cumulative real API spend over the last 24h crosses $20**

```bash
$ mngr usage wait --until 'api_cost.total_cost_usd > 20.0'
```

**Cap real spend in the last week (subscription cost is imputed and ignored here)**

```bash
$ mngr usage wait --until 'api_cost.total_cost_usd > 100.0' --since 7d
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
$ mngr usage --stale-after 60
```

**Per-session breakdown (human + JSON, mode-tagged)**

```bash
$ mngr usage --detail
```

**Machine-readable output**

```bash
$ mngr usage --format json
```

**Custom format template (real API spend only)**

```bash
$ mngr usage --format '{api_cost.total_cost_usd} across {api_session_count} sessions'
```
