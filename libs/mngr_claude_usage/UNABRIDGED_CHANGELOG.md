# Unabridged Changelog - mngr_claude_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-18

Internal: reworded a docstring example of a config-defined Claude subtype to use a neutral `coder` name. No behavior change.

## 2026-06-17

The usage statusline-shim provisioner now filters agents with `isinstance(agent, ClaudeCoreAgent)` instead of `ClaudeAgent`. `ClaudeAgent` was split into a shared `ClaudeCoreAgent` base and an interactive TUI subclass, and `headless_claude` now extends the core (not the TUI subclass); switching the check to the shared base keeps the shim provisioned for `claude`, `headless_claude`, and claude-derived custom types exactly as before. Behavior is unchanged.

## 2026-06-16

Added an `aggregate_usage_source` reader hookimpl so Claude usage events are aggregated through the new `mngr_usage` reader hook rather than special-cased inside `mngr_usage`. It claims the `claude` source and aggregates it with the process-cumulative strategy (Claude Code reports cost cumulatively across a process, so a `/clear` that rotates `session_id` must not double-count); other sources are declined. No user-visible change -- the aggregated output is identical.

## 2026-06-12

Internal: routed `host_dir / "agents"` path constructions through the shared `get_agents_root_dir` / `get_agent_state_dir_path` helpers (now in `imbue.mngr.hosts.common`). No behavior change.

## 2026-06-10

Hardened the plugin test suite. Replaced the ad-hoc BaseModel agent/host stubs in the `on_before_provisioning` filter test with a real non-Claude `BaseAgent` on the real local host, so the test fails for the right reason if the agent-type guard regresses. Moved the `claude_statusline.sh` shim subprocess test into its own `test_shim.py` integration file (with a bash-availability skip), and moved the shared `writer_path` / `events_file` fixtures into `conftest.py`. Strengthened the writer event-timestamp assertion to validate the full nanosecond-precision UTC ISO 8601 shape, and the concurrent-append test to confirm every distinct event survives exactly once.

Raised the stale coverage floor from 80% to 95% to match the coverage CI already measures (~96%).

## 2026-06-08

- Now auto-discovered as a publishable package by the release tooling (the writer half of the usage split; pairs with `mngr_usage`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-mngr==0.2.6` / `imbue-mngr-claude==0.2.6` pins are realigned to the current `0.2.10`. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-mngr==0.2.6` / `imbue-mngr-claude==0.2.6` pins are realigned to the current `0.2.10`. No runtime change.

## 2026-06-05

Updated references following the `mngr_uncapped_claude` plugin rename: mentions
of the `mngr uncapped-claude` command (in the changelog and a test docstring)
now read `mngr robinhood`. No behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

Statusline shim refactor that fixes an infinite-recursion bug when running successive claude agents in the same work_dir (as `mngr robinhood` always does). The shim and writer scripts now live at host-stable paths (`<host_dir>/commands/claude_statusline.sh` and `<host_dir>/commands/claude_usage_writer.sh`), so the work_dir's `settings.local.json`'s `statusLine.command` stays valid across agent lifecycles. The runtime sidecar (captured user `statusLine.command`) remains per-agent at `$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd`. The shim exits 0 silently when `MNGR_AGENT_STATE_DIR` is unset (standalone `claude` invocations outside mngr), and legacy per-agent shim paths still in existing `settings.local.json` files are detected and overwritten with the stable path on the next provision pass.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-14

The Claude statusline writer (`mngr_claude_usage`) captures `rate_limits` +
per-render `session_id` + `cost.*` from Claude Code's statusline JSON, into
`events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`
since the file is no longer rate-limit-only). The event `type` is
`cost_snapshot`. The writer no longer skips emission when only `cost` is
present (no `rate_limits`), so cost tracking now works for direct
`ANTHROPIC_API_KEY` users -- Claude Code doesn't emit `rate_limits` for them
(it's Pro/Max only), but `cost` is always present. The writer script is
named `claude_usage_writer.sh` and reads `$MNGR_USAGE_EVENTS_PATH` for
the test override.

## 2026-05-12

- Events are appended by a per-agent statusline shim (in the `mngr_claude_usage` plugin) that captures the JSON snapshot Claude Code feeds to its statusline command on every render. The shim composes with any pre-existing user `statusLine.command` (the user's command runs after ours via `MNGR_USER_STATUSLINE_CMD`). All provisioning file I/O goes through `host.read_text_file` / `host.write_file`, so the shim works for local and remote agents (Modal, vps_docker, lima, ...) uniformly.

The Claude writer now also emits `window_seconds` per fixed-duration
window (`five_hour=18000`, `seven_day=604800`), enabling the reader to
derive `elapsed_seconds` / `elapsed_percentage` per window. These new
fields are surfaced in `mngr usage --format json` output (alongside the
existing `seconds_until_reset`) and are available to `mngr usage wait`
CEL predicates. Variable-duration windows (Claude's overage) intentionally
omit `window_seconds`, so the derived fields are `null` there.
