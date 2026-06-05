# Unabridged Changelog - mngr_kanpan

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_kanpan/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
