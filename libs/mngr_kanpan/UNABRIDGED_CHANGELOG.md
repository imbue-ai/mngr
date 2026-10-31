# Unabridged Changelog - mngr_kanpan

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_kanpan/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-01

Followed the annotations urwid 4.0.13 tightened, with no behaviour change to the board:

- An alarm callback's user data is now typed optional, because `set_alarm_in`'s `user_data` defaults to None. Every alarm callback accepts the optional form and checks it; every scheduling site still passes a value.

- A canvas's `coords` is now a TypedDict rather than a plain dict, and `content()` takes plain ints. The OSC 8 hyperlink canvas wrapper matches both.

- urwid_readline's keymap typing no longer needs the search-backspace suppression, which is gone.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-15

Drops a type-checker suppression on the kanpan input filter that urwid's current annotations no longer need. No behavior change.

## 2026-09-01

The peek panel no longer asks for `--role assistant`: streams from before the ATIF cutover now report an unsupported-format error instead of rendering, so the retired role spelling could never match anything. It asks for `--role user --role agent`.

The peek panel now asks `mngr transcript` for `--role agent` alongside `--role user --role assistant`, so agents on ATIF-shaped transcripts (which name that role `agent`) show their conversation in the panel instead of appearing empty.

The kanpan peek panel asks `mngr transcript` for `--role user --role agent`, following the ATIF role vocabulary (the retired pre-ATIF spelling was `--role assistant`).

## 2026-08-26

Fixed a CI flake (MIND-198) in the mute write-semantics acceptance test
(`test_set_agent_mute_writes_the_state_it_is_given`). It verified persistence by
running a full `fetch_board_snapshot` after each write, and a board fetch shells
out to probe every agent's tmux lifecycle; doing that three times could exceed the
test's 10s timeout on a loaded CI sandbox. The test now reads the flag straight
back from the agent's persisted plugin data -- exactly what `set_agent_mute`
writes -- so it no longer depends on the board-read pipeline, and a stand-in
`tmux` on PATH pins the read-back off that path. No product behavior changed; the
board's own surfacing of the muted flag stays covered by the sibling
`fetch_board_snapshot` tests.

## 2026-08-21

Muting an agent from the kanpan board now resolves the agent through the one provider that owns it, instead of discovering every configured provider and filtering the result by host.

Because mute addressed only `(host, agent)` and left the provider unset, persisting a mute triggered an unscoped discovery across every backend (local, ssh, docker, cloud, ...). That made muting a purely local agent depend on the health and latency of unrelated providers: a provider that was slow made the write time out, and a discovery that momentarily lacked the target host failed the write with `No hosts found matching host-<id>`. Mute now names the exact `(provider, host, agent)` instance -- the provider is already known from the board row -- so resolution touches only that provider and no longer depends on any other being reachable.

## 2026-08-16

Board entries now carry the agent's `host_id`, and everything row-scoped is keyed by the (host, agent) instance: dired-style marks, focus/peek memory, and batch execution. Mark-driven commands (`mngr destroy`, `mngr connect`, mute) address the exact instance as `ID@HOST`, so acting on one row can never touch a same-id agent on another host (agent ids are unique per host, not globally).

Custom commands additionally receive `MNGR_HOST_ID`; `"$MNGR_AGENT_ID@$MNGR_HOST_ID"` is a full mngr address for the exact instance.

Known limitation (documented follow-up): data-source field maps remain keyed by bare agent id, so a column value may mix two same-id instances during a migration window (display-only).

## 2026-08-12

Kanpan now identifies an agent by its globally unique id, not its name, whenever it acts on one, selects one, caches its data, or carries data across a refresh -- so two agents that share a name on different hosts are always kept apart. Names are still what the board displays.

Agent names are unique only within a single host, so anything kanpan did by name could hit the wrong agent (or several), or merge two agents' data, once two hosts held same-named agents. In particular, marking one such agent for deletion ran a single `mngr destroy <name> --force` that destroyed *both* of them, with no confirmation. Kanpan now:

- keys dired-style marks by agent id, so two same-named agents mark and act independently (previously a second one could not even be marked);

- resolves delete, mute, attach, peek, and peek-reply by id -- delete runs `mngr destroy <id>`, so it removes exactly the agents you marked; a reply goes only to the peeked agent instead of every agent with that name;

- restores focus after a refresh by id, so the cursor stays on the exact agent rather than jumping to the first one sharing its name;

- keys the per-agent field cache and every data source's output by id, and matches old/new snapshots by id when carrying PR/CI columns forward, so same-named agents no longer overwrite or inherit each other's column data;

- exports both `MNGR_AGENT_ID` (globally unique) and `MNGR_AGENT_NAME` to custom commands, so a command can target an agent unambiguously. The README examples now use `$MNGR_AGENT_ID`.

The board's JSON output (`--format json`/`jsonl`) now carries an `agent_id` field per entry. The incremental search (`/`) still matches by name; navigating between two same-named matches is a follow-up.

The board's `Refreshed <n> ago` stamp now reports the age of what is actually on screen.

Coming back from an attach used to cost a whole refresh interval. The attach blocks the board's
event loop for as long as the session is open, and on return the board asks for a local read;
the periodic full refresh then came due against that read and was dropped rather than held, so
the remote columns and the stamp stayed on the last full refresh for up to another interval. A
tick displaced by a local read is now owed, and runs as soon as that read finishes. A tick that
lands while a full refresh is already running is still skipped, as before.

A refresh that failed used to reset the stamp to `Refreshed just now` even though the board was
still showing the previous fetch. Only a refresh that landed renews it now, so a board whose
fetches are failing reports how old its rows really are. When it is the very first fetch that
fails there are no rows to age and no stamp to show, so the footer reports the failure itself.
The failure retry keeps its cooldown, which now counts from the attempt rather than from the
stamp.

The board also measures the terminal again when an attach returns. A terminal narrowed while the
board was suspended reached no resize handler, so the board came back drawn to its old width: the
footer belt ran past the right edge onto the line below, which scrolled the screen once per
repaint and left a copy of the belt behind each time -- a stack of `Refreshing` rows growing for
as long as the refresh ran.

A command that repaints the board (`refresh_afterwards`) holds its outcome until the repaint
lands. If another notification claimed the footer in the meantime -- muting an agent, say -- the
held outcome used to overwrite it, answering the keypress just made with the result of an earlier
command against an agent the user had not touched. The outcome now waits for that notification to
finish and reports after it, so neither message is lost. The command's in-progress label is
retired as soon as it finishes, rather than spinning until its outcome reaches the footer.

## 2026-08-11

Muting an agent no longer flashes the row back to the section it came from.

A refresh that was already running when `m` was pressed read the agent before the keypress, so
landing it put the row back until the next read moved it to Muted again. Board values now record
when they were read, and a mute set on the board outlasts any fetch that read the agent earlier.

Pressing `m` a second time during that window used to undo the first mute rather than repeat it,
because the board decided which way to flip from what it was showing while the write decided from
what was stored. The write now takes the state to set, so the two cannot disagree.

A reply typed into the peek panel now starts the agent it is addressed to. Previously the send passed no `--start`, so replying to an agent that was not live failed with `Agent is not running (state: STOPPED)` and the typed reply was dropped.

The reply now sends `mngr message --start`, which brings up an offline host and (re)launches a `STOPPED` or `DONE` agent before delivering. Note that reviving a `DONE` agent tears down its lingering tmux session and discards that pane's content, so attach first if you want to read it. The send's ceiling is now three minutes, which clears a live agent's own waits; a start slow enough to outrun it is reported as a failure whether or not the reply eventually lands.

The board now re-probes local state after a failed reply as well as a delivered one. The (re)launch happens before delivery is attempted, so a send that fails afterwards -- typically on its delivery confirmation -- can still have left the agent running, and the failure does not say whether it got that far. The row would otherwise keep showing `STOPPED` until the periodic local refresh, or until a manual `r` where that timer is turned off.

## 2026-08-05

Corrects what the docs say about replying to an agent that is mid-turn. The README and the peek-reply docstring both claimed a busy agent cannot give its submission signal until its current turn ends, and explained the peek reply's background send by that claim.

That is not how a claude agent behaves. Its durable evidence is the reply appearing in its transcript, and Claude Code writes a queued reply there as it queues it, so `mngr message` to an agent mid-turn confirms in seconds rather than at the end of the turn. Measured against a claude agent held in a foreground tool call: the send returned successfully in 4.4s while the agent stayed `RUNNING`.

The ~90s figure is unchanged and remains correct as the bound the send waits within, which is why the peek reply is still not awaited on the UI thread. The turn-end behaviour does apply to agent types whose evidence is a turn marker rather than transcript content (codex, antigravity), where the marker only advances once the queued prompt opens a turn; the docs now say which case is which.

Replying to an agent from the kanpan peek panel now refreshes the board once the reply is delivered.

Accepting a reply puts a `WAITING` agent back to work, but the board had no refresh trigger for that path, so the row kept rendering its pre-reply `STATE` until the next manual `r` or the 10-minute timer. Answering the question an agent was waiting on left it reading `WAITING` long after it had resumed. A failed send still only drops its optimistic echo and shows the error.

Every action that repaints the board now goes through one request, and a request made while a refresh is already running is held rather than dropped. The fetch in flight was started before the action, so it cannot show what the action changed; a second one now runs as soon as it lands. Previously such a request was lost outright, so push, delete, attach, a `refresh_afterwards` command, or a reply could all leave the board showing pre-action state until the next manual `r`. An outcome message held for its repaint waits for that second refresh too, so the message and the rows it describes still arrive together.

The periodic full refresh no longer stops for the rest of the session when its timer happens to fire while another refresh is in flight. Only a full refresh re-armed that timer on completion, so a firing that landed on a local refresh previously ended the chain and left the board with no periodic refresh at all; the interval is now skipped and the timer resumes on the next one.

The local refresh -- the one that already runs after push, delete, attach and `refresh_afterwards` commands -- now also runs on a timer, every 30 seconds by default. `STATE`, `commits_ahead` and label columns therefore describe the fleet as it is, rather than as the last full refresh found it up to ten minutes earlier. This changes what an untouched board costs. It already read the agent list on a timer -- the ten-minute full refresh does -- but at roughly a second per few dozen agents, doing so every thirty seconds instead spends a low double-digit percentage of an idle board's time on it rather than about one percent.

```toml
[plugins.kanpan]
# Shorten it to watch a count move in close to real time, or set 0 to refresh
# only in response to an action, as before.
local_refresh_interval_seconds = 5.0
```

That makes a header count like `Running: {state == "RUNNING"}` track reality rather than the last ten-minute cycle. Remote sources sit these refreshes out, so PR and CI keep the full refresh's cadence; a count over those columns keeps it too, and so does the `Errors:` block, which would otherwise lose whatever a remote source reported on the first tick after it landed.

Zero is how the timer is turned off, since TOML has no null and a setting that is on by default needs a value that means off. It arms no alarm at all; a negative interval is refused.

An interval of zero or less on `refresh_interval_seconds`, which has no such off switch, is refused at startup. Both timers re-arm when they fire, so an alarm that is always due leaves the event loop no idle moment to repaint in: the board would peg a core and freeze rather than merely refresh too often. The bound declared on the field cannot catch this on its own, because plugin config is built without validation, so the board checks both intervals itself before it takes the screen.

The interval is a floor rather than a promise. A tick that lands while the previous refresh is still running is skipped rather than queued, so an interval shorter than a refresh takes settles into back-to-back refreshes instead of a growing backlog, and a slow data source stretches the effective period instead of piling work up. These refreshes use their own worker, so a slow one delays neither the periodic full refresh nor an action's repaint.

A tick that goes wrong leaves the board exactly as it was -- no error row, no spinner, nothing at all -- because at a few seconds an interval anything else would be noise. Trouble that persists still reaches the board within `refresh_interval_seconds`, since the full refresh does report the fetch errors it runs into.

## 2026-08-04

The kanpan header can now carry a line of your own text on its right, built from counts over the agents on the board:

```toml
[plugins.kanpan]
header_status = '{state == "RUNNING"} running / {total}'
```

Each braced CEL expression renders as the number of agents it holds for -- the same language `--include` takes, evaluated against the entry shape `--format json` emits, so a count can be taken against any board column (`cells.ci.text == "failure"`, `fields.pr.state == "OPEN"`, `section == "PR_MERGED"`) and not just the agent state. `{total}` counts every agent, and `{{`/`}}` are literal braces. Counts run over the agents the board is showing, so they follow any active filter and any custom `section_order`.

The title stays centred on the screen whatever the status says, and a status too wide for the space beside it is dropped whole rather than clipped into a fragment of itself. An unbalanced brace, or an expression that is not valid CEL, is reported when kanpan starts rather than once the board is up.

The `PR` column is now the `PRS` column: it lists the pull requests on every branch an agent's worktree has checked out, not just the one on the branch mngr recorded when it created the agent. An agent that opened a follow-up PR from the same worktree used to show only its first PR while the newer one was invisible to the board.

There is nothing to turn on, and nothing changes for the ordinary case: an agent with one branch renders one PR in one cell with one hyperlink, exactly as before. The column only grows for a worktree that has been on more than one branch, and every way of having no extra branches -- an agent with no local work dir, a work dir git cannot read, a repository with no reflog, a branch that never became a PR, a failed fetch -- degrades to that same single-PR cell rather than to an error or an empty one.

The cell leads with the recorded branch's PR, so the row's section and its `CI`, `CONFLICTS` and `UNRESOLVED` columns keep describing exactly the PR they always did, and a configured `column_order` needs no new name. When the recorded branch has no PR yet the cell reads `+PR, #110`, so the create-PR link and the worktree's other PR are both there.

The cell is never abridged, so an agent on six branches makes the column as wide as its six PR numbers, and a column is sized to its widest cell. A wide board can push the columns after `PRS` past the terminal's right edge until the window is widened.

Each PR number in the cell is a separate clickable hyperlink. PRs are listed whatever their state, matching what the column has always done for a single PR. Only the entries after the first are colored, because the row's section already reports the leading PR's state: a merged PR takes the magenta of the `Done` section and a closed one the grey of `Cancelled`. A muted or stale row still greys out whole.

Reading the branch history costs one `git rev-parse` per agent work dir and one `git for-each-ref` per repository -- every worktree of a repository shares its branch list, so a board full of agents on one repository asks git for it once. The extra pull requests ride along in the board's existing search query rather than costing a request per branch or per agent; the board pages that one query 100 pull requests at a time, so the only round trip this can add is a page the wider match set needs.

The extra branches are capped board-wide by how long they make that query, because GitHub answers an over-long query with an empty response that fails every column the board fetches. Recorded branches are never dropped, the extras are given up evenly across agents, and the board reports how many it dropped. That cap covers the query's length only: GitHub's search API separately returns at most its first 1000 results, so a very large board already near that ceiling can be pushed over it, which truncates the fetch rather than failing it.

The branch list comes from the worktree's HEAD reflog, which infers rather than records which branches belong to an agent. Branches are read against the repository's actual branch list, so a tag, an abbreviated commit, or a revision expression checked out along the way cannot take a slot from a branch that has a pull request.

The `--format json` / `--format jsonl` output now carries a `runs` list on every cell, giving each linked segment its own text, url and color so consumers can reach every pull request without parsing the display text.

Marked operations executed with `x` now run several at a time instead of strictly one after another. Marked agents are independent, so a batch no longer costs the sum of its parts -- which mattered most for commands that block on the agent, such as `mngr message`, where messaging five agents could take five times as long as messaging one.

Four run at once by default, tunable (set it to 1 for the previous strictly-sequential behaviour):

```toml
[plugins.kanpan]
batch_concurrency = 8
```

The in-progress footer now counts finished work (`Executing 2/5`) rather than naming a single operation, since several are in flight at once.

Batch work also moved off the shared worker that serves board refreshes, so a long batch no longer holds up the next refresh. Failure handling is unchanged: a failed operation keeps its mark so it can be retried, and the failures are listed at the bottom of the board.

Quitting during a batch drops whatever has not started yet, so `q` returns you to the shell instead of waiting on operations you walked away from; anything already running is left to finish.

Executing a delete mark no longer jumps the board. The view is held in place across a refresh by anchoring on the focused row, and deleting removes exactly that row -- so it now falls back to the nearest surviving neighbour instead of dropping the anchor and rendering from the top. An open search still re-anchors itself to the row it started from.

Custom kanpan commands can now ask you for a value before they run, so free-form per-agent input (a note, a rename, a message) is config rather than one keybinding per possible value:

- Set `prompt` on a `[plugins.kanpan.commands.<key>]` entry to float a small input in the middle of the board when the key is pressed, titled with the agent it will act on. The text you type reaches the command as the `MNGR_INPUT` environment variable, alongside the existing `MNGR_AGENT_NAME`. The input has the same readline editing as the peek reply, and the board stays visible around it.

- `Enter` runs the command, including on an empty line (that is how you clear a value); `Esc` or `Ctrl-C` cancels and runs nothing. The target agent is captured when the prompt opens, so a refresh landing while you type cannot retarget the command. Prompted commands carry a trailing `…` wherever their key is listed -- the `?` overlay, and the footer belt in the case where the command overrides one of the keys the belt advertises.

- The value is passed through the environment rather than interpolated into the command string, but the command still runs under a shell, so quote `"$MNGR_INPUT"` in your command.

- Prompted commands run on their own worker, so a command against an unresponsive host can no longer hold up a board refresh.

- `prompt` combined with `markable` prompts once for a whole batch: mark agents with the key, press `x`, and the value you type is applied to every marked agent. Several prompted commands marked at once are asked for in turn, one prompt each, before anything runs; cancelling any of them runs nothing at all and keeps the marks. A retry after a failure asks again rather than reusing the earlier answer.

- Label-backed columns now blank their cell as soon as the label is cleared, instead of showing the old value until the next full refresh. Note that `mngr label` has no delete path, so clearing a label sets it to the empty string: the cell blanks but presence filters such as `has(labels.note)` keep matching. In `--format json`/`--format jsonl` this means a label-backed column's key is now always present in an agent's `fields` and `cells`, carrying an empty value for agents that lack the label, where it was previously omitted -- so test the value rather than the key's presence.

The README documents `prompt`/`MNGR_INPUT` and adds a worked example that combines a prompted command with a label-backed column to keep a note against each agent -- with the caveat that a label holds a single value, so this is one note rather than a set of tags, and a small fixed vocabulary is better served by a key per value than by a prompt.

While the prompt, the peek panel, or the `?` overlay is open, clicks on the board are ignored rather than moving the selection out from under the panel. (A click during a `/` search still ends the search on the row clicked, as before.)

Added `/` search to the kanpan board, for finding a row on a board with too many agents to scan.

Pressing `/` opens a prompt in the footer; as you type, the selection jumps to the best matching row. The query is matched case-insensitively against every visible column, so you can jump by agent name, PR number, CI status, repo, or any custom column -- `/kanpan` by name, `/#2531` by PR. Matches rank by name prefix, then name substring, then any other cell, with ties keeping board order.

`↑`/`↓` step through matches, `Enter` closes the prompt leaving the match selected, and `Esc` returns to the row you started from -- or to no selection at all, if a refresh has since taken that row off the board. Backspace retraces what you typed: erasing the query rewinds the selection and leaves the prompt open, and the next backspace erases the `/` and cancels. `Ctrl-U` kills back to the start of the query, for a retype without leaving. Clicking a row closes the prompt and selects what you clicked. `Enter` only selects; attaching stays a separate keystroke afterwards.

The prompt opens in the footer's status slot, in place of the refresh stamp rather than as an extra row, so the board does not shift when it opens. The belt beside it carries the match count (`2/6`, or `no match`) and the prompt's keys in place of the board's, which cannot fire until the prompt closes.

The board is never filtered or reordered -- search only moves the selection, so nothing is hidden and no state is left behind when the prompt closes.

`/` is a builtin command, so it appears in the footer and the `?` overlay, and like any builtin it can be overridden by a custom command on the same key or disabled via `[plugins.kanpan.commands]`.

The footer legend no longer clips mid-binding on a narrow terminal, where half of `r: refresh` used to render as `resh`. Bindings are dropped whole, from the left, and re-fitted whenever the terminal is resized or the status text beside them changes width. `?: more keys` is last to go, being the way to find whatever was dropped.

`Ctrl-T` (readline's transpose) no longer takes the board down. It crashed on an empty input, and scrambled a short one instead of transposing, so it is unbound in both the search prompt and the peek reply input.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-16

Added attach, peek, and reply to the kanpan board, so you can work with an agent without leaving it:

- Attach to the focused agent's session with `Enter`. The board suspends while attached and restores immediately when you detach (`Ctrl-b d`), returning you to the board instead of a bare shell. On attach the screen clears to a brief `Connecting to <agent>...` line rather than flashing the shell behind the board; a failed connect shows its exit code as a footer message instead of vanishing with the repaint.

- Peek at an agent with `Space`: a live bordered panel below the board shows the agent's recent user/assistant conversation (via `mngr transcript --role user --role assistant`), refreshed every couple of seconds, with the board still visible above. Tool calls and framework-injected turns do not appear, so the peek reads like the human conversation. Each `[timestamp] role:` header is trimmed to a dim role cue. It shows the newest lines, so a long final message renders its end under a `⋯` marker rather than being cut off at the top or mirroring the agent's scrolled-up screen. The panel says `(no messages yet)` when there is no readable message, and its border title (`name · state · provider`) re-renders on every board refresh so it cannot go stale. `Esc` closes the panel.

- Reply from the peek panel: type into the `›` input and press `Enter` to send a message to that agent (an empty reply does nothing). Your reply is echoed into the panel immediately as a `›` line, so it appears without waiting; the send runs in the background (`mngr message` blocks up to ~90s on the agent's submission signal, which a busy agent cannot give until its current turn ends), and once the agent accepts the reply and it shows up in the transcript the echo is replaced by the real message. Several replies typed in a row are delivered in order. A failed send drops its echo and renders a dim `(reply failed: ...)` line, so a lost message never silently looks delivered.

- The reply input supports readline-style editing (via the `urwid_readline` library, with the Option/Ctrl+arrow chords added): word movement (`Option`/`Ctrl`+`←`/`→`, `Meta-B`/`F`), word delete (`Option`+`Delete`, `Ctrl-W`, `Meta-D`), jump to start/end (`Ctrl-A`/`Ctrl-E`), and kill to start/end (`Ctrl-U`/`Ctrl-K`).

- The footer is a single continuous blue belt (no gap between its two sides): on the left a relative refresh stamp -- `Refreshed just now · 3.2s` right after a refresh, aging to `Refreshed 32s ago` / `Refreshed 5m ago` (the fetch duration only shows while fresh) -- and on the right the command keys one forgets -- `r: refresh  m: mute  d: mark delete  x: execute  q: quit  ?: more keys` -- with the keys highlighted. Everything else (`space` peek, `enter` attach, marks, configured shell commands) is listed by the `?` overlay: a bordered, padded panel anchored above the footer's `?`, keys accented in their own column; `Esc`, `q`, or `?` closes it. The peek panel's `enter: send · esc: close` hint is quiet in-panel text, tucked into the bottom right, with the same key accent.

- Fixed: the row focus highlight is now one continuous band across muted and stale cells; inverting their dim gray used to punch dark holes in it.

- Kanpan sets the terminal title (`kanpan`) while running: it re-takes the title after you detach from an attached agent session (which sets its own), and restores the previous title on exit in terminals that support the title stack.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

The kanpan plugin's cross-scope config merge now follows the same standard config-merge semantics as every other config field. Previously `KanpanPluginConfig` carried a custom `merge_with` that automatically *unioned* its six dict fields (`commands`, `data_sources`, `shell_commands`, `columns`, `on_before_refresh`, `on_after_refresh`) across config scopes (user < project < local), so a key set by a lower scope always survived. That method is removed and the merge now runs through the standard overlay pipeline: these fields assign-by-default, guarded by the cross-scope narrowing detector.

The user-visible effect: when a higher-precedence scope's `[plugins.kanpan]` block drops a key that a lower scope set in any of those six dict fields, it now raises the standard flag-gated settings-narrowing error (naming the scopes and how to opt in) instead of silently unioning the two scopes. To merge additively, use the `__extend` operator; to assign and drop keys without the warning, set `allow_settings_key_assignment_narrowing = true` (or use `key__assign`). Pure additions -- a higher scope that keeps every lower-scope key and adds more -- still apply unchanged.

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-15

`mngr kanpan --help` synopsis: replace placeholder `[OPTIONS]` with an enumerated list of the filter flags users typically reach for -- `[--include CEL] [--exclude CEL] [--running] [--stopped] [--archived] [--active] [--local] [--remote] [--project PROJECT]`. The full agent-filter set is still available; rarely-used flags (`--label`, `--host-label`) are omitted from the synopsis but remain on the command.

## 2026-06-14

Fixed the kanpan footer flickering when a background refresh and a user action (e.g. deleting a marked agent) ran at the same time. The footer is now driven by a single writer that picks what to show by priority (transient notification > active action > refresh spinner > marked-agent summary > steady text), so overlapping spinner loops can no longer overwrite each other on alternating ticks.

Batch operations in the kanpan TUI (delete, push, and markable custom commands) now surface failures instead of silently doing nothing. When `x` execution fails, the per-agent error detail (including a clear "timed out after Ns" message for timeouts) is listed at the bottom of the board, in the same place fetch errors appear, and persists until the next execution. The marks for failed agents are kept so you can retry.

## 2026-06-10

`mngr kanpan --format json` now prints a single board snapshot instead of launching the TUI, for programmatic use. The JSON has the ordered columns, agents grouped into sections (with human labels), and any fetch errors; each agent carries both the pre-rendered cells (text/url/color) and the structured field values (PR number, CI status, commits-ahead count, etc.).

`--format jsonl` is also supported: it emits one agent record per line in board order, followed by any error lines.

Previously `--format json` was accepted but silently ignored.

The GitHub data source now pages through PR search results instead of fetching only the first 100. Boards tracking more than 100 agents previously hit GitHub's hard per-page cap and silently rendered "Create PR" for the overflow agents; kanpan now follows the search cursor and fetches every page (up to GitHub's ~1000-result ceiling, beyond which it surfaces an explicit error).

Each page request is also retried with exponential backoff when GitHub returns a transient failure (HTTP 403 secondary rate limit, 5xx, or an unparseable body). A failure on a later page keeps the pages already fetched and retries only the failing page, so earlier results are never re-fetched.

Raised the stale coverage floor from 83% to 85% to match the coverage CI already measures (~86%).

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-01

Fixed a bug where muted agents could appear mixed in with the other rows
(typically alongside "PRs not loaded") whenever provider discovery transiently
failed during a refresh -- e.g. a flaky network connection to a remote
provider. Previously the board loaded the muted set with a separate
all-or-nothing discovery pass, so if any one provider failed to load, the
entire muted set came back empty and every agent was reclassified by its PR
state.

The muted flag is now surfaced as a regular agent field via kanpan's
`agent_field_generators` (online) and `offline_agent_field_generators`
(offline) hooks, so it rides on the same agent list the board already fetches
through `list_agents` -- which tolerates a failing provider. A provider failing
during a refresh no longer drops the muted classification of agents on the
providers that did load, and the muted bit is preserved for offline/unreachable
agents too.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test file), as required by `ty` 0.0.39.
- `_submit_batch_item` now dispatches on the command type with a `match` statement (`case MarkableBuiltinCommand()/ActionBuiltinCommand()/CustomCommand()`, with a `case _: assert_never(item.cmd)` catch-all) instead of an `isinstance` chain. This narrows `item.cmd` to `CustomCommand` before reading `.command` (which ty could not prove via the previous structure) and makes exhaustiveness explicit. Behavior is unchanged.
- Documented the urwid `Widget` -> `Text` downcast on a row's name cell with a `# ty: ignore[invalid-assignment]` (the first column is always a `Text` by construction, but urwid types `.contents` only as `Widget`).

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-26

Collapse the GitHub data source's four separate `gh` calls (`gh pr list --state open`, `gh pr list --state all`, `gh pr view --json mergeable`, `gh api graphql` for unresolved threads) into a single `gh api graphql` request per board refresh. The new query uses GitHub search's OR semantics over `repo:` and `head:` qualifiers to filter directly to the (repo, branch) pairs the agents need, and embeds `mergeable`, `statusCheckRollup { state }`, `reviewThreads`, and `comments` inline on every returned PullRequest. This eliminates the gh HTTP cache race that the lock-based fix was working around (no `gh pr list`, no SearchType introspection, no cache file to corrupt), provides an atomic point-in-time snapshot of the entire board, and cuts the refresh from 2M+2K HTTP calls (where M = unique repos, K = open PRs) to exactly one. Removes ~250 lines of fetch/parse/orchestration code along with the `_GH_PR_LIST_LOCK`, the `ThreadPoolExecutor`, and the conflicts/unresolved second-pass fetcher.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-08

## mngr_kanpan: staleness taint semantics

Field values now track when they were computed and render dimmed when older than a configurable threshold, surfacing potentially-out-of-date data at a glance.

- Added a required `created: datetime` field to every `FieldValue`. Values derived from cached inputs inherit the oldest `created` of the inputs they actually used (taint propagation); world-derived values use the current time.
- Added `staleness_threshold_seconds` to `KanpanPluginConfig`. Defaults to 90% of `refresh_interval_seconds` so values that weren't refreshed last cycle render as stale.
- Stale cells render in dark grey via new `stale` / `stale_focus` urwid palette entries. Muted-row dimming wins over per-cell stale dimming.
- `ShellCommandConfig` now declares its cached `inputs` explicitly so shell-derived staleness can propagate correctly. Shells with no declared inputs are treated as world-fresh.
