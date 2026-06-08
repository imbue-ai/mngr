# Unabridged Changelog - mngr_robinhood

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_robinhood/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-06

`mngr robinhood` can now surface an approximate, live view of the response as it is produced, sourced from the spawned agent's tmux-based `stream_buffer` (see `imbue-mngr-claude`).

- `--include-partial-messages` is now accepted (previously rejected). With `--output-format stream-json` it emits claude-native `stream_event` / `content_block_delta` / `text_delta` events as the response streams, followed by the authoritative `assistant` message from the transcript -- matching claude's native partial-message ordering.
- New `--stream-plain-text` flag: with the default text output, streams the response text to stdout incrementally and suppresses the trailing full-text dump so the streamed content is not duplicated.
- When either streaming flag is set, robinhood enables the streaming watcher on the spawned agent (`streaming_snapshot_interval_seconds = 0.25`) and defaults the model to sonnet (so fast mode is off and streaming is observable); a user-passed `--model` still takes precedence. Both flags are consumed by the wrapper and not forwarded to the spawned claude.
- The orchestrator reads `stream_buffer` over the host inside its existing end-of-turn poll loop, diffing the cumulative body against what it last emitted (prefix-extension -> append delta; reset -> new message) so deltas are pure appends. The streamed text is best-effort; the `result` envelope and the final `assistant` message remain the source of truth.
- `--include-partial-messages` requires `--output-format stream-json`, and `--stream-plain-text` requires the default text output; mismatches exit with code 2.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-mngr==0.2.8` / `imbue-mngr-claude==0.2.8` pins are realigned to the current `0.2.10`. No runtime change.

## 2026-06-05

Renamed the plugin from `mngr_uncapped_claude` to `mngr_robinhood`. The
PyPI package is now `imbue-mngr-robinhood`, the importable package is
`imbue.mngr_robinhood`, and the CLI command is now `mngr robinhood`
(previously `mngr uncapped-claude`). Spawned agents now use the `robinhood-`
name prefix and a `created-by=robinhood` label. Every occurrence of
"uncapped" was replaced with "robinhood" (case-preserving), including error
classes (`RobinhoodError`) and CLI option types. Behavior is otherwise
unchanged.

## 2026-06-04

Replaced the module-local `_get_local_host` helper with the shared `get_local_host` from `imbue.mngr.api.providers` (deduplication; no behavior change).

## 2026-06-02

`RobinhoodError` (the plugin's base error) now inherits from `MngrError` instead of
`BaseMngrError`, matching the repo-wide consolidation of the error hierarchy under a single
user-facing parent class. This also removes a prior inconsistency where its subclasses
(`UnsupportedClaudeFlagError`, `InvalidStreamJsonInputError`, `MissingPromptError`) were already
`MngrError` instances via `UserInputError` while the base was not. No behavior change.

Updated to the repo-wide error-hierarchy consolidation: `except BaseMngrError` handlers now use
`except MngrError` (`BaseMngrError` has been removed). No behavior change. The error-hierarchy
unit test (`errors_test.py`), which only documented the old two-tier distinction, was removed.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-25

### Add the missing changelog/ directory to mngr_robinhood

The recently added `mngr_robinhood` project shipped with
`CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` but no `changelog/`
directory for per-PR entry files, which left the project out of the
uniform changelog layout that every other project follows (and failed
`test_meta_ratchets.py::test_every_project_has_changelog_layout`).

This adds the `changelog/` directory (tracked via `.gitkeep`, matching
the convention used by every other project) so the nightly consolidator
can fan per-PR entries into the project's `UNABRIDGED_CHANGELOG.md` and
`CHANGELOG.md`. No behavior of the `mngr robinhood` command
changes.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

Add `mngr robinhood`, a new top-level command provided by the `imbue-mngr-robinhood` plugin. It acts as a drop-in replacement for `claude -p`: every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent that runs in-place in the current directory. The prompt is read from positional argv (or stdin), the agent runs to end-of-turn, the response is harvested from the agent's common transcript, and the agent is destroyed on exit.

- `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json) are simulated by the wrapper to shape stdin/stdout.
- The following flags are explicitly rejected with exit code 2 in v1: `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, `--session-id`.
- The spawned agent runs with `auto_dismiss_dialogs=True` and `auto_allow_permissions=True` so it never blocks on Claude Code dialogs or permission prompts.
- Per-agent `MNGR_*` and `LLM_USER_PATH` env vars are deliberately *not* forwarded from the parent process: those are set by mngr per-agent, and forwarding them would override the spawned agent's correct values and break the readiness hook (which writes to `$MNGR_AGENT_STATE_DIR`), the background-tasks script, and the common-transcript writer.

Also includes a small `imbue-mngr-claude` change unrelated to the env-var fix: `resolve_shared_claude_config_dir()` (used when a claude agent opts into `use_env_config_dir=True`) now falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset, instead of raising. The fallback matches claude's own default, so callers of that flag can treat it as a pure "don't touch the config dir" knob even on machines where the user never sets `CLAUDE_CONFIG_DIR`.

The `robinhood` CLI now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s "stdout/stderr contains only the response" contract. Previously mngr's own progress lines (`Creating agent state...`, `Starting agent ...`, `Sending initial message...`) leaked into stderr and broke scripts that parsed the output.

Also fixes an empty-`result` bug that surfaced for short turns: the orchestrator's end-of-turn detection was keyed on mngr's lifecycle `WAITING` state (derived from the `active` file), which is unreliable in two ways. First, it flickers briefly to `WAITING` during tool-permission auto-approval (the `PermissionRequest` hook touches `permissions_waiting`, elevating `RUNNING` to `WAITING` for a brief window), so the orchestrator could mistake mid-turn for end-of-turn. Second, even at the real end of turn the `Notification:idle_prompt` hook flips the file effectively the moment claude reaches end-of-turn, but `stream_transcript.sh` mirrors claude's per-session JSONL into `events.jsonl` only every ~1 second -- so the turn's final assistant message frequently hadn't been mirrored yet when the orchestrator finalized.

The orchestrator now polls the transcript directly for the only fully reliable signal: an `assistant_message` event whose `stop_reason` is terminal (`end_turn` / `max_tokens` / `stop_sequence`). It snapshots `writer.assistant_message_count` at turn start and waits until that count has grown AND the most-recent stop_reason is terminal -- which catches both simple text turns and multi-cycle tool turns correctly. The lifecycle state is consulted only as a fallback to detect agent death (STOPPED / DONE / REPLACED / RUNNING_UNKNOWN_AGENT_TYPE). A generous no-progress safety timeout (10 minutes of zero new assistant events while the agent is still alive) guards against `stream_transcript.sh` dying or the agent being wedged; users wanting tighter bounds wrap the command in `timeout(1)` per the spec.

Also refactors `imbue-mngr-claude-usage`'s statusline-shim provisioning to fix an infinite-recursion bug that surfaced when running successive claude agents in the same `work_dir` (as `mngr robinhood` always does). The shim and writer scripts now live at host-stable paths (`<host_dir>/commands/claude_statusline.sh` and `<host_dir>/commands/claude_usage_writer.sh`) shared by every claude agent on the host, rather than under each agent's state dir. The work_dir's `settings.local.json`'s `statusLine.command` therefore stays valid for the lifetime of the host -- it never references an agent state dir that might be destroyed -- and re-provisioning in the same work_dir is a no-op for that entry. The runtime sidecar (the captured user `statusLine.command` that the shim chains to) remains per-agent under `$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd`, which the shim dereferences via the env at render time. When the shim is invoked standalone -- i.e. `claude` is run outside of any mngr agent and `MNGR_AGENT_STATE_DIR` is unset -- it now exits 0 silently instead of erroring on every render. The provisioner also tolerates work_dirs whose `settings.local.json` still points at a legacy per-agent shim path: such entries are treated as mngr-owned and replaced with the stable path on the next provision.
