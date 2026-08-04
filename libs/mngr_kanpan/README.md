# Kanpan

All-seeing agent tracker. The name combines Sino-Japanese 看 (*kan*, "to look", as in 看板 *kanban*) and Greek πᾶν (*pan*, "all") -- a unified view that aggregates state from all sources (mngr agent lifecycle, git branches, GitHub PRs and CI) into a single board.

Launch with `mngr kanpan`. Requires the `gh` CLI to be installed and authenticated.

The footer shows the command keys (`/: search  r: refresh  m: mute  d: mark delete  x: execute  q: quit  ?: more keys`); press `?` for an overlay listing every binding (`space` peek, `enter` attach, marks, and your configured commands). `Esc` closes it.

On a terminal too narrow for the whole legend, bindings are dropped whole rather than clipped mid-word, starting from the left. `?: more keys` is listed last and so survives longest, since it is how the dropped keys can still be found.

## Attach, peek, and reply

Interact with an agent without leaving the board:

- **Attach** (`Enter`): enter the focused agent's full interactive session (equivalent to `mngr connect`). The board suspends while you are attached and restores immediately when you detach (tmux's `Ctrl-b d`), so you return to the board rather than a bare shell. The screen clears to a brief `Connecting to <agent>...` line on the way in, rather than flashing the shell.
- **Peek** (`Space`): open a live bordered panel below the board showing the focused agent's recent user/assistant conversation (via `mngr transcript --role user --role assistant`), refreshed every couple of seconds, with the board still visible above. Tool calls and framework-injected turns do not appear, so the peek reads like the human conversation. Each message's `[timestamp] role:` header is trimmed to a dim role cue. It shows the newest lines, so a long final message renders its end (the agent's conclusion, or the question it is waiting on) under a `⋯` marker, rather than mirroring the agent's scrolled-up screen. The panel says `(no messages yet)` when there is no readable message.
  - `Esc` closes the panel. To peek a different agent, close it, move the board selection, and press `Space` again.
- **Reply**: type into the panel's `›` input and press `Enter` to send the message to that agent (equivalent to `mngr message`); an empty reply does nothing. Your reply is echoed into the panel immediately as a `›` line, so you see it without waiting: `mngr message` blocks up to ~90s on the agent's submission signal (which a busy agent cannot give until its current turn ends), so the send runs in the background and is not awaited. Once the agent accepts the reply and it appears in the transcript, the echo is replaced by the real message. Several replies typed in a row are delivered in the order you sent them.
  - The input supports readline-style editing: word movement (`Option`/`Ctrl`+`←`/`→`), word delete (`Option`+`Delete`, `Ctrl-W`), start/end (`Ctrl-A`/`Ctrl-E`), and kill to start/end (`Ctrl-U`/`Ctrl-K`).
- **Selections**: a text reply cannot move a selection cursor, and selection menus (e.g. `/login`) are not part of the transcript, so they do not appear in the peek; attach (`Enter`) to make the choice in the real session.

These are builtins; they do not need any configuration.

## Search

Press `/` to jump to a row on a crowded board. A prompt opens in the footer and the selection moves to the best match as you type -- the board itself is never reordered or hidden.

The query is matched case-insensitively against every column you can see, so you can jump by agent name, PR number, CI status, repo, or the value of any custom column: `/kanpan` finds an agent by name, `/#2531` finds the agent whose PR that is. Matches are ranked by name prefix, then name substring, then any other cell, and ties keep board order. The prompt shows which match you are on (`2/6`), or `no match`, in which case the selection stays where it was.

| Key | Effect |
|---|---|
| `↑` / `↓` | move to the previous/next match |
| `Enter` | close the prompt, leaving the match selected |
| `Esc` | close the prompt and return to the row you started from |
| `Backspace` | erase a character; on an empty query it erases the `/` too and cancels, same as `Esc` |
| click a row | close the prompt and select the row you clicked |

Backspace retraces exactly what you typed: erasing the query rewinds the selection to where the search started and leaves the prompt open, and only the next backspace takes the `/` with it. `Ctrl-U` kills back to the start of the query, for a retype without leaving the prompt.

`Enter` only selects: it closes the prompt and leaves the match highlighted, it does not attach. Acting on the row is a separate keystroke afterwards, so `/mngr` `Enter` `Enter` attaches (`space` peeks, `d` marks).

While the prompt is open it owns the keyboard, so `q` and the command keys type into the query instead of acting on the board. The prompt opens in the footer's status slot, taking the place of the refresh stamp rather than adding a row, so the board never shifts under the cursor; the belt beside it swaps the board's key legend for the prompt's and shows the match count, since none of the board keys fire until you close it. The query input takes the same readline editing as the peek reply input (`Ctrl-A`/`Ctrl-E`, `Ctrl-W`, `Ctrl-U`, word movement).

Searching only moves the selection -- it leaves no filter behind, and a board refresh while the prompt is open re-runs the query against the new rows, leaving you on the match you were on unless it is no longer there. If such a refresh leaves the query with nothing to match, the selection falls back to the row you started from, so the highlight always shows what `Enter` would commit to. If it takes that row away too, `Esc` comes back to no selection at all rather than keeping the match, which is what `Enter` means.

`/` is a builtin bound like any other command, so a custom command on `/` overrides it and `enabled = false` disables it, through `[plugins.kanpan.commands]`.

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
- `sections` groups agents the same way the board does, in `section_order`, omitting empty sections. Each entry carries both `cells` (the pre-rendered text/url/color shown on the board, plus `runs` for cells that link more than one thing) and `fields` (the structured underlying values -- e.g. the PR number as an integer -- so consumers don't have to parse display text).
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

#### The PRS column

The `pr` column lists the pull requests on every branch an agent's worktree has checked out. mngr records one branch per agent when it creates it, but a worktree is a checkout and can host any number of branches over its life -- stacked or follow-up work opens further PRs from the same worktree, and the board used to be blind to all of them.

```
  NAME        STATE    GIT           PRS                                 CI       REPO
  bundle      STOPPED  [7 unpushed]  #2376                               success  imbue-ai/mngr
  mngr-msg    STOPPED  [up to date]  #2392, #2482                        success  imbue-ai/mngr
  release40   STOPPED  [up to date]  #158, #248, #250, #247, #209, #210  success  imbue-ai/mngr-internal
  dev-bundle  STOPPED  [not pushed]  +PR, #110                           success  imbue-ai/mngr-internal
```

There is nothing to turn on, and one PR is the ordinary case: an agent with a single branch renders `#2376` exactly as it always has, one cell with one hyperlink. The column only grows for a worktree that has been on more than one branch.

Note what that does and does not claim. These are branches the worktree has *had checked out*, which is not the same as branches it *produced*: check out a colleague's branch to look at it and its PR joins this agent's cell, because a reflog cannot tell working from visiting. The lookup also matches your own PRs rather than PRs this particular agent opened.

The cell is never abridged, so an agent on six branches makes the column as wide as its six PR numbers -- and because a column is sized to its widest cell, that width applies to every row. The board clips at the terminal's right edge, so a wide board can push the columns after `PRS` out of view until the window is widened. Listing every PR is the point of the column; the alternative is a count you cannot open.

Each PR number is its own clickable hyperlink. The cell leads with the branch mngr recorded -- the PR the row's section and its `CI`, `CONFLICTS` and `UNRESOLVED` columns all describe -- so nothing else about the row changes meaning; the rest follow in priority order. `dev-bundle` shows the shape when the recorded branch has no PR yet: `+PR` still opens the create-PR page, and `#110` came out of the same worktree.

PRs are listed whatever their state, as the column has always done for a single PR. Only the entries after the first are colored, since the section heading already reports the leading PR's state: a merged PR takes the magenta of `Done` and a closed one the grey of `Cancelled`, so a finished PR listed away from its section still reads as finished. A muted or stale row still greys out whole.

The branch list is the agent's recorded branch plus its worktree's HEAD reflog, in this priority order: the checked-out branch, the recorded branch, then the rest of the checkout history most recent first.

Everything about it degrades to the single-PR cell rather than to an error: an agent with no local work dir, a work dir git cannot read, a repository with no reflog, a branch with no PR, and a failed fetch all fall back to what the column showed before. Reading the branch history costs one `git rev-parse` per agent work dir and one `git for-each-ref` per *repository* -- worktrees of one repository share a branch list, so a board full of agents on one repo asks for it once.

The extra PRs ride along in the board's existing search query rather than costing a request per branch or per agent; the board pages that one query 100 PRs at a time, so the only round trip this can add is a page the wider match set needs.

Each branch is another clause in that one query, and GitHub refuses a query past a certain length, so the extra branches are capped twice: per agent, and board-wide by how long the query is getting. Recorded branches are never dropped -- only the extra ones are, evenly across agents, and the board reports how many when it happens. A board big enough to hit the cap therefore loses breadth in this column rather than its `CI` column.

That cap covers the query's length only. GitHub's search API separately returns at most its first 1000 results, and a wider query matches more PRs, so a very large board that already sits near that ceiling can be pushed over it. That truncates the fetch rather than failing it: the agents whose PR fell past the ceiling show `?`.

The reflog infers rather than records which branches belong to an agent: a branch checked out once in passing shows up, and git expires reflog entries after 90 days by default, so a long-idle branch drops out. Branches are read against the repository's actual branch list, so a tag or an abbreviated commit checked out along the way cannot take a slot from a branch that has a PR, and a branch that never became a PR contributes nothing at all.

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

The PR column displays clickable hyperlinks (OSC 8) in terminals that support them. When an agent has a PR, the column shows `#<number>` linked to the PR URL. When no PR exists but the branch is pushable, it shows `+PR` linked to the create-PR URL. Each PR number in the cell links separately.

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
