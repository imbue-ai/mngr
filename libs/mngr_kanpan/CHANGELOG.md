# Changelog - mngr_kanpan

A concise, human-friendly summary of changes for the `mngr_kanpan` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.3.0] - 2026-10-22

### Added

- Added: Attach, peek, and reply to the focused agent from the kanpan board. `Enter` attaches to the agent's tmux session and restores the board on detach (`Ctrl-b d`). `Space` opens a live bordered peek panel below the board showing the agent's recent user/assistant conversation via `mngr transcript --role user --role agent`, refreshed every couple of seconds; tool calls and framework-injected turns don't appear, so the peek reads like the human conversation. Type into the panel's `›` input and press `Enter` to send a message to the agent (the send runs in the background via `mngr message`; your reply is echoed as a `›` line immediately and replaced by the real message once the agent accepts it, so several replies typed in a row deliver in order; a failed send drops the echo and renders a `(reply failed: ...)` line). The reply input supports readline-style editing via `urwid_readline` (word movement/delete, jump-to-start/end, kill-to-start/end). A `?` overlay lists the full keymap; the footer shows a relative refresh stamp on the left and the core keys on the right.
- Added: `/` search: as you type, the selection jumps to the best-matching row across every visible column (name prefix, then name substring, then any cell); `↑`/`↓` step through matches, `Enter` keeps the match, `Esc` returns to the starting row. The board is never filtered.
- Added: `prompt` on a `[plugins.kanpan.commands.<key>]` entry asks for a value before the command runs and passes it as `MNGR_INPUT`; combined with `markable`, one prompt applies to the whole batch.
- Added: `header_status` config: a header line of CEL counts over the board's agents, e.g. `'{state == "RUNNING"} running / {total}'`.
- Added: `local_refresh_interval_seconds` (default 30; `0` disables) runs the local refresh on a timer, so `STATE`, `commits_ahead` and label columns stay current between full refreshes.
- Added: `batch_concurrency` (default 4): marked operations executed with `x` run concurrently, off the refresh worker; the footer counts progress (`Executing 2/5`) and quitting drops operations not yet started.
- Added: Custom commands receive `MNGR_AGENT_ID` and `MNGR_HOST_ID` (`"$MNGR_AGENT_ID@$MNGR_HOST_ID"` addresses the exact agent), and `--format json`/`jsonl` entries carry `agent_id`.

### Changed

- Changed: `[plugins.kanpan]`'s six dict fields (`commands`, `data_sources`, `shell_commands`, `columns`, `on_before_refresh`, `on_after_refresh`) no longer auto-union across config scopes (user < project < local). A higher-precedence scope assigns by default and raises the standard flag-gated settings-narrowing error when it would drop a lower-scope key. Use `__extend` to merge additively, or `key__assign` / `allow_settings_key_assignment_narrowing = true` to opt out of the guard. Purely additive cross-scope overrides still load unchanged.
- Changed: Kanpan sets the terminal title (`kanpan`) while running, re-takes it after you detach from an attached agent session, and restores the previous title on exit in terminals that support the title stack.
- Changed: The `PR` column is now `PRS`: it lists the PRs of every branch the agent's worktree has checked out (read from its HEAD reflog), leading with the recorded branch's PR, each a separate hyperlink. `--format json`/`jsonl` cells gain a `runs` list (per-segment text, url and color).
- Changed: Label-backed columns blank as soon as the label is cleared, and in `--format json`/`jsonl` a label-backed column's key is always present in `fields` and `cells` (empty when the agent lacks the label).
- Changed: A `refresh_interval_seconds` of zero or less is refused at startup.

### Fixed

- Fixed: Row focus highlight is now one continuous band across muted and stale cells (previously inverting their dim gray punched dark holes in it).
- Fixed: Kanpan identifies agents by id instead of name: marks, delete, mute and focus key on the (host, agent id) instance and cached column data on the agent id, so same-named agents on different hosts are kept apart. Previously deleting one ran `mngr destroy <name> --force` and destroyed both.
- Fixed: Muting resolves the agent through its own provider only, so a slow or unreachable unrelated provider no longer fails the write. A refresh already in flight no longer flashes a just-muted row back to its old section, and pressing `m` again in that window no longer unmutes it.
- Fixed: A repaint requested by an action (push, delete, a `refresh_afterwards` command) while a refresh is running is held and run afterwards instead of dropped, and the periodic full refresh no longer stops for the session when its timer fires during another refresh.
- Fixed: Executing a delete mark keeps the view in place, moving focus to the nearest surviving row instead of jumping to the top.

## [v0.2.17] - 2026-06-18

## [v0.2.16] - 2026-06-16

## [v0.2.15] - 2026-06-16

### Changed

- Changed: `mngr kanpan --help` synopsis now enumerates the commonly-used filter flags (`--include` / `--exclude` / `--running` / `--stopped` / `--archived` / `--active` / `--local` / `--remote` / `--project`) instead of showing `[OPTIONS]`.

## [v0.2.14] - 2026-06-15

### Fixed

- Fixed: The kanpan footer no longer flickers when a background refresh and a user action (e.g. deleting a marked agent) run at the same time. A single writer now picks what to show by priority, so overlapping spinner loops can't overwrite each other on alternating ticks.
- Fixed: Batch operations in the kanpan TUI (delete, push, markable custom commands) now surface per-agent failure details (including a clear "timed out after Ns" message) at the bottom of the board instead of silently doing nothing. Marks for failed agents are kept so you can retry.

## [v0.2.13] - 2026-06-13

### Added

- Added: `mngr kanpan --format json` now prints a single board snapshot instead of launching the TUI — the JSON has ordered columns, agents grouped into sections with human labels, and any fetch errors; each agent carries both the pre-rendered cells and the structured field values (PR number, CI status, commits-ahead count, etc.). `--format jsonl` emits one agent record per line in board order, followed by error lines. Previously `--format json` was accepted but silently ignored.

### Fixed

- Fixed: GitHub data source now pages through PR search results instead of fetching only the first 100. Boards tracking more than 100 agents previously hit GitHub's hard per-page cap and silently rendered "Create PR" for the overflow agents; kanpan now follows the search cursor (up to GitHub's ~1000-result ceiling, beyond which it surfaces an explicit error).
- Fixed: Each GitHub page request is now retried with exponential backoff when GitHub returns a transient failure (HTTP 403 secondary rate limit, 5xx, or an unparseable body). A failure on a later page keeps earlier pages and retries only the failing page.

## [v0.2.12] - 2026-06-08

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

### Fixed

- Fixed: Muted agents no longer appear mixed in with other rows (typically alongside "PRs not loaded") when provider discovery transiently fails during a refresh. The muted flag now rides on the same agent list the board already fetches via the `agent_field_generators` (online) and `offline_agent_field_generators` (offline) hooks, so a single provider failing during a refresh no longer drops the muted classification of agents on providers that did load, and the muted bit is preserved for offline/unreachable agents too.

## [v0.2.9] - 2026-05-28

### Changed

- Changed: GitHub data source now refreshes via a single `gh api graphql` request per board cycle (with mergeability, status checks, review threads, and comments embedded inline), replacing the four separate `gh` calls and eliminating the gh HTTP cache race.

## [v0.2.7] - 2026-05-11

### Added

- Added: `mngr_kanpan` field-value staleness — each `FieldValue` carries a `created` timestamp, taint propagates through cached inputs, and stale cells render dimmed; new `staleness_threshold_seconds` config.

### Fixed

- Fixed: `mngr kanpan` no longer logs per-agent CEL warnings for `--include` / `--exclude` filters that reference keys on tolerant schemaless fields.
