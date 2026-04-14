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
# create_pr_url = true
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
| `MNGR_FIELD_CI_STATUS` | CI status (from cached fields) |

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
name = "events"
command = "mngr events $MNGR_AGENT_NAME"
refresh_afterwards = true
```

Each entry defines a keybinding (the table key, e.g. `c`) that appears in the status bar and runs with the `MNGR_AGENT_NAME` environment variable set to the focused agent's name. Custom commands override builtins when they share the same key. Set `enabled = false` to disable a builtin.

By default, custom commands run immediately on the focused agent. Set `markable = true` to make a command use dired-style batch marking instead: pressing the key marks agents, then `x` executes all marks at once.

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

By default, sections are displayed in this order: Done (PR merged), Cancelled (PR closed), In review (PR pending), In progress (no PR), In progress (PRs not loaded), Muted. To customize:

```toml
[plugins.kanpan]
section_order = ["STILL_COOKING", "PR_BEING_REVIEWED", "PR_MERGED", "PR_CLOSED", "MUTED"]
```

Valid section names are: `PR_MERGED`, `PR_CLOSED`, `PR_BEING_REVIEWED`, `STILL_COOKING`, `PRS_FAILED`, `MUTED`. Sections not listed in `section_order` are omitted.

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
