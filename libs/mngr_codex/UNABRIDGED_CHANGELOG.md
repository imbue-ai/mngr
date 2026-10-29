# Unabridged Changelog - mngr_codex

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-27

Lowered the trailing-comment ratchet count now that the shared rule no longer counts bare `# noqa` directives.

## 2026-10-26

Codex agents now read their process presence from the host's shared lifecycle snapshot, so a listing reads tmux and `ps` once per host rather than once per agent. When that snapshot can't be read, a codex agent reads UNKNOWN rather than STOPPED.

A laptop-side `mngr list` of a remote host (modal, vps, imbue_cloud) no longer reads every live codex agent as DONE. That listing knew only claude's process name and looked for the first word of codex's launch command, `(`, which never runs. The process name `codex` is now declared on the agent class, where that listing reads it.

## 2026-10-24

The `codex app-server` socket directory name now hashes the agent's codex home with `hashlib.sha1(..., usedforsecurity=False)`, marking the hash as a path shortener rather than a security control. This resolves Bandit's B324 (`hashlib`) finding on `get_codex_app_server_socket_path`; the socket path itself is unchanged.

## 2026-10-23

The codex `common_transcript.sh` tests now stop their background poll daemon with `running_in_own_process_group` from `imbue.mngr.utils.testing` (MIND-377), the same stop the claude watcher tests use.

The old stop sent SIGTERM to the daemon's process group and returned as soon as the daemon's bash process had exited. Two kinds of process could outlive it: a child being forked at the moment of the signal, which gVisor (the kernel in CI's Modal sandboxes) lets join the group after the signal has gone out, and any other process left in the group once bash was gone. A stand-in that rejoins the group after the signal stayed alive 3 times out of 3 against the old stop. The new stop sends SIGKILL to the group until every process in it has exited, and only then reaps bash.

`_start_watcher` and `_stop_watcher` are replaced by a `_running_watcher()` context manager, so a test cannot start the daemon without stopping it. The daemon's stderr now goes to pytest's captured output instead of `/dev/null`.

Tests only. No change to the shipped script or to codex agents' behaviour.

## 2026-10-22

Test-only: the test stand-ins for the host file reader accept the new optional `max_byte_count` argument of `read_file_tail_from_offset`.

## 2026-10-20

The codex `waiting_reason` field generator's docstring no longer claims its registration limits it to codex agents; field generators run for every listed agent, and the existing `isinstance` guard is what keeps the field off other types.

## 2026-10-16

A renamed codex agent keeps working.

- A codex agent that was renamed and then stopped and started came back without its app-server, so its TUI could not connect and the agent sat stopped: its stored launch command spawned the app-server window into the session's old name. The launch command now finds its session when it runs, and `mngr start` upgrades the stored command of an agent created before this change.

- The transcript keeps streaming after the agent (or its minds chat) is renamed, so `mngr transcript` keeps growing. The background-tasks supervisor that runs the raw transcript streamer, the common transcript converter and the usage writer tracks the agent's tmux session by its session id, which a rename does not change. For an agent created by an older mngr, `mngr start` also rewrites the session name its stored command passes the supervisor, so its transcript runs again after its next restart.

- The supervisor's pidfile is keyed on the agent id, so a new agent that takes a renamed agent's old name gets its own supervisor.

## 2026-10-14

`CodexAgent.request_compaction` supports the bounded message-lock wait and idle re-check that mngr's autocompact plugin uses. With `message_lock_timeout_seconds` it raises `MessageLockTimeoutError` instead of waiting on another send, and with `expected_idle_since` it skips with `AgentNoLongerIdleError` when the agent is no longer idle since that moment. A skipped compaction sends nothing and records nothing. The idle start a compaction records is read once the lock is held.

The autocompact eligibility check reads a Codex agent's idle timestamp and context token count from one backward scan of each rollout or transcript file's tail, cached between checks, rather than parsing whole files. An unchanged file costs no reads of its complete lines, and a grown one is read only from where the last check stopped. The values found match a whole-file parse, including skipping the task completion of a compaction turn named by a newer compaction record, except that a final line without a trailing newline is read once its newline arrives, and a record containing U+0085, U+2028 or U+2029, which `str.splitlines()` splits apart, is read whole. Removed the whole-text parsers `extract_latest_assistant_timestamp_from_jsonl` and `extract_context_tokens_from_jsonl`; added `CodexCompactionTranscriptScanner` and the per-record `extract_context_tokens_from_record`.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-08

Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio". No behavior change: every identifier (`MINDS_*` env vars,
`minds-*` service and scope names, paths) keeps its spelling, per
`apps/minds/style_guide.md`.

Review follow-up to the Imbue Studio prose sweep: the rollout transcript
fixture's comment said "running with Minds' codex features" and now names the
product Imbue Studio.

## 2026-10-07

- The codex app-server client can sign in to ChatGPT: `start_chatgpt_login()` (browser login, returns the authorize URL), `start_device_login()` (device code), and `wait_login_completed()`, which waits for the `account/login/completed` notification of that login.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-30

Removed the 17 comment lines that had accumulated inside `libs/mngr_codex/imbue/mngr_codex/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-29

A codex agent's app-server socket lives in a directory of its own, `/tmp/mngr-codex-<hash>/app-server.sock`, which the daemon's launch creates at mode 0700. codex 0.157 and later refuse to bind a socket whose parent other users can write to, so a socket directly in a `/tmp` that is world-writable without the sticky bit (as on Modal sandboxes) keeps the daemon from starting, and codex creates there fail after their 45 s readiness wait.

## 2026-09-28

New `fall_back_to_account_default_model` setting for the `codex` agent type (default `false`). With it on and `model` pinned, mngr reads the account's `model/list` before it starts a thread (the root conversation it establishes at create, and a fresh thread on the send path). If the account does not offer the pinned model, the thread starts on the account's default model instead, and mngr logs a warning naming both. Without it, a pinned slug the account's plan cannot use fails every turn with "model is not supported when using Codex with a ChatGPT account". A thread that already exists keeps its model, so a model switched mid-conversation survives a restart. If the model list cannot be read, the thread starts on the pinned model as before.

`CodexAppServerClient.model_list` now raises `CodexAppServerError` for a malformed `model/list` entry (one missing a required field) instead of a raw pydantic `ValidationError`, so every caller treats it like any other unreadable model list.

## 2026-09-24

A codex agent's ATIF trajectory now reports which model ran each step and what it cost.

The common-transcript converter reads two rollout record types it previously discarded, and neither becomes a turn of its own:

- `turn_context` supplies the turn's model and reasoning effort, stamped as `model_name` and `reasoning_effort` on every agent step of that turn.

- `event_msg` / `token_count` supplies one model response's token usage, stamped as ATIF `metrics` (`prompt_tokens`, `completion_tokens`, `cached_tokens`, plus cache-write and reasoning-output counts under `metrics.extra`) on the last agent step that response produced. codex counts cached and cache-written input inside `input_tokens` and reasoning output inside `output_tokens`, which is already ATIF's convention, so the buckets are carried across rather than summed -- summing the per-step metrics over a trajectory reproduces codex's own running session total, and `mngr transcript --format atif` fills in `final_metrics` for codex the way it already did for claude. The sum falls short when a response leaves no agent step to carry its usage (one whose only message is empty), and differs from codex's final figure when codex itself resets or lowers its running total mid-session. A `token_count` that repeats the previous one's running total, which codex sometimes writes partway through a later response, measures nothing.

Every other `event_msg` but a shell command's `item_completed` (below) stays ignored: the rest are display duplicates of response items.

An agent step that no `token_count` ever closes -- an aborted or interrupted response -- carries no `metrics` at all rather than a fabricated row of zeros a reader would price as a free inference. Because codex writes the usage line only after the response items it measures and the outputs of the tool calls they made, a conversion pass over a still-growing rollout holds the trailing unmeasured step back until the usage lands. The converter daemon releases it on the next cycle once the rollout has stopped growing, so a response whose usage never arrives is never stranded -- except while a tool call of that response still awaits its output, however long the tool runs, since its usage is still coming.

On a rollout codex writes in its paginated history mode (codex 0.154 does by default), each shell command a tool call ran also reaches the trajectory. codex's `CommandExecution` item becomes an extra observation result on the call that started the command -- a code-mode `exec` program can start several -- with an empty `content` and the command under `extra.command_execution`: the command line and argv, working directory, status, exit code, duration, start and end times, and its output, clipped to 16,000 characters with the full length recorded when it was longer. That is what actually ran, including a command the program built at run time and one that exited nonzero inside a program that did not fail. codex does not say which call started a command, so ownership is read from time: the program that was running when the command started, told apart by its own text when two were running and otherwise by the turn codex recorded the command in, which also picks among earlier calls for a command that started while no program was running; `attribution` says which rule decided. Rollouts in the legacy history mode carry no such items and convert as before.

## 2026-09-23

Implement `HasCompactionMixin` for the Codex CLI agent (`CodexAgent`), enabling context compaction via the Codex app-server's `thread/compact/start` JSON-RPC method, idle duration tracking, cache TTL reporting (30 minutes), and context token inspection from transcript events.

## 2026-09-21

When mngr cannot press through codex's "Hooks need review" screen at create, it no longer waits the full attach timeout for a TUI that is still on that screen, and no longer logs a second warning saying the screen was cleared. The create goes on as soon as the keypress fails, with the one warning that says the hooks may stay untrusted.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-17

Persisting a trusted source repo into the user's global Codex `config.toml` writes that file in place (`is_atomic=False`) rather than through the atomic rename that `write_text_file` now defaults to, so a `config.toml` that is a symlink into the user's dotfiles stays a symlink and keeps its mode and owner.

## 2026-09-16

Carries `mngr/chat-agent-refactor-8` into this branch: a codex agent created with an initial message no longer has that first turn cut off, since mngr waits for the visible TUI to finish attaching after the "Hooks need review" screen before it sends the message. No further mngr_codex changes in this PR.

## 2026-09-15

A codex agent created with an initial message no longer has that first turn cut off. Once mngr presses through codex's "Hooks need review" screen, it now waits for the visible TUI to finish attaching before it sends the message. Before, the freed TUI's own resume could shut the just-started thread down and rebuild it, which aborted a turn that had begun a few milliseconds earlier and left the message unanswered ("Conversation interrupted" in the pane). The wait is bounded and a TUI that never shows its composer is logged, not fatal.

Codex chats remain reachable for sending messages after their history grows beyond 1 MiB. The local WebSocket client now accepts the complete history returned by the Codex daemon instead of disconnecting during resume or status reads.

## 2026-09-12

`RUST_LOG` for the codex daemon is unchanged, and now records why. Adding `codex_app_server=info` looks like the obvious way to get the request and connection behind each daemon log line, and it does -- but codex builds the app-server's stderr layer with `FmtSpan::FULL`, so enabling one of its spans also synthesizes an `enter`/`exit` record every time the runtime polls the underlying future. Measured on a live workspace: 10,660 of each from 82 actual requests, 99% of the file's bytes, and the log grew about a megabyte a minute on an idle agent with no rotation to stop it. `RUST_LOG` cannot separate the two, because EnvFilter decides only whether a span is enabled and disabling it is what drops the attribution. The same request and connection attribution is already recorded, without the lifecycle records, in the app-server's own SQLite log database, which the bug-report collector now reads instead.

## 2026-09-01

Trimmed historical framing from a test comment (comments state the current constraint, not the migration history).

The codex common-transcript emitter now writes ATIF-shaped stream records
(`header` / `step` / `observation`) instead of the legacy
`user_message` / `assistant_message` / `tool_result` envelopes, per
`specs/atif-transcript-alignment/spec.md`. Every stream opens with a
`header` line pinning `ATIF-v1.7`, and the envelope's `source` field is
renamed `emitter` (ATIF claims `source` for the step originator).

Full fidelity: tool `arguments` is the complete parsed JSON object (a
`custom_tool_call` script or any other non-object invocation rides whole under
`_raw`) and tool output is recorded untruncated. The 200-character input
previews and 2000-character output cap are gone; truncation is now purely a
display-time concern in `mngr transcript`.

Tool calls carry codex's native `call_id` as the ATIF `tool_call_id`, so
results pair back to their call without a synthetic id. An output whose call
was never seen is no longer dropped -- it is emitted with the tool name
`unknown` so the output is never lost.

Newly captured: rollout `reasoning` items become agent steps carrying
`reasoning_content` (dropped only when the item exposes nothing but its
encrypted payload), and codex's instruction injections -- the AGENTS.md context
blob and the `<user_instructions>` / `<environment_context>` initial-context
items -- become system steps carrying their full text instead of being silently
dropped.

An assistant message carrying no text no longer produces an empty agent step,
matching how an empty user message is dropped. codex records a tool invocation
as its own rollout item, so such a message has nothing left to show.

The ATIF stream header's `event_id` is now unique per agent stream: `header-<sha256(agent_id:emitter)[:32]>` instead of the fixed `"header"`. A fixed id repeats identically for every agent on every host, so analytics' fleet-wide event-id dedupe collapsed all header rows to one per account, destroying the per-agent (emitter, `schema_version`) mix.

The agent id is the agent state directory's basename (a UUID4-based value), so distinct agents never collide and a restart reuses the same id.

The codex common-transcript emitter now writes ATIF-shaped stream records (`header` / `step` / `observation`) per `specs/atif-transcript-alignment/spec.md`: full-fidelity tool arguments and outputs, native `call_id` pairing, `reasoning` items and instruction injections captured instead of dropped, and a per-agent-unique header `event_id`. The per-layer `mngr-atif-*.md` entries in this PR carry the details.

The app-server websocket transport now raises its typed `TransportClosedError` from `send` as well as `receive`, so a request written to a connection that has closed surfaces as the same retryable transport error regardless of which side of the round-trip hits the closure first (previously a write-side closure escaped as a raw `websockets.ConnectionClosed`).

## 2026-08-30

Marked `test_assemble_command_is_posix_compatible` as `@pytest.mark.flaky` with a 60s timeout: it shells out to shellcheck and was observed exceeding the default 10s pytest-timeout under CI sandbox load (12.348s).

## 2026-08-27

# Globally unique codex common-transcript event ids

- Common-transcript event ids are now derived by hashing the rollout line's timestamp and the first 1024 characters of its content (plus the item kind), instead of the line index alone. Line-index ids (`line-3-user`) repeated identically for every codex agent on every host, which collides under fleet-wide dedup by event id (e.g. in analytics). Re-processing the same input stays idempotent, and outputs written by the old converter are still recognized so upgrading never re-appends already-converted history.

## 2026-08-26

Renamed `auto_dismiss_dialogs` to `auto_dismiss_dialogs_at_startup`. Same behaviour; the name now says when it applies.

## 2026-08-25

Release-test helpers' `workspace`/`prepare_adoption_workspace` identifiers renamed to `project_dir`/`prepare_adoption_project_dir` (mngr-level terminology sweep; no behavior change).

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-24

mngr no longer writes hook commands of its own choosing into a codex agent's `hooks.json`.

`build_codex_hooks_config` now emits only mngr's own bookkeeping hook -- `UserPromptSubmit` -> `record_session_pointers.sh`, which records the rollout session id and transcript path. The fixed list of guard commands it used to carry is gone.

Codex loads hooks from every active config layer and does not let a higher-precedence layer replace a lower one, so a repo that wants guards on its own agents puts them in its `.codex/hooks.json`; they run alongside mngr's. Nothing is needed from mngr for that: the plugin already marks the work dir trusted and passes `--dangerously-bypass-hook-trust`, which is what a project layer's hooks require.

## 2026-08-17

Fixed intermittent "Sending…/Queued…" hangs (and eventual send failures) when
messaging a codex agent after a turn that produced substantial output. The
pre-send readiness poll looked for codex's `/model to change` header, which sits
at the top of the TUI and scrolls out of the visible pane once a turn renders
enough output -- so the poll could not confirm the composer was ready and
withheld the paste until it timed out. Readiness now keys off the composer
prompt glyph (`›`), which is pinned at the bottom input line and never scrolls
off -- the same approach `mngr_claude` already uses with `❯`.

The codex agent type gains two settings, `output_style` and `append_system_prompt`, that a
create template can set so a role describes codex's behaviour without spelling out argv.

Codex has no output-style concept, so both land in the top-level `developer_instructions` key of
the per-agent `config.toml` -- the key that appends to codex's built-in instructions (unlike
`model_instructions_file`, which replaces them). The `append_system_prompt` blocks go in first,
in stack order, then the style file's body verbatim, frontmatter block included, so a style
reads the same whichever harness runs it. The style comes last so it is the nearest instruction
to the model, matching how a harness with a real output-style setting applies the style over the
prompt. Style names are resolved from `.agents/output-styles/` in the work dir.

Known limit: a style that suppresses a harness's built-in prompt cannot behave identically here,
because `developer_instructions` can only append.

A message submitted to a codex agent while a turn is running is now confirmed as accepted the
instant it is queued, rather than hanging until the running turn ends. codex queues such a
message without firing `UserPromptSubmit`, so the `active` marker -- the previous sole evidence
of submission -- does not advance until the turn finishes. Send confirmation now also watches the
queued-input sidecar the patched codex binary appends to on every enqueue, so a queued message
confirms immediately (the two probes are OR-ed; a started message still trips the marker). Agents
on an older codex binary without the sidecar are unaffected: that probe simply never fires.

Codex agents now enforce the same PreToolUse policy guards claude does. `build_codex_hooks_config`
adds a `PreToolUse` entry that runs the workspace's existing guard scripts from the work dir
(`$MNGR_AGENT_WORK_DIR/system/scripts/`): block a command that pipes into `tail`/`head`, block
`git rebase` / `git commit --amend|--fixup` / `git pull --rebase`, and rewrite every bash command
with the OOM self-tag + the agent's git identity. Codex speaks claude's hook protocol (same
`PreToolUse` payload and the exit-2+stderr block convention, verified live under code mode), so it
reuses the exact same scripts with no new logic. See `system/scripts/POLICY_HOOKS.md`.

Fixed the command-rewrite guard failing under recent codex (verified against codex-cli 0.146.0),
which surfaced as repeated `PreToolUse hook returned updatedInput without permissionDecision:allow`
errors and meant the OOM self-tag + git-identity rewrite never applied on codex. Newer codex
requires a hook that returns `updatedInput` to also carry an explicit `permissionDecision: "allow"`;
`build_codex_hooks_config` now invokes the rewrite script with a `--codex` flag that adds that
decision. The two block guards are unaffected (they return no `updatedInput`), and the rewriter's
`allow` does not weaken them -- codex honors an earlier block over a later allow (verified live).

Codex agents now also run the tk workflow-discipline guards, matching claude: `build_codex_hooks_config`
adds the require-steps soft reminder and the non-standalone `tk start`/`close` block to `PreToolUse`,
the open-steps carryover reminder to `UserPromptSubmit` (with a `--codex` flag so it is emitted as
`additionalContext` JSON -- codex rejects UserPromptSubmit stdout that starts with `[`), and the
open-steps stop nudge to `Stop`. All are the same dwt scripts claude runs, from the work dir. Verified
live against codex-cli 0.146.0. See `system/scripts/POLICY_HOOKS.md`.

Deleted the retired marker-lifecycle files that the app-server rewrite had already
orphaned: `set_active_marker.sh`, `clear_active_marker.sh`, `codex_marker_state.sh`,
`subagent_started.sh`, `subagent_stopped.sh` and their four test files. They tested
helpers (`run_codex_hook`, `install_common_transcript_flush_stub`,
`SUBMIT_WAIT_CHANNEL_PREFIX`) that no longer exist, so they failed at collection.

Rebuilt the `codex` agent to run entirely over the stock `codex app-server` (JSON-RPC) instead of screen-scraping the TUI, while preserving every mngr harness contract at 1:1 parity with the TUI form. The app-server is invisible to clients: `mngr create` / `message` / `stop` / `start` / `connect` / `destroy` and `mngr ls` lifecycle behave exactly as for claude and pi.

- `CodexAgent` now subclasses `BaseAgent` (like pi/opencode) with its own `send_message` (`turn/start` when idle, `turn/steer` when a turn is running) and a `thread/status`-derived RUNNING/WAITING lifecycle; the old `tmux send-keys` drive and the on-disk marker hooks are gone.

- `mngr create` establishes ONE durable root conversation up front — `thread/start` + `thread/inject_items` materializes the rollout with no model turn — and persists it, so the terminal (`codex resume <id> --remote`), `mngr message`, and Minds all land in the same conversation; `mngr stop`/`start` resumes it.

- The daemon now launches with `--dangerously-bypass-hook-trust --enable hooks`, and create auto-clears codex's one-time "Hooks need review" screen, so the workspace safety hooks (PreToolUse guards) and the transcript recorder actually run — on typed AND web/CLI turns.

- Fixes found by driving the real release lifecycle: the unix-socket path moved to a short `/tmp` location (a deep state dir exceeded `SUN_LEN`, so the daemon could not bind); mngr records the rollout path itself (`codex_transcript_path`) since the hook that used to does not fire before the first turn; the transcript supervisor no longer races the executable-bit on its scripts; and an `--adopt`/`--from` agent gets its send/transcript pointers written so it recalls its resumed history.

The codex app-server daemon sidecar window now inherits the agent environment (the same env the
visible `--remote` TUI already sources). Turns run in the daemon, so it is where codex fires its
hooks; the detached `tmux new-window` previously started with a bare environment, so every hook
resolved `$MNGR_AGENT_STATE_DIR/...` / `$MNGR_AGENT_WORK_DIR/...` to a nonexistent path and died
with exit 127 -- silently disabling the transcript-marker writer and the PreToolUse safety guards.

The visible `codex --remote` terminal now launches with `--dangerously-bypass-hook-trust`. codex
parks newly-discovered command hooks as untrusted ("in review") until a human approves them in the
TUI, and an untrusted hook never runs -- not for a typed turn and not for a programmatic
(web/CLI) `turn/start`. The flag clears that review gate so the workspace's safety hooks (the
pipe/rebase blockers, the OOM + git-identity command rewrite, and the tk workflow guards) run on
every turn regardless of who sent it. It is consent-gated: it only takes effect on a workspace the
user has already trusted.

Fixed a latency bug where a codex Stop/shoulder-tap could block for seconds during a heavily-streaming turn. `CodexAppServerClient.poll_notifications` held the frame lock for the whole drain, waiting up to `timeout` for *each* subsequent frame -- so while a turn streamed continuously the background reader pinned the lock and a concurrent fire-and-forget `interrupt_nowait` waited behind it (observed ~3.8s under load). The drain now waits `timeout` only for the first frame and drains the rest of the already-buffered burst non-blocking, then releases -- so an interrupt cuts in promptly (observed ~0.1s). Behavior is otherwise unchanged.

The codex app-server client (`CodexAppServerClient`) is now safe for one persistent connection shared between a background notification reader and live request threads: a reentrant frame-level lock serializes `poll_notifications`, `_request`, and `_notify` so the reader and a concurrent send/interrupt never steal each other's frames. This is what lets the system interface give each codex agent one long-lived connection driving its message ledger.

Removed the retired stop-retract idle machinery now that the system interface interrupts codex through its live ledger rather than a control file: `mark_codex_agent_idle`, the `codex_marker_state.sh` marker helper (and its provisioning), and the legacy `permissions_waiting` marker name.

The codex release end-to-end test now observes RUNNING from the daemon's live `thread/status` (a turn in flight is `active`), matching how the live lifecycle reads it, since codex no longer writes an `active` marker.

- Reformatted `codex_config_test.py` with ruff (no behavior change).

- New `mark_codex_agent_idle(agent_state_dir, host_dir)` in `codex_config.py`: marks a codex agent idle out-of-band by running the provisioned `codex_marker_state.sh` helpers (lock, clear the `codex_root_active` root-turn flag, recompute the `active` marker -- in-flight subagents keep it -- and clear a stranded `permissions_waiting`), then appends one activity event so `mngr observe` promptly re-probes. The programmatic sibling of the four lifecycle hooks' own marker maintenance and of mngr_claude's `mark_claude_agent_idle`. Caller: the system-interface stop button after a native retract interrupt, where codex fires no Stop hook and would otherwise report RUNNING forever.

- The codex common-transcript converter now surfaces tool activity from the patched codex 0.146.0 build: the unified exec tool's `custom_tool_call` / `custom_tool_call_output` rollout items are converted into paired assistant tool_call / tool_result records (previously every tool call and result was silently dropped, so codex transcripts showed zero tool activity). The legacy `function_call` / `function_call_output` shapes remain supported. Verified against a real 0.146.0 rollout committed as a test fixture (`resources/test_fixtures/codex_0146_rollout_exec_turn.jsonl`, excluded from the wheel).

- Instruction injections that ride in as user-role rollout messages -- the AGENTS.md context blob (`# AGENTS.md instructions for <dir>` with an `<INSTRUCTIONS>` envelope) and codex's `<user_instructions>` / `<environment_context>` items -- are now filtered out of the common transcript, so only genuine user turns emit as `user_message` records.

- The codex raw-transcript streamer (`stream_transcript.sh`) no longer duplicates rollout lines when its 1s poll daemon interleaves with the turn-end `--single-pass` flush: every emit pass now re-reads the persisted per-rollout offset and advances it under the same per-agent lock the common-transcript converter uses (`mngr_common_transcript_lib.sh`), instead of trusting a per-process in-memory offset cache that went stale whenever the other instance advanced the offset first. Whichever pass runs second now sees the other's offset and emits nothing, so every rollout line reaches the raw transcript exactly once, in order. Covered by a new interleaving regression test driven by a real codex 0.146.0 rollout fixture.

- New U5 enforcement test (`resources/common_transcript_diff_test.py`): a native-vs-common transcript diff that independently enumerates every user-visible turn in the real codex 0.146.0 rollout fixture -- with every legitimately-excluded shape (session/event bookkeeping line types, reasoning items, developer-role and user-role instruction injections) named in explicit allowlists -- and asserts the converted common transcript contains exactly those turns, exactly once, in order, with each tool call structurally paired to its result. Unlisted rollout shapes fail the enumeration, so a future binary's new item kind (the exact blind spot that let the 0.146 tool-drop ship green on schema validity) fails loudly. Also asserts reconverting the same rollout appends nothing.

Removed the codex private-prompt file mechanism: codex no longer copies a repo-committed `.codex/AGENTS.md` into `CODEX_HOME/AGENTS.md` at provision time. Codex-specific instructions now reach the agent through `developer_instructions` (the harness-neutral `append_system_prompt` channel, set on the `[agent_types.codex]` config block) -- the same channel every other harness already uses -- so there is one uniform way to deliver per-harness instructions and no bespoke per-agent file copy.

Dropped the now-unused `get_codex_global_instructions_path` and `get_repo_codex_instructions_path` helpers and the `_GLOBAL_INSTRUCTIONS_FILENAME` constant from `codex_config`.

Codex now launches on the stock `codex app-server`: each agent runs the daemon in a hidden sidecar tmux window and the visible TUI attaches to it via `codex --remote`, replacing the single-process `codex` launch. `tmux kill-session` reaps the daemon with the session, so there are no orphaned daemons or leaked sockets, and a stale socket is cleaned on every start.

Added `CodexAppServerClient`, a per-agent WebSocket JSON-RPC client for the daemon (behind an injected transport for deterministic testing): `initialize` handshake, `thread/start`/`resume`/`read`, `submit` (idle -> `turn/start`, busy -> `turn/steer`, with a single ABA re-decide), `interrupt`, `model/list`, and `thread/settings/update`.

Readiness now resolves by connecting to the daemon and completing the `initialize` handshake instead of polling the TUI prompt glyph.

Sending a message to a codex agent now drives the daemon over JSON-RPC instead of typing into the TUI: over a short-lived per-call connection mngr discovers the agent's thread, reads its live status, and delivers the message as a new turn when idle or parks it into the running turn when busy. This is invisible to callers -- the `mngr message` command and its exit codes are unchanged (a message that is not accepted still fails; a message that lands but leaves codex blocked on an approval is still reported as delivered-but-blocked). The old keystroke-injection send path, its submission-evidence probes, and the readiness prompt-glyph are retired for codex.

A codex agent's running-vs-waiting state now comes solely from the daemon's live `thread/status`: a turn in flight reads RUNNING (held until the turn fully completes, not merely until the model stops producing tokens), a turn parked on an approval or input it cannot self-clear reads WAITING, and an idle thread reads WAITING. Process presence (via tmux/ps) still answers alive-vs-dead (STOPPED/DONE), and because the visible `codex --remote` window's life is tied to the daemon, a dead daemon reads STOPPED. The old on-disk lifecycle markers -- the `active` and `permissions_waiting` files, the four marker hooks (`set_active_marker.sh`, `clear_active_marker.sh`, `subagent_started.sh`, `subagent_stopped.sh`) and their shared lock/recompute helper -- are deleted, along with the every-launch marker-reset prelude; there is no marker fallback. A single new `record_session_pointers.sh` UserPromptSubmit hook records the rollout session id and transcript path (the pointers the resume/adopt and raw-transcript machinery need), preserving those flows unchanged; it does no lifecycle work.

`CodexAppServerClient` now captures the `ThreadInfo` seed from the most recent `thread/start` / `thread/resume` on `last_thread_info`, so a caller that binds through the opener can seed the model bar to the settings the daemon resumed with (model / effort / service tier) without a second RPC.

## 2026-08-13

`mngr list` now reports a codex agent blocked on a tool-approval dialog as `WAITING` rather than `RUNNING`.

The plugin already treated such an agent as waiting, but only for the reader `mngr wait`/`message`/`find` use; the agent listing reads the lifecycle probe directly and never saw it, so `mngr list` showed `RUNNING` for an agent that was making no progress.

The plugin now answers `is_blocked_on_dialog()` and the probe folds that into the state it builds, so both readers agree.

## 2026-08-04

Tightened the recorded `trailing comments` ratchet count from 14 to 12. The two that went away were never trailing comments: they are `openai/codex #15269` issue references inside prose, which the rule now exempts.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Reduced the idle CPU cost of the per-agent transcript watchers, which polled continuously whether or not the agent was producing output.

`common_transcript.sh` now skips a conversion pass entirely when the raw rollout stream has not changed since the last completed pass. Each pass spawns a Python interpreter that re-reads and re-parses both the whole input file and the whole output file, so on an idle agent that work was repeated every 5 seconds for no result. The change-detection state is held in memory only, so a restart still performs one full reconciling pass exactly as before, and a pass skipped because a concurrent `--single-pass` flush holds the convert lock is retried on the next cycle rather than being lost.

`stream_transcript.sh` no longer forks subprocesses to discover the active rollout on every poll. The hook-recorded rollout path is now read with the `read` builtin rather than `head`, and the offset key is derived with parameter expansion rather than `basename` piped through `tr`. Both are exact equivalents of what they replace; at a one-second poll interval they were most of an idle cycle's work.

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-14

# Codex: marker-based message delivery confirmation

- Changed: `mngr message` to a codex agent now confirms submission by polling the `active` marker (set by the `set_active_marker.sh` UserPromptSubmit hook) advancing past its pre-Enter state, instead of waiting on the `mngr-submit-<session>` tmux `wait-for` signal. tmux latches a signal fired with no waiter, which could instantly false-confirm a later send (and kill its pending Enter keystroke); the marker gives the same timing without that failure mode, and it exists on agents created by older mngr versions, so no reprovisioning is needed.

- Changed: newly created agents still fire the (now-unlistened) `mngr-submit` signal so older mngr senders keep confirming; the signal is marked for removal in a future release.

- Added: a message-delivery journey release test (idle -> busy -> rapid sequential -> long buffer-pasted message, each delivered exactly once).

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-26

Added scope docstrings to this package's release tests so the TMR (test
map-reduce) harness can anchor each test's intended scope on its docstring
rather than on a tutorial block. Docstring-only; no test logic changed.

## 2026-06-19

No production behavior change. The Codex agent-config merge test now exercises the unified overlay merge pipeline (`merge_models_via_overlay`) instead of the removed `AgentTypeConfig.merge_with` helper. The behavior it locks in is unchanged: an override's `cli_args` replaces the base while other fields keep their defaults.

Trimmed the README to user-relevant content (removed internal implementation details and roadmap notes) and tightened it for concision. The app-server "future direction" roadmap note that was dropped from the README is preserved as an engineering comment on the `CodexAgent` class.

## 2026-06-18

Added a `version` field to the codex agent type that pins the installed codex CLI: installation runs `npm i -g @openai/codex@<version>` and provisioning verifies the installed codex matches, erroring on a mismatch. A pin also suppresses the provision-time update check (`update_policy` is ignored), since updating would defeat the pin. codex's own blocking startup update prompt remains disabled as before.

Codex agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting. (The behavior comes from the shared base agent; the codex lifecycle test was updated to name its primary window accordingly.)

## 2026-06-17

The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (sandbox mode + approval policy) and `HasVersionManagementMixin` (the codex update policy).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `codex` CLI is installed and installs it (`npm i -g @openai/codex`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. The install-if-missing check runs before the existing best-effort update notifier. A new `check_installation` config field (default `True`) disables the check when set to `False`.

Test-only: removed a fragile install-path provision test that crashed on CI, and added focused unit tests for the codex update flow and the CODEX_HOME-resolution error path (covering pre-existing codex plugin code) so the plugin clears the per-package coverage gate.

The auto-allow permission apply-path (`approval_policy="never"`) now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`CodexAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.

`CodexAgent` now implements the functional `reconcile_installed_version` contract (replacing the descriptive `get_version_policy`): it runs codex's existing network-free update check + `update_policy` action (`_maybe_check_for_codex_update`) at the same point in provisioning, so the update behavior is unchanged.

Codex agents can now adopt an existing codex session at create time, so a fresh agent resumes that conversation instead of starting empty.

The session to adopt is resolved from a session id (or an absolute rollout `.jsonl` path) across three stores: the user's native `~/.codex/sessions`, every live local mngr codex agent, and every preserved (destroyed) codex agent. An id matching in more than one store is rejected as ambiguous, with a clear message telling you to pass the full rollout path.

On adoption, the resolved rollout store is copied into the new agent's `CODEX_HOME/sessions`, the recorded working directory inside the rollout (the `session_meta` and `turn_context` records) is rewritten to the new agent's work dir -- so `codex resume` does not pop the "Choose working directory to resume this session" modal -- and the adopted session id is written as the agent's resume pointer.

This is driven by the central `mngr create --adopt <id>` flag (repeatable). `--adopt-session` is still accepted as an alias. The codex plugin now reads the values from the first-class `CreateAgentOptions.adopt_session` field rather than from `plugin_data["adopt_session"]`. A bad or ambiguous id is still caught up front (before any host or worktree is created) as a clean error rather than a traceback.

Multiple `--adopt` values are now each copied into the new agent (their date-nested rollouts coexist, so all are available in codex's session switcher), and the last one named is the conversation actually resumed.

Cloning a codex agent with `mngr create <new> codex --from <agent>` now resumes the source agent's conversation too: the clone transfers the source's native session store, resumes its most-recent rollout, and rebinds the recorded working directory to the clone's work dir (so no resume modal appears). `--adopt` and `--from` may now be combined -- every named session plus the clone is made available, and the clone's conversation is the one resumed. When a `--from` clone has no resumable codex session (no store, or a store with no rollout), the clone now warns and continues -- falling back to a fresh start, or to the last `--adopt` session if one was given -- since `--from` is fundamentally a workspace clone and carrying the session forward is a bonus.

A failure to resolve the user's `CODEX_HOME` during provisioning now surfaces as a clean, user-facing error instead of an abrupt process exit.

The codex common-transcript converter now records a tool invocation as a nested assistant `tool_calls` entry (sharing the same id as the paired `tool_result`). codex models a tool call as a standalone rollout item separate from the assistant's text, so previously the `function_call` item only labeled the later `tool_result` and the assistant turn carried no call -- diverging from the other agent ports and the canonical envelope, where the assistant message carries the call. codex's release test now forces a bash tool call (run unattended via `approval_policy=never`) so this is exercised end to end.

The codex common-transcript converter now emits `finish_reason` instead of `stop_reason` on assistant records (aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array. A codex assistant turn is either text-only or a single tool_call (codex models each call as its own rollout item), so `parts[]` holds that one segment and `parts_ordered` is true.

## 2026-06-16

The codex background-tasks supervisor now also launches an optional usage writer (`codex_usage.sh`) when it's present in the agent's `commands/` dir -- installed by the new `imbue-mngr-codex-usage` package -- and restarts it if it dies, alongside the existing raw/common transcript watchers. No change for agents without the usage plugin installed.

The common-transcript converter's rollout-to-common conversion logic now lives in a standalone `common_transcript_convert.py` (provisioned alongside `common_transcript.sh` and invoked by it) rather than an inline `python3` heredoc, so it is type-checked, linted, and unit-tested directly. Malformed rollout lines and unreadable existing-output lines are dropped silently.

codex now flushes the common transcript at turn end. When the root turn finishes and no subagents are in flight (the agent goes WAITING), the Stop / SubagentStop hooks run one synchronous `--single-pass` conversion, so a consumer harvesting the final message on the WAITING signal no longer races the 5s converter daemon -- matching claude and antigravity. The converter takes the shared convert lock around its read-modify-write so this flush and the background daemon cannot produce duplicate events.

The common-transcript watcher no longer echoes converter errors to the agent's pane: a genuine conversion error is recorded in the structured log only, instead of also being written to the watcher's stderr.

Codex agents now preserve their transcripts on destroy (closing the carried-forward session-preservation gap), matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before a codex agent's state directory is deleted on destroy, its raw and common transcripts and the root session-id history are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- The native resumable rollout session store under `CODEX_HOME/sessions` is now preserved on destroy too, so a preserved agent can be resumed/adopted from codex's own session files. Only the `sessions/` directory is targeted, so the auth-token symlink and config that sit as siblings in `CODEX_HOME` are still excluded.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The codex release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `codex` binary.

## 2026-06-15

Codex agents now report *why* they are waiting, via a `waiting_reason` field in `mngr list` (matching `mngr_claude`):

- `PERMISSIONS` -- the agent is blocked on a tool-approval dialog. A `PermissionRequest` hook touches a `permissions_waiting` marker, and the agent's lifecycle state now reports WAITING (not RUNNING) while the dialog is open. `PostToolUse` clears the marker once the approved tool runs, and both the root `Stop` and the start of the next turn (`UserPromptSubmit`) clear any stranded marker.

- `END_OF_TURN` -- the agent is idle with its turn complete.

The `PERMISSIONS` reason is now gated on the agent's `active` (in-turn) marker, so a stranded `permissions_waiting` marker that outlived its turn reports `END_OF_TURN` rather than wrongly showing `PERMISSIONS` -- the verdict no longer depends on a cleanup hook having deleted the file.

This applies only in supervised mode; with `auto_allow_permissions = true` codex never prompts, so a permission reason never appears.

Known limitation: cancelling a dialog (Esc / "No") interrupts the turn and codex 0.139.0 fires no terminal hook for it (no PostToolUse, Stop, or Notification), so the markers persist until the next turn's Stop. The agent's state stays `WAITING` (correct), but `waiting_reason` may read `PERMISSIONS` instead of `END_OF_TURN` during that window; it self-heals at the next Stop.

Verified live against codex 0.139.0: approve fires `PermissionRequest` -> `PostToolUse` -> `Stop` (marker cleared); cancel fires `PermissionRequest` and then no terminal hook.

## 2026-06-13

Stabilized the codex marker-lock concurrency smoke test (`test_concurrent_root_stop_and_last_subagent_stop_clears_marker`), which could time out under heavy CI load. The test forks roughly 32 short-lived bash subprocesses, so the suite-wide 10s timeout was too tight when a runner was busy; it now gets a generous per-test timeout (a real deadlock would still hang far longer) and is marked flaky so offload retries it. No production behavior changed.

## 2026-06-12

Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation gives each agent its own config, sessions, and transcripts without relocating the user's real `$HOME`.
- Shared auth via a write-through `auth.json` symlink to a shared `~/.codex/auth.json` (with `cli_auth_credentials_store = "file"` pinned), so logging in once authenticates every agent and token refreshes propagate.
- RUNNING/WAITING lifecycle with subagent-aware gating across four hooks (`UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`). Because codex subagents run asynchronously (the root's `Stop` fires while subagents are still working, with no ordering guarantee on the later `SubagentStop` hooks and no `fullyIdle` signal), the `active` marker is recomputed under a lock from a root-turn flag plus one file per in-flight subagent, so it stays RUNNING until the root turn AND every subagent are done. The `Stop` clear is still guarded against a nested/recursive codex via the recorded root `session_id`.
- Conversation resume across stop/start: the root `session_id` is captured into a tracking file and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start). The rollout JSONL is flushed per line, so it survives `mngr stop`'s hard kill.
- Common transcripts readable by `mngr transcript`, plus seeded trust and onboarding for a silent first launch.
- `send_message` waits for submission to register: the `UserPromptSubmit` hook signals a `mngr-submit-<session>` tmux wait-for channel after it sets the `active` marker, and the sender blocks on that channel, so `mngr message` returns only once the agent reads RUNNING (closes a race where a follow-up lifecycle check could see the pre-turn idle state).
- Update handling: codex's blocking startup "Update available!" prompt is disabled (it would intercept the first message), and mngr surfaces updates itself at provision instead. It compares `codex --version` to the latest version codex recorded in `~/.codex/version.json` (no network call); this check always runs and is best-effort (failures never block provisioning). When codex is outdated, the action is governed by a single `update_policy` setting (default `ASK`): `AUTO` runs `codex update` with no prompt, `ASK` prompts on an attended local run (interactive tty + local host, not `--yes`) and otherwise logs a non-blocking notice, and `NEVER` only logs the notice. Because `ASK` is gated on the host being local as well as interactive (mirroring the claude plugin's `is_unattended = not host.is_local`), an unattended remote/deploy agent provisioned from a local terminal defaults to neither prompting nor upgrading the remote's global install.

Not yet implemented (carried-forward gaps): session-preservation-on-destroy, deploy/scheduling contributions, field generators (`waiting_reason`), the streaming snapshot, and install/version management. A future app-server-backed agent variant (drive `codex app-server` over JSON-RPC for programmatic messaging + a `codex --remote` TUI viewer + clean `initialize`-based readiness) is the recommended follow-up; its design and the OpenAI-ToS caveat (identify honestly, no `codex-tui` spoofing) are documented in the plugin README.

Added a conformance test asserting that codex's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`). The release test now runs on the
shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`). The
full lifecycle (including stop/start resume) passes end-to-end against the real codex
binary. Simplified the release test's plumbing to reuse the shared `init_git_repo` helper
and the autouse fixture's tmux-server isolation instead of hand-rolling its own git repo
setup and private tmux server. Now that codex's `send_message` blocks until the agent
reads RUNNING (the submit/lifecycle race fix), the release test also observes the RUNNING
marker like the pi and opencode tests do.

## 2026-06-09

Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation: each agent runs `codex` under its own `CODEX_HOME` so its config, sessions, and transcripts stay isolated, without relocating the user's real `$HOME`.
- Shared auth: each agent's `auth.json` is a write-through symlink to a shared `~/.codex/auth.json`, so the first agent's login authenticates every other agent and token refreshes propagate ("log in once, anywhere"). `cli_auth_credentials_store = "file"` is pinned so the shared file backend is used.
- RUNNING/WAITING lifecycle with subagent-aware gating: a `UserPromptSubmit`/`Stop` hook pair maintains an `active` marker driving `BaseAgent`'s RUNNING/WAITING detection. Subagents fire a distinct `SubagentStop` and run in separate rollout files, so they never touch the marker by construction; the root session id is recorded so `Stop` clears the marker only at root-agent scope.
- Conversation resume: the root `session_id` is captured from a hook into a tracking file, and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start), so the agent keeps its context across a stop/start. The rollout JSONL is flushed per line, so it survives the hard process kill `mngr stop` performs.
- Transcripts: codex agents emit a common transcript readable by `mngr transcript`, mapping codex's rollout `message`/`function_call`/`function_call_output` lines into the agent-agnostic format.
- Trust and onboarding: the agent's canonical work-dir path is seeded as `trusted` and the onboarding NUX is seeded for a silent first launch, so codex starts without interactive trust/login prompts.
