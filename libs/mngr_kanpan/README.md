# Kanpan

All-seeing agent tracker. The name combines Sino-Japanese 看 (*kan*, "to look", as in 看板 *kanban*) and Greek πᾶν (*pan*, "all") -- a unified view that aggregates state from all sources (mngr agent lifecycle, git branches, GitHub PRs and CI) into a single board.

Launch with `mngr kanpan`. Requires the `gh` CLI to be installed and authenticated.

## Filtering

Filter which agents appear on the board using CEL expressions:

```bash
# Show only agents for a specific project
mngr kanpan --project mngr

# Show only running agents
mngr kanpan --include 'state == "RUNNING"'

# Exclude done agents
mngr kanpan --exclude 'state == "DONE"'
```

`--include` and `--exclude` accept arbitrary CEL expressions (repeatable). `--project` is a convenience shorthand that translates to an include filter on `labels.project`. Multiple `--project` flags are OR'd together.

When any filter is active, the header displays a `[filtered]` indicator.

## JSON output

Pass `--format json` (or `--format jsonl`) to skip the TUI and print a single board snapshot to stdout instead. This is a read-only one-shot intended for scripting: it fetches the board once (reusing the on-disk field cache, without writing it back) and exits. The same `--include`/`--exclude` filters apply.

`--format json` emits one object with `columns`, `sections`, `errors`, and `fetch_time_seconds`:

- `columns` lists the displayed columns in board order (mirroring `column_order`). Headers are the plain column titles.
- `sections` groups agents the same way the board does, in `section_order`, omitting empty sections. Each entry carries both `cells` (the pre-rendered text/url/color shown on the board) and `fields` (the structured underlying values -- e.g. the PR number as an integer -- so consumers don't have to parse display text).
- Sections you exclude from a custom `section_order` are omitted from the output too, matching what the board shows.

`--format jsonl` emits one agent record per line (each the same shape as an `entries` element, in board order), followed by one `{"event": "error", "message": "..."}` line per fetch error. Use it for streaming/line-oriented consumers; the column and section-order metadata that `json` carries is omitted.

## Data sources

Kanpan uses pluggable data sources to fetch per-agent data. Each data source produces typed fields that become columns on the board. Built-in data sources:

- **repo_paths**: Extracts GitHub repo path from agent remote labels (infrastructure data for other sources)
- **git_info**: Computes commits-ahead count from `git rev-list`
- **github**: Fetches PRs, CI status, merge conflict status, and unresolved review comments via the `gh` CLI

### Configuration

Data sources are configured in your mngr settings file:

```toml
[plugins.kanpan]
column_order = ["name", "state", "commits_ahead", "conflicts", "unresolved", "ci", "pr"]

# GitHub data source: all fields enabled by default
[plugins.kanpan.data_sources.github]
enabled = true
# Toggle individual fields:
# pr = true
# ci = true
# conflicts = true
# unresolved = true
```

### Shell command data sources

Add custom columns backed by shell commands:

```toml
[plugins.kanpan.shell_commands.slack_thread]
name = "Find Slack thread"
header = "SLACK"
command = """
THREAD=$(find-slack-thread --channel project-mngr --search "$MNGR_AGENT_NAME")
if [ -n "$THREAD" ]; then
  echo "$THREAD"
fi
"""
```

Shell commands run once per agent in parallel. The stdout (trimmed) becomes the column value. Commands receive environment variables:

| Variable | Description |
|---|---|
| `MNGR_AGENT_NAME` | Agent name |
| `MNGR_AGENT_BRANCH` | Git branch (empty if none) |
| `MNGR_AGENT_STATE` | Agent lifecycle state |
| `MNGR_AGENT_PROVIDER` | Provider instance name |
| `MNGR_FIELD_PR_NUMBER` | PR number (from cached fields) |
| `MNGR_FIELD_PR_URL` | PR URL (from cached fields) |
| `MNGR_FIELD_PR_STATE` | PR state: OPEN, MERGED, or CLOSED (from cached fields) |
| `MNGR_FIELD_CI_STATUS` | CI status (from cached fields) |
| `MNGR_FIELD_<KEY>` | Display text for any other cached field, uppercased key (e.g. `MNGR_FIELD_COMMITS_AHEAD`) |

If your script consumes any `MNGR_FIELD_<KEY>` env vars, declare those keys in `inputs` so the cell is marked stale whenever the inputs it depends on are stale. When `inputs` is unset (default), the cell is treated as freshly produced.

```toml
[plugins.kanpan.shell_commands.pr_age]
name = "PR age"
header = "PR_AGE"
command = '''
if [ -n "$MNGR_FIELD_PR_NUMBER" ]; then
  echo "PR #$MNGR_FIELD_PR_NUMBER"
fi
'''
inputs = ["pr"]  # marked stale when the cached `pr` field is stale
```

### Label-backed columns

Add extra columns that read from agent labels:

```toml
# Column showing the agent's "blocked" label value
[plugins.kanpan.columns.blocked]
header = "BLOCKED"
# label_key defaults to the field key ("blocked") if omitted
label_key = "blocked"

[plugins.kanpan.columns.blocked.colors]
yes = "light red"
no = "light green"
```

Each entry defines a column keyed by the field key (e.g. `blocked`). The `label_key` specifies which agent label to read (defaults to the field key). Use `colors` to map label values to urwid color names.

### Disabling a data source

Set `enabled = false` to disable a data source. Its cached fields are excluded from the board:

```toml
[plugins.kanpan.data_sources.github]
enabled = false
```

## Custom commands

Add to your mngr settings file (e.g. `.mngr/settings.toml`):

```toml
[plugins.kanpan.commands.c]
name = "connect"
command = "mngr connect $MNGR_AGENT_NAME"

[plugins.kanpan.commands.l]
name = "event"
command = "mngr event $MNGR_AGENT_NAME"
refresh_afterwards = true
```

Each entry defines a keybinding (the table key, e.g. `c`) that appears in the status bar and runs with the `MNGR_AGENT_NAME` environment variable set to the focused agent's name. Custom commands override builtins when they share the same key. Set `enabled = false` to disable a builtin.

By default, custom commands run immediately on the focused agent. Set `markable = true` to make a command use dired-style batch marking instead: pressing the key marks agents, then `x` executes all marks at once. If any operation fails (including a builtin delete), the marks for the failed agents are kept so you can retry, and the failures are listed at the bottom of the board (alongside fetch errors) until the next execution.

```toml
[plugins.kanpan.commands.s]
name = "stop"
command = "mngr stop $MNGR_AGENT_NAME"
markable = true
refresh_afterwards = true
```

## Column order

Control which columns appear and in what order:

```toml
[plugins.kanpan]
column_order = ["name", "state", "commits_ahead", "ci", "pr"]
```

Built-in column names: `name`, `state`. Data source field keys: `commits_ahead`, `pr`, `ci`, `conflicts`, `unresolved`, `repo_path`. Shell command field keys match their config key (e.g. `slack_thread`).

## Section order

By default, sections are displayed in this order: Done (PR merged), Cancelled (PR closed), In review (PR pending), In progress (draft PR), In progress (no PR yet), In progress (PRs not loaded), Muted. To customize:

```toml
[plugins.kanpan]
section_order = ["STILL_COOKING", "PR_DRAFT", "PR_BEING_REVIEWED", "PR_MERGED", "PR_CLOSED", "MUTED"]
```

Valid section names are: `PR_MERGED`, `PR_CLOSED`, `PR_BEING_REVIEWED`, `PR_DRAFT`, `STILL_COOKING`, `PRS_FAILED`, `MUTED`. Sections not listed in `section_order` are omitted.

The PR column displays clickable hyperlinks (OSC 8) in terminals that support them. When an agent has a PR, the column shows `#<number>` linked to the PR URL. When no PR exists but the branch is pushable, it shows `+PR` linked to the create-PR URL.

## Refresh behavior

Kanpan uses two refresh strategies:

- **Full refresh** (manual 'r' key, periodic 10-minute timer): runs all data sources. Only one can be in flight at a time -- pressing 'r' while a refresh is running is ignored.
- **Agent-only refresh** (after push, delete, custom commands): runs only local data sources (repo_paths, git_info). Remote data (PR, CI) is carried forward from the previous snapshot.

Both are configurable:

```toml
[plugins.kanpan]
# Seconds between periodic full refreshes (default 10 minutes)
refresh_interval_seconds = 600.0
# Seconds before retrying after a failed full refresh
retry_cooldown_seconds = 60.0
```

## Staleness

Cells whose underlying value is older than `staleness_threshold_seconds` are rendered in dark grey to signal that the value may be out of date.

```toml
[plugins.kanpan]
# Cells older than this are rendered greyed-out. If unset (the default),
# resolves to 90% of refresh_interval_seconds, so a value that was not
# updated by the most recent refresh cycle shows as stale.
# staleness_threshold_seconds = 540.0
```
