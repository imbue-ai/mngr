# Unabridged Changelog - mngr_robinhood

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_robinhood/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Trimmed the README to user-relevant content (removed the live SDK test docs and condensed the SDK divergence detail) and tightened it for concision.

## 2026-06-17

The streaming consumers now delegate their snapshot-diff bookkeeping to the agent's shared `LiveOutputReader` instead of each re-implementing it.

`_StreamBufferConsumer` (CLI orchestrator) and `StreamEventSynthesizer` (Agent SDK driver) both obtain a reader via `agent.make_live_output_reader()` and the buffer path via `agent.get_live_output_path()`, then call `reader.feed()` / `reader.finalize()` rather than tracking `emitted_body` / `last_content` themselves. The `stream_buffer` module (the `compute_stream_delta` diff helpers) moved to `mngr_claude`, where the snapshot format lives; this package imports them from there. No user-visible behavior change.

## 2026-06-16

Best-effort agent teardown (`stop_agent` / `destroy_agent`) and the SDK
restart-with-resume flow now also swallow the `CleanupFailedGroup` that `Host.stop_agents` /
`Host.destroy_agent` raise when cleanup leaves a resource behind, matching the existing
intent of logging and continuing rather than letting a teardown failure abort the run (or,
for restart, abort the relaunch).

## 2026-06-15

Fixed the robinhood streaming release tests (`test_streaming.py`), which drive a real
claude agent in tmux. They were missing `@pytest.mark.tmux`, so the resource-guard PATH
wrapper blocked their tmux usage and the robinhood subprocess exited 2. Added the mark
(plus a longer per-test timeout, since a real agent run far exceeds the default 30s).
Test-only change; robinhood's streaming behavior is unchanged.

## 2026-06-14

# Stream-json producers use the shared typed envelope

Both robinhood stream-json producers now build their events through the shared
`imbue.mngr_claude.stream_json` typed boundary instead of hand-rolled dicts:

- The CLI token stream (`output_modes.py`'s `emit_partial_text`) emits its
  `content_block_delta` / `text_delta` via the shared builder. The wire output is byte-identical.
- The Agent-SDK synthesizer (`_agent_sdk/stream_events.py`) builds its full framing sequence
  (`message_start` ... `message_stop`) via the shared builders, and the `assistant` summary's inner
  message is now constructed through `anthropic.types.Message`.

Because the framing events and the assistant message are now dumped from the `anthropic` Python
models, they carry that SDK's optional, null-valued fields (e.g. `content_block.citations: null`,
`tool_use.caller: null`, the `usage` cache/detail nulls). The token stream itself is unchanged and
stays byte-identical to the real `claude` binary. The other events are shape-compatible but not
byte-identical: the Python and TypeScript SDKs carry different optional fields, so the real binary
omits `citations` and emits a populated `caller`, among other differences. These departures are
cosmetic (consumers validate leniently) and documented in the `imbue.mngr_claude.stream_json`
module docstring.

## 2026-06-11

Strengthened the unit test suite after a bad-tests audit. No production behavior change; these
changes close gaps where a real regression would have slipped past CI:

- Removed the weak `test_build_pass_env_vars_is_populated` smoke test (it only asserted the result
  was non-empty): `build_pass_env_vars`'s forward-and-drop behavior is already covered meaningfully
  by the existing `..._drops_kitty_terminal_vars` / `..._drops_caller_tmux_session_vars` tests, and
  the per-agent `MNGR_*` drop is trivial set membership not worth a dedicated test.
- Added coverage for the `tool_result` stream-json conversion branch (including the `is_error`
  true/false cases), assistant `tool_use` content blocks, and `_parse_input_preview`
  (empty / valid-JSON / unparseable inputs).
- The user-event stream-json test now asserts the full converted message, not just the envelope type.
- Added raw-transcript coverage for tool-use input-preview rendering and truncation, usage
  conversion (including the empty-usage -> None case), tool_result output truncation, list-form
  tool_result content flattening, and mixed text + tool_result user messages.
- Tightened `test_rejected_flags_raise` to assert the per-flag rejection reason, and added a test
  for the inline `--flag=value` form of pass-through claude value flags.
- Strengthened the `monotonic_ms_since` test to guard the milliseconds scaling, and documented the
  deliberate timing margin in the polling-ticker test.
- Added a lightweight integration test (`test_cli.py`) that drives the real `mngr robinhood`
  command through the top-level CLI for the no-spawn failure paths: it asserts the command is
  registered, that each rejected flag maps to exit code 2, and that a bad `--output-format` value
  exits 2. This is the first end-to-end coverage of the click wiring and exit-code contract.

## 2026-06-10

Raised the stale coverage floor from 65% to 70% to match the coverage CI already measures (~74%).

## 2026-06-09

Updated an internal test to construct `EventsTarget` with its new single `host` field (replacing
the former `online_host`) after the events API was migrated to read through the unified host
file-read interface. No behavior change to the orchestrator itself.

## 2026-06-08

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr robinhood` CLI -- a drop-in `claude -p` replacement -- documented with `uv tool install imbue-mngr-robinhood`). It will be offered for first publication to PyPI on the next release, so those documented install instructions stop 404-ing. Its stale `imbue-mngr==0.2.8` / `imbue-mngr-claude==0.2.8` pins are realigned to the current `0.2.10`. No runtime change.

Add some docs about the SDK divergences for the Agent SDK
Update install instructions
Update robinhood README

## 2026-06-07

# mngr-backed Claude Agent SDK (`imbue.mngr_robinhood.agent_sdk`)

Added an alternative, mngr-backed implementation of the Claude Agent SDK Python interface,
importable as a drop-in replacement for `claude_agent_sdk`:

```python
from imbue.mngr_robinhood.agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
```

The new `imbue.mngr_robinhood.agent_sdk` module re-exports every SDK *type* verbatim from
`claude_agent_sdk` (so `isinstance` checks and field shapes are identical) and re-implements the
behavioral entry points on top of mngr: each session is a `robinhood-`-prefixed mngr claude agent,
driven through the in-process mngr API and read back from its native transcript. This works with
any agent mngr can run; v1 targets the claude agent type.

Implemented and verified live against the real API:

- `query()` (string and streaming-input prompts) and the full `ClaudeSDKClient` lifecycle
  (async context manager, `connect`/`disconnect`, `query`, `receive_response`/`receive_messages`,
  multi-turn on one connection with a stable `session_id`).
- The observable `ClaudeAgentOptions` subset: `model`, `system_prompt` (string + preset/append),
  `allowed_tools`/`disallowed_tools`, `permission_mode` (default/acceptEdits/plan/bypass),
  `cwd`, `add_dirs`, `env`, `settings`, `max_turns`.
- Built-in tool use end-to-end (Bash/Read/Write/Edit/Glob/Grep), with correlated
  `ToolUseBlock`/`ToolResultBlock`.
- Message/content-block/result type shapes, including a synthesized `system`/`init` message and a
  terminal `ResultMessage` (with `usage`, `model_usage`, durations, `session_id`, `uuid`).
- The session functions keyed by `directory`: `list_sessions` (newest-first, `limit`/`offset`),
  `get_session_info`, `get_session_messages`, `rename_session`, `tag_session`, with the documented
  `None`/`[]`/`FileNotFoundError` contracts. `session_id` is read from the transcript (never
  assumed equal to the mngr agent id, since claude rotates session ids).
- `resume` / `continue_conversation` by reusing and restarting the agent that owns the session.

Known limitations on the mngr transport (the tests skip the mngr target for these and still run
them against the real SDK): in-process `can_use_tool` / `hooks` callbacks, `interrupt`,
partial-message `StreamEvent` streaming, live `get_server_info`, `fork_session`, and
`total_cost_usd` (absent from claude's native session JSONL). `set_model` / `set_permission_mode`
are accepted but do not retroactively re-configure the already-running agent.

Test suite: the existing `test_sdk_*.py` live suite is parameterized over an `sdk` fixture so each
test runs against both the real `claude_agent_sdk` and the mngr implementation.

Also extracted shared agent-runtime helpers out of `orchestrator.py` into `agent_runtime.py`
(used by both the robinhood CLI and the SDK), and hardened env forwarding to drop shell-unsafe
values that could corrupt the agent's env file.

Added an opt-in, live integration test suite (`imbue/mngr_robinhood/test_sdk_*.py`) that
performs clean-room verification of the documented `query()` and `ClaudeSDKClient` interfaces
of the Claude Agent SDK (`claude_agent_sdk`) end-to-end against the real API. The suite covers
`query()` (string and streaming-input prompts), `ClaudeSDKClient` lifecycle and control
(`connect`/`disconnect`, multi-turn, `receive_messages`/`receive_response`, `set_model`,
`set_permission_mode`, `interrupt`), `can_use_tool` allow/deny and a `PreToolUse` hook,
message/content-block type shapes, the session functions (`list_sessions`,
`get_session_info`, `get_session_messages`, `rename_session`, `tag_session`), and documented
`FileNotFoundError` error paths.

The suite was then expanded with ~100 additional tests covering: field-level message contracts
(`SystemMessage` init data, `ResultMessage` usage/cost/duration/model-usage, `AssistantMessage`
metadata, `StreamEvent`); built-in tool use end-to-end (Bash/Read/Write/Edit/Glob/Grep, tool
use/result id correlation, failing-command `is_error`); more `ClaudeAgentOptions` behavior
(`env`, `allowed_tools`/`disallowed_tools`, `permission_mode` bypass/accept/plan, pinned
`model`, `system_prompt` string and preset, `add_dirs`, `cwd`); `ClaudeSDKClient` introspection
(`get_server_info`, `get_mcp_status`) and streaming input / partial messages; advanced
permission and hook behavior (`can_use_tool` input rewriting and deny semantics, `PreToolUse`/
`PostToolUse`/`UserPromptSubmit` hooks and matchers); and session continuation (`resume`,
`fork_session`, `continue_conversation`) plus `SDKSessionInfo`/`SessionMessage` field contracts
and `list_sessions` paging.

A dedicated `test_sdk_session_functions.py` adds thorough coverage of the five session
functions (`list_sessions`, `get_session_messages`, `get_session_info`, `rename_session`,
`tag_session`): `limit`/`offset` paging on real sessions, `list_sessions` newest-first
ordering, `SDKSessionInfo` field-value contracts (summary/tag/custom_title/file_size/created_at),
directory isolation, and rename/tag overwrite-and-clear semantics.

These tests make real, paid API calls and are excluded from all CI runs via the new `sdk_live`
marker; they only run when `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are both set (run them
with `just test-sdk-live`). Added `claude-agent-sdk` as a dependency and `pytest-asyncio` as a
dev dependency (`asyncio_mode = "strict"`). See the README's "Running the live SDK tests"
section for details.

# Finish the mngr-backed Agent SDK

Bug fix: `mngr robinhood` (and the Agent SDK) no longer forward the caller's tmux/terminal session
variables (`TMUX`, `TMUX_PANE`, `KITTY_*`) into the spawned agent's environment. When `mngr robinhood`
is run from inside a tmux/mngr session, forwarding `TMUX` pointed the new headless agent's tmux
machinery (readiness detection, transcript capture) at the *parent's* pane, so the agent never
signalled readiness and the command hung. (This was latent on `main` too, masked by `KITTY_PUBLIC_KEY`'s
unquoted-backtick value accidentally truncating the env file before those vars; this branch's env-file
hardening removed that accident and exposed the bug, now fixed properly in `build_pass_env_vars`.)

Completed the previously-stubbed control surfaces of the mngr-backed Agent SDK
(`imbue.mngr_robinhood.agent_sdk`) so it is a faithful drop-in for `claude_agent_sdk`:

- `can_use_tool` and `hooks` callbacks now fire in-process, served by a local HTTP bridge that
  the mngr claude agent calls via `--settings` hook commands (PreToolUse/PostToolUse/
  UserPromptSubmit). Permission allow/deny/`updated_input` all work, and denials are surfaced in
  `ResultMessage.permission_denials`.
- `ClaudeSDKClient.interrupt()` now ends an in-flight turn (the response stream is streamed
  incrementally and terminates at a `ResultMessage`); the next `query()` continues the conversation.
- `set_model` / `set_permission_mode` now take effect by rewriting the agent's stored launch
  command with the new configuration (via the agent's `set_command` API) and restarting it on the
  resumed session (previously a no-op); this can switch to a genuinely different model mid-session.
- `get_server_info()` returns real commands / output style from a one-shot `claude` stream-json probe.
- `ResultMessage.total_cost_usd` is computed from per-turn token usage and a per-model price table.
- `include_partial_messages` now yields `StreamEvent`s on the mngr target: the agent's tmux pane is
  watched (via mngr_claude's streaming `stream_buffer`) and the reconstructed assistant text is
  wrapped in the claude-native partial-event sequence (`message_start` -> `content_block_delta` ->
  `message_stop`). The event shapes conform to the real SDK; the text is approximate (reconstructed
  from the rendered pane, not claude's token-level deltas) and `usage`/`total_cost_usd` stay on the
  authoritative transcript-derived `ResultMessage`. Off by default; the caller's model is honored
  (the SDK does not force sonnet). The `stream_buffer` parse/diff logic is shared with the robinhood
  CLI streaming path via a new `stream_buffer.py` module.

The corresponding live tests are now unskipped for the mngr target (they previously ran only
against the real SDK). The README documents the dual-target live suite and the supported control
surfaces.

`fork_session` remains real-SDK-only: claude's `--fork-session` does not assign a new session id
when driven interactively over an adopted, resumed session, so the mngr-backed SDK raises
`AgentSdkNotImplementedError` rather than returning a wrong/duplicate id.

Fixed duplicated paragraphs in `mngr robinhood`'s live streaming output (`--stream-plain-text` and `--include-partial-messages`).

- When Claude's TUI reflowed already-rendered text as later text streamed in -- most visibly collapsing a blank line around a markdown horizontal rule (`---`) as the following paragraph arrived -- the stream-buffer body was no longer a clean prefix-extension of what had already been emitted. The delta computation then re-emitted everything past the (character-level) divergence point, and because plain-text output cannot be unprinted, the already-printed region appeared a second time.
- `compute_stream_delta`'s divergence branch now recognizes already-emitted content across whitespace reflow (treating whitespace runs as equivalent and absorbing collapsed/added blank lines), so only genuinely new content is emitted. At worst a little already-printed whitespace is left stale; no visible content is duplicated.

Added tmux window-sizing flags to `mngr robinhood`: `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`).

- The spawned agent's tmux window now defaults to a large, pinned size (`2048` columns x `256` rows, `manual`) so the live-streamed response -- reverse-mapped from the rendered tmux pane -- is no longer chopped into hard line wraps at a narrow pane width.
- All three flags are consumed by the wrapper (not forwarded to claude); invalid values exit with code 2.

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
