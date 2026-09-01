# Kanpan

All-seeing agent tracker. The name combines Sino-Japanese 看 (*kan*, "to look", as in 看板 *kanban*) and Greek πᾶν (*pan*, "all") -- a unified view that aggregates state from all sources (mngr agent lifecycle, git branches, GitHub PRs and CI) into a single board.

Launch with `mngr kanpan`. Requires the `gh` CLI to be installed and authenticated.

The footer shows the command keys (`/: search  r: refresh  m: mute  d: mark delete  x: execute  q: quit  ?: more keys`); press `?` for an overlay listing every binding (`space` peek, `enter` attach, marks, and your configured commands). `Esc` closes it.

On a terminal too narrow for the whole legend, bindings are dropped whole rather than clipped mid-word, starting from the left. `?: more keys` is listed last and so survives longest, since it is how the dropped keys can still be found.

## Attach, peek, and reply

Interact with an agent without leaving the board:

- **Attach** (`Enter`): enter the focused agent's full interactive session (equivalent to `mngr connect`). The board suspends while you are attached and restores immediately when you detach (tmux's `Ctrl-b d`), so you return to the board rather than a bare shell. The screen clears to a brief `Connecting to <agent>...` line on the way in, rather than flashing the shell.
- **Peek** (`Space`): open a live bordered panel below the board showing the focused agent's recent user/agent conversation (via `mngr transcript --role user --role agent`), refreshed every couple of seconds, with the board still visible above. Tool calls and framework-injected turns do not appear, so the peek reads like the human conversation. Each message's `[timestamp] role:` header is trimmed to a dim role cue. It shows the newest lines, so a long final message renders its end (the agent's conclusion, or the question it is waiting on) under a `⋯` marker, rather than mirroring the agent's scrolled-up screen. The panel says `(no messages yet)` when there is no readable message.
  - `Esc` closes the panel. To peek a different agent, close it, move the board selection, and press `Space` again.
- **Reply**: type into the panel's `›` input and press `Enter` to send the message to that agent (equivalent to `mngr message`); an empty reply does nothing. Your reply is echoed into the panel immediately as a `›` line, so you see it without waiting: `mngr message` blocks until durable evidence shows the agent accepted the reply, up to ~90s, so the send runs in the background and is not awaited. Being busy is not what makes an agent slow to accept: a claude agent's evidence is the reply appearing in its transcript, which Claude Code writes as it queues it, so a reply to an agent mid-turn confirms in seconds rather than when the turn ends. An agent type whose evidence is a turn marker rather than transcript content (codex, antigravity) does hold its confirmation until the queued prompt opens a turn. Once the agent accepts the reply and it appears in the transcript, the echo is replaced by the real message. Several replies typed in a row are delivered in the order you sent them.
  - Replying to an agent that is not live starts it (the send passes `mngr message --start`): an offline host is brought up and a `STOPPED` or `DONE` agent is (re)launched, so the reply lands rather than failing with the agent's state. Reviving a `DONE` agent tears down its lingering tmux session, discarding that pane's content -- attach instead if you want to read it first. The reply takes correspondingly longer to confirm, since the agent has to come up before it can accept anything, and a start slow enough to outrun the send's own three-minute ceiling is reported as a failure whether or not the reply eventually lands.
  - A reply leaves the row's `STATE` stale -- a `WAITING` agent goes back to work, a `STOPPED` one is now running -- so the board re-probes local state once the send returns and the row catches up on its own. A failed send re-probes too, since it may have moved the row as well: the (re)launch happens before delivery is attempted, and the failure does not say whether it got that far.
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

## Header status

The right of the header can carry a line of your own text, built from counts over the agents on the board:

```toml
[plugins.kanpan]
header_status = 'Running: {state == "RUNNING"}'
```

Each braced CEL expression renders as the number of agents it holds for; everything outside the braces is literal text, so the label goes wherever you want it. The expressions are the same language `--include` takes, evaluated against the entry shape `--format json` emits -- so a count can be taken against any board column, not just the agent state:

```toml
[plugins.kanpan]
header_status = 'Running: {state == "RUNNING"} · Red: {cells.ci.text == "failure"} · Ahead: {fields.commits_ahead.count > 0} · Merged: {section == "PR_MERGED"}'
```

`cells.<column>.text` is the text a column shows, `fields.<column>` the structured value behind it, and `section` the board section. `{total}` counts every agent, though each section heading already carries its own count. Write `{{` and `}}` for literal braces.

Use a single-quoted TOML string, since the expressions themselves contain double quotes.

An agent with no value for the column a count names -- or whose value has no such member, as a `pr` column holding a fetch failure has no `state` -- is simply not counted. Counts run over the agents the board is showing, so they follow any active filter.

A count sees the board entry, which is a narrower shape than `--include` sees. `--include` filters agents before the board is built and so reads the full agent (`labels.project`, `host.provider`, `age`, ...); a count reads only what the board holds: `name`, `state`, `provider_name`, `branch`, `work_dir`, `is_muted`, `section`, `fields`, `cells`. A count naming anything else matches no agent and stays at zero -- filter with `--project` or `--include` instead.

The title stays centred on the screen whatever the status says. The status takes the space to the right of the title, and is dropped whole on a terminal too narrow to hold it -- a right-aligned clip would leave a fragment of itself.

An unbalanced brace, or an expression that is not valid CEL, is reported when kanpan starts rather than once the board is up.

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
command = "mngr connect $MNGR_AGENT_ID"

[plugins.kanpan.commands.l]
name = "event"
command = "mngr event $MNGR_AGENT_ID"
refresh_afterwards = true
```

Each entry defines a keybinding (the table key, e.g. `c`) that appears in the status bar. The command runs with three environment variables set for the focused agent: `MNGR_AGENT_ID` (its ID), `MNGR_HOST_ID` (the ID of the host it runs on), and `MNGR_AGENT_NAME` (its name). Both the id and the name are unique only per host, not globally: two hosts can hold same-named agents, and the same agent id can exist on multiple hosts (e.g. while an agent is migrated between hosts). Prefer `$MNGR_AGENT_ID` over `$MNGR_AGENT_NAME` for commands that target the agent, and use the full address `"$MNGR_AGENT_ID@$MNGR_HOST_ID"` when the command must resolve to exactly one instance. Custom commands override builtins when they share the same key. Set `enabled = false` to disable a builtin.

By default, custom commands run immediately on the focused agent. Set `markable = true` to make a command use dired-style batch marking instead: pressing the key marks agents, then `x` executes all marks at once. If any operation fails (including a builtin delete), the marks for the failed agents are kept so you can retry, and the failures are listed at the bottom of the board (alongside fetch errors) until the next execution.

```toml
[plugins.kanpan.commands.s]
name = "stop"
command = "mngr stop $MNGR_AGENT_ID"
markable = true
refresh_afterwards = true
```

Marked operations run several at a time, since marked agents are independent -- otherwise a batch costs the sum of its parts, which is painful for a command that waits on the agent (`mngr message` blocks until the agent accepts). Four run at once by default:

```toml
[plugins.kanpan]
batch_concurrency = 8   # or 1 to run them strictly one at a time
```

The footer counts finished work (`Executing 2/5`) rather than naming one operation, since several are in flight. Batch work runs on its own worker pool, so a slow batch never delays a board refresh.

### Prompting for a value

Set `prompt` to ask for a value before the command runs. Pressing the key floats a small bordered input in the middle of the board, captioned with the `prompt` text and titled with the agent it will act on; the text you type is passed to the command as the `MNGR_INPUT` environment variable.

```toml
[plugins.kanpan.commands.R]
name = "rename"
prompt = "new name: "
command = 'mngr rename "$MNGR_AGENT_ID" "$MNGR_INPUT"'
refresh_afterwards = true
```

- The input supports the same readline editing as the peek reply (`Ctrl-A`/`Ctrl-E`, `Ctrl-W`, `Ctrl-U`/`Ctrl-K`, `Option`/`Ctrl`+`←`/`→`).
- `Enter` runs the command. **An empty line is a valid submission** -- it is how you clear a value -- so `Esc` (or `Ctrl-C`) is the way to cancel. Cancelling runs nothing and changes nothing.
- The target agent is captured when the prompt opens, so a periodic refresh landing while you type cannot retarget the command.
- Pressing the key with no agent focused does nothing.
- Prompted commands carry a trailing `…` wherever their key is listed: the `?` overlay, and the footer belt in the case where the command overrides one of the keys the belt advertises.
- The prompt is modal. Board keys type into it, and clicks on the board are ignored while it is open, so the selection cannot move out from under the agent named in the title. The board's own highlight is not drawn meanwhile, which is why the title names the target.
- Always quote `"$MNGR_INPUT"` in your command. The value is passed through the environment rather than interpolated into the command string, but the command still runs under a shell, so an unquoted expansion is word-split and glob-expanded.
### Prompting once for many agents

Combine `prompt` with `markable` to answer once for a whole batch: press the key on each agent to mark it, press `x`, and the value you type is applied to every marked agent.

```toml
[plugins.kanpan.commands.M]
name = "message"
prompt = "message: "
command = 'mngr message "$MNGR_AGENT_ID" -m "$MNGR_INPUT"'
markable = "light cyan"
```

- The prompt opens when `x` executes, not when you mark, and names how many agents it covers.
- Mark different agents with different prompted commands and `x` asks for each in turn, one prompt per command, before anything runs.
- `Esc` at any of those prompts runs **nothing at all** and leaves the marks, so a batch is never half-applied.
- Failures keep their marks so you can retry, and a retry asks again rather than reusing the earlier answer -- the value may be exactly what went wrong, and the prompt is the only place you see what is about to be applied.
- `refresh_afterwards` is redundant here: the batch path always refreshes when it finishes.

### Example: a note per agent

A prompted command that writes an agent label, plus a label-backed column that displays it, gives you one free-form line against each agent:

```toml
[plugins.kanpan.commands.w]
name = "note"
prompt = "note: "
command = 'mngr label "$MNGR_AGENT_ID" -l "note=$MNGR_INPUT"'
refresh_afterwards = true

[plugins.kanpan.columns.note]
header = "NOTE"
```

Press `w` on an agent, type a line, press `Enter`; `refresh_afterwards` repaints the `NOTE` cell. Submitting an empty line blanks it.

**This is one note, not a set of tags.** A label holds a single value, so pressing the key again replaces what was there -- there is no adding a second value or removing one of several. Name it accordingly: calling it a tag promises set semantics the mechanism does not have.

For a *status* -- a small fixed vocabulary you want colored -- a prompt is the wrong tool, since you would retype the same few words. Bind each value to its own key instead, which needs no prompt at all:

```toml
[plugins.kanpan.commands.B]
name = "blocked"
command = 'mngr label "$MNGR_AGENT_ID" -l "status=blocked"'
refresh_afterwards = true

[plugins.kanpan.columns.status]
header = "STATUS"

[plugins.kanpan.columns.status.colors]
blocked = "light red"
review = "yellow"
```

Two `mngr label` behaviours to know about, neither specific to kanpan:

- **An empty value clears the display, not the label.** `mngr label` has no delete path, so an empty submission sets the key to the empty string. The cell blanks, but `--include 'has(labels.note)'` keeps matching that agent. Filter on the value (e.g. `labels.status == "blocked"`) rather than on presence.
- **Use a bare identifier as the label key.** Filter expressions are CEL, where a key containing a dash parses as subtraction and silently matches zero agents. `note` works; `my-note` does not.

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
- **Local refresh** (after push, delete, custom commands, attach, and a peek reply whether or not it was delivered, plus on a timer if you set one): runs every local data source -- `repo_paths`, `git_info`, and any label-backed column -- and carries the rest (PR, CI, shell columns) forward from the previous snapshot. Label-backed columns being local is why `refresh_afterwards` on a command that writes a label repaints that label's column.

An action's refresh is held, not dropped, when one is already running: the fetch in flight was started before the action, so it cannot show what the action changed, and a second one runs as soon as it lands. So the board always catches up with an action you took, whatever the refresh timing happened to be. A held outcome message waits for that second refresh too, keeping the message and the rows it describes together. A periodic full refresh that lands mid-fetch is skipped for that interval and resumes on the next one.

Both are configurable:

```toml
[plugins.kanpan]
# Seconds between periodic full refreshes (default 10 minutes)
refresh_interval_seconds = 600.0
# Seconds before retrying after a failed full refresh
retry_cooldown_seconds = 60.0
# Seconds between periodic local refreshes (default 30 seconds); 0 runs them
# only in response to an action.
local_refresh_interval_seconds = 30.0
```

### Keeping the board live

A local refresh runs every `local_refresh_interval_seconds`, so `STATE`, `commits_ahead` and any header count over them describe the fleet as it is rather than as the last full refresh found it. Shorten the interval to watch a count move in something close to real time:

```toml
[plugins.kanpan]
local_refresh_interval_seconds = 5.0
header_status = 'Running: {state == "RUNNING"}'
```

Set it to `0` to go back to refreshing only when you act. That is the way to turn the timer off -- TOML has no null, so a setting that is on by default needs a value that means off, and zero arms no alarm rather than one that is always due. A negative interval is rejected.

What to weigh in choosing one: a refresh costs roughly a second per few dozen agents and is spent whether or not anything changed. The default trades a small share of an idle board's time for columns that are seconds rather than minutes old; a few seconds an interval keeps a count live at the cost of the board reading the agent list almost continuously.

The interval is a floor, not a promise. A tick that lands while the previous refresh is still running is skipped rather than queued, so an interval shorter than a refresh takes settles into back-to-back refreshes instead of a growing backlog. A refresh that finishes inside its interval leaves the columns under two intervals behind; a slower one stretches that in step with itself, so a slow data source costs you freshness rather than piling work up. These refreshes run on their own worker, so a slow one delays neither the periodic full refresh nor an action's repaint.

A header count is only ever as fresh as the columns it reads. `{state == "RUNNING"}` and `{fields.commits_ahead.count > 0}` ride this timer, but `{cells.ci.text == "failure"}` still moves at the GitHub source's cadence, since remote sources sit these refreshes out.

The `Errors:` block at the bottom of the board belongs to the full refresh for the same reason: a tick has trouble of its own to report -- a provider that will not answer counts against it as much as against a full refresh -- but taking its list would drop what a remote source reported, which only the full refresh can put back. So a tick that goes wrong leaves the board exactly as it was -- no error row, no spinner, nothing at all. At a few seconds an interval, anything else would be noise. Trouble that persists still reaches the board within `refresh_interval_seconds`, since the full refresh does report the fetch errors it runs into, and what it reported stays up across these ticks.

## Staleness

Cells whose underlying value is older than `staleness_threshold_seconds` are rendered in dark grey to signal that the value may be out of date.

```toml
[plugins.kanpan]
# Cells older than this are rendered greyed-out. If unset (the default),
# resolves to 90% of refresh_interval_seconds, so a value that was not
# updated by the most recent refresh cycle shows as stale.
# staleness_threshold_seconds = 540.0
```
