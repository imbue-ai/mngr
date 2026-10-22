# Unabridged Changelog - mngr_pi_coding

Full, unedited changelog entries for the `mngr_pi_coding` project, consolidated nightly from individual files in `libs/mngr_pi_coding/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-22

Test-only: the test stand-ins for the host file reader accept the new optional `max_byte_count` argument of `read_file_tail_from_offset`.

## 2026-10-20

`mngr list` no longer reports a `plugin.pi-coding.waiting_reason` field on agents of other types. The pi-coding field generator ran for every listed agent, so a `command` or `codex` agent showed `END_OF_TURN` under `plugin.pi-coding` (the marker it reads is one such an agent never writes), at the cost of one remote `path_exists` call per agent per `mngr list`. The generator now returns nothing for an agent that is not a PiCodingAgent, as the codex generator already did. Reported by 19 mappers in the 2026-10-13 TMR run.

## 2026-10-14

Autocompact does not compact a Pi agent a second time right after a compaction. A compaction record's `tokensBefore` (the size before the compaction) is not reported as the current context size. A compaction record that is newer, in file order, than every usage makes `get_context_tokens()` return `None` until the next reply reports a real size: a `compaction` entry in the raw transcript or session file, a compaction `cost_snapshot` in the usage stream, or a compaction step in the common transcript. The idle timestamp is unaffected, and a suppressed size is logged at debug level with the agent and file path. The new `is_compaction_record` identifies these records.

- `PiCodingAgent.request_compaction` supports the bounded message-lock wait and idle re-check that mngr's autocompact plugin uses. With `message_lock_timeout_seconds` it raises `MessageLockTimeoutError` instead of waiting on another send, and with `expected_idle_since` it skips with `AgentNoLongerIdleError` when the agent is no longer idle since that moment. A compaction that could not take the lock records nothing, and the idle start a compaction records is read once the lock is held.

- The inbox append that delivers a message or a compaction request has a timeout, so a stuck host command fails the send instead of holding the message lock forever. An append that fails with no output says it may have hit the timeout.

The autocompact eligibility check reads a Pi agent's idle timestamp and context token count from one backward scan of each transcript, usage stream or session file's tail, cached between checks, rather than parsing whole files. An unchanged file costs no reads of its complete lines, and a grown one is read only from where the last check stopped. The values found match a whole-file parse, except that a final line without a trailing newline is read once its newline arrives, and a record containing U+0085, U+2028 or U+2029, which `str.splitlines()` splits apart, is read whole. Removed the whole-text parsers `extract_latest_assistant_timestamp_from_jsonl` and `extract_context_tokens_from_jsonl`; added the per-record `extract_assistant_timestamp_from_record` and `extract_context_tokens_from_record` and `PiCompactionTranscriptScanner`.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-08

Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio". No behavior change: every identifier (`MINDS_*` env vars,
`minds-*` service and scope names, paths) keeps its spelling, per
`apps/minds/style_guide.md`.

Review follow-up to the Imbue Studio prose sweep.

The retract test's docstring still said "Minds hands them back to the
composer", a verbatim twin of the lifecycle comment the sweep had changed.
"no Imbue Studio-view lag" is also reworded to "no lag in Imbue Studio's view",
which parses correctly now that the product name is two words.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-26

The pi lifecycle extension no longer carries any shell-command policy. It used to block a command that pipes into `head`/`tail` or rewrites git history, and to prefix every bash command with the OOM self-tag and the agent's git identity (recording the unprefixed command as `mngrOriginalCommand` for other extensions). Those are workspace rules, not mngr's: claude and codex have always received them from the workspace's own hooks, and pi now gets them the same way, from the workspace's `.pi/extensions/`. A pi agent launched by mngr in a repo that ships no such extension runs its commands unmodified, as a claude or codex agent there already does.

## 2026-09-23

Add support for context compaction in `pi-coding` agents (`HasCompactionMixin`):
- Implement `request_compaction`, `get_cache_ttl_minutes`, `get_context_tokens`, and `get_idle_since` on `PiCodingAgent`.
- Provide compaction helper utilities for tracking compaction state, idle state detection, cache TTL resolution (Anthropic 60m, OpenAI 30m), and token extraction from transcripts.
- Handle compaction requests via the inbox sentinel (`mngr_compact`) in `mngr_pi_lifecycle.ts` by invoking `ctx.compact()`.
- Listen for `session_compact` events in `mngr_pi_lifecycle.ts` and emit raw compaction records, ATIF v1.7 common transcript compaction steps, and usage cost snapshots.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-12

The interactive pi-coding agent now captures its own stderr to `stderr.log` in the agent's
state directory. Agents run under tmux rather than supervisord, so a startup error or
crash reached none of the workspace's service logs, and unlike codex, antigravity and
opencode this harness wrote no diagnostic file of its own -- a bug report had no way to
see it. The TUI still renders on stdout, so the pane is unchanged; the file is truncated
per launch, which bounds it without rotation. The bug-report collector picks it up with
the rest of the agent's logs.

Note the tradeoff: stderr now goes to the file instead of the pane, so a crash message
that used to stay in the pane's scrollback is no longer visible there. It is still
collected -- from the file, and durably, which the pane was not.

The interactive pi-coding agent's stderr now reaches both the pane and the agent's `stderr.log`, instead of only the file. The previous change captured stderr for bug reports but took it off the pane, so a crash message was no longer visible where someone looking at a dead agent expects it. The launch command now redirects stderr through `tee -i` via shell process substitution, which keeps stdout and the TUI untouched, keeps pi's own exit status, truncates the file per launch as before, and survives a Ctrl-C that pi handles itself.

This makes the agent pane's shell a bash/zsh/ksh requirement (process substitution is not POSIX), which every shipped image and both desktop platforms already satisfy.

## 2026-09-01

Trimmed historical framing from a test comment (comments state the current constraint, not the migration history).

The ATIF stream header's `event_id` is now unique per agent stream: `header-<sha256(agent_id:emitter)[:32]>` instead of the fixed `"header"`. A fixed id repeats identically for every agent on every host, so analytics' fleet-wide event-id dedupe collapsed all header rows to one per account, destroying the per-agent (emitter, `schema_version`) mix.

The agent id is the agent state directory's basename; pi writes the header once, when the file is created.

The pi lifecycle extension now writes its common transcript as the ATIF-shaped stream (`header` / `step` / `observation`) per `specs/atif-transcript-alignment/spec.md`: full-fidelity tool arguments and outputs, thinking captured as `reasoning_content`, ATIF metric names for token usage, compaction summaries as `context_management` system steps, and a per-agent-unique header `event_id`. The per-layer `mngr-atif-*.md` entries in this PR carry the details.

The pi lifecycle extension now writes its common transcript as the ATIF-shaped
stream (`specs/atif-transcript-alignment/spec.md`) instead of the old
`user_message`/`assistant_message`/`tool_result` envelope.

The stream opens with a `header` line pinning `ATIF-v1.7`, written once when the
file is created (a resumed restart appends to the existing stream). Each pi
assistant message becomes one `step` record with ATIF field names, user messages
become `user` steps, and `toolResult` messages become `observation` records keyed
to their call by `source_call_id`.

Full fidelity: tool arguments and tool output are no longer truncated at 200 /
2000 characters -- `mngr transcript` truncates for display instead. Thinking
blocks are captured as `reasoning_content` (previously dropped entirely), and
images render as the `[image omitted]` text placeholder.

Token usage now uses the ATIF metric names: `prompt_tokens` counts all input
including cache reads and cache writes, `cached_tokens` counts the cache reads,
the cache-write count rides under `metrics.extra.cache_creation_input_tokens`, and
pi's client-side per-message cost fills ATIF's `cost_usd`.

Compaction summaries, previously dropped, become `system` steps carrying ATIF
v1.7's `context_management` mark with the summary as an inline observation.

An empty or whitespace-only native tool argument payload is now emitted as an
empty `arguments` object rather than `{"_raw": ""}`, matching the codex and
antigravity converters -- an absent payload means "no arguments".

## 2026-08-27

# Globally unique pi common-transcript and usage event ids

- Common-transcript and usage event ids are now derived by hashing the message's own timestamp and the first 1024 characters of its content, instead of a per-agent line counter. Counter ids (`pi-0`, `evt-pi-usage-0`) repeated identically for every pi agent on every host, which collides under fleet-wide dedup by event id (e.g. in analytics).

## 2026-08-26

Pi's command-refusal messages are now copied verbatim from the shared guard scripts the other harnesses run, so an agent gets the same explanation for the same mistake on every harness. The wording had drifted since pi re-expresses these rules in TypeScript rather than running the scripts.

Removed a lifecycle handler registered on `session_switch`, an event pi does not declare and never emits, so it had never run. Its work (recording the live session file) was already covered by the `session_start` handler, which pi fires for `/new`, `/resume` and fork alike. Extension registration does not validate event names, so a wrong one is silently inert rather than an error.

Renamed `auto_dismiss_dialogs` to `auto_dismiss_dialogs_at_startup`. Same behaviour; the name now says when it applies.

## 2026-08-25

Release-test helpers' `workspace`/`prepare_adoption_workspace` identifiers renamed to `project_dir`/`prepare_adoption_project_dir` (mngr-level terminology sweep; no behavior change).

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-24

The lifecycle extension no longer carries checks or task-tracking rules of its own choosing.

It used to spawn a fixed checker script before each bash tool call, and to run a task-tracker discipline -- a reminder when a tool ran with no step in progress, a carryover of still-open steps into the next turn's system prompt, and a note on settle -- reading state from a tracker binary in the agent's work dir. All of that belonged to the repo the agent runs in, not to the agent runner: it assumed that repo's tracker, its command names, and its prose.

What remains is what holds for any pi agent: the pipe-into-`tail`/`head` and git history-rewrite blocks, and the OOM/git-identity command rewrite.

pi auto-discovers extensions from `.pi/extensions/` in the project and composes handlers across extensions -- `tool_call` blocks when any returns `{block, reason}`, `tool_result` handlers chain like middleware, `before_agent_start` chains the system prompt -- so a repo ships its own extension there and it runs alongside this one.

Because they share one event, the command rewrite now also records the command the agent actually wrote, as `mngrOriginalCommand` on the tool-call event. A guard in another extension that reads `input.command` after the rewrite would otherwise see the OOM/git-identity prefix as a command chained ahead of the agent's, and refuse it; pi gives no control over handler order, so the pre-rewrite command travels with the event instead. This is the pi equivalent of running the rewriter last, which is how the same guards stay correct on claude and codex.

## 2026-08-17

The pi lifecycle extension now records the live model and thinking level (pi's effort
axis) to `$MNGR_AGENT_STATE_DIR/pi_model_state.json` (`{provider, model, thinking_level}`).
It writes on `session_start` -- which fires at TUI startup, before the first prompt, so
the pre-turn-1 selection is available immediately -- and refreshes on `model_select` /
`thinking_level_select` as the user switches. This gives the chat model bar a low-latency,
on-disk source for pi's current model and effort, which pi otherwise exposes only through
its extension API.

Messages sent to a running pi agent are now delivered as `steer` rather than `followUp`.
pi's agent loop re-polls its steering queue after every tool-call round and injects steered
messages before the next model response, so a message sent mid-run reaches the agent greedily
at the next tool boundary instead of waiting for the whole turn to end. Delivery to an idle
agent is unchanged (it starts a turn either way).

The lifecycle extension's model-state mirror moves to the harness-uniform contract: it now
writes `$MNGR_AGENT_STATE_DIR/model_state.json` with the shared `{model: "provider/id",
effort, fast}` schema (previously `pi_model_state.json` with pi-specific keys), atomically via
tmp + rename. The system interface reads the same file name and schema for every harness.

The model-switch control channel becomes a single-slot mailbox (`pi_control.json`): the model
bar's resolver atomically overwrites it with the newest intent (an unconsumed older pick is
replaced -- last wins), and the extension consumes it (rename, apply, delete) at session start
and on its poll. A switch made while the agent is stopped now applies on the next start instead
of being silently swallowed by the old append-log's startup baseline.

The pi lifecycle extension now supports an atomic shoulder-tap. Minds appends a
`{"minds_interrupt": true}` control line to `pi_inbox`; on it the extension, in one
synchronous tick, interrupts the running turn (only if one is running) and captures the
steers pi drains into the composer -- clearing and restoring any user draft around the
drain so the resubmit is sourced purely from pi's own queue -- then, once the turn
settles, resubmits them as one merged turn. Injection is paused between the interrupt and
the resubmit so nothing opens a competing turn. Delivery to an idle agent is a no-op.

The lifecycle extension now generation-scopes the durable inbox at load: any `pi_inbox`
lines present when the extension loads belong to a prior process generation, so they are
archived verbatim to a sibling `pi_inbox_history` (raw history preserved) and `pi_inbox`
is truncated in place before the injection offset is seeded. The inbox therefore only
ever holds current-generation lines, so the Minds queue mirror's replay of `pi_inbox`
from zero can no longer resurrect dead generations' messages as phantom queued entries.
Safe against races: mngr appends to the inbox only after the readiness sentinel, which
`session_start` writes after load. Behavior toward pi is unchanged -- pre-existing lines
were already never re-injected (the offset seed skipped them).

Pi agents now enforce the same shell-command policy guards claude/codex do. The lifecycle
extension gains a `tool_call` handler that, for the bash tool, blocks a command piping into
`tail`/`head` and the git history-rewriting commands (`rebase`, `commit --amend|--fixup`,
`pull --rebase`) by returning `{block, reason}`, and otherwise rewrites the command in place with
the OOM self-tag + the agent's git identity (name from the agent's `data.json`, email
`<agent_id>@<host_id>`). Pi has no shell-hook surface, so the same rules the claude/codex scripts
apply are re-expressed in ~20 lines of TypeScript; the handler fails closed. Loaded only via the
per-agent `pi -e` flag, so a user's normal `pi` is unaffected. See `system/scripts/POLICY_HOOKS.md`.

The lifecycle extension now handles a second inbox sentinel for the stop button:
`{"minds_interrupt_retract": true}` is the retract sibling of the shipped
`{"minds_interrupt": true}` flush. On it the extension interrupts the running turn using the
same shared abort-and-capture core (interrupt only when a turn runs, clear and restore the
user's draft around pi's own steer drain) but DISCARDS the captured steers instead of
resubmitting them -- Minds hands the queued messages back to the user's composer, so
resubmitting here would double-deliver. A distinct key (not a field on the flush sentinel)
means an older extension treats it as inert rather than mistaking a retract for a flush. Both
sentinels are now tick-deferred: a sentinel is never consumed in a drain tick that already
injected a string line (the async send must park the steer first), so the steer is always
flushable/retractable before the abort. Delivery to an idle agent is a no-op, and a
retract discards nothing then keeps draining so later messages still inject.

The inbox sentinel (flush and retract) now waits for every injected steer to actually PARK
before it aborts, not just for one drain tick to elapse. The extension tracks the count of
in-flight `sendUserMessage` injections (settled in a `finally`) and defers a sentinel while any
remain outstanding, in addition to the existing same-tick deferral. Previously the sentinel was
deferred exactly one poll on the assumption the async send parked within it; a slower-parking
steer could be aborted before it landed and then escape the flush/retract to commit as a stray
turn. Gating on the actual settle makes "the steer has parked before the abort" hold regardless
of send latency (the stop button's in-flight message is provably captured-and-retracted, never
left to run). Idle delivery is still a no-op.

The pi lifecycle extension now enforces the tk workflow-discipline guards, matching claude/codex: a
non-standalone `tk start`/`close` is blocked in the `tool_call` handler (reusing the shared
`claude_tk_standalone_check.py`); a require-steps reminder is appended to the `tool_result` content when
a substantive tool ran with no in-progress step; an open-steps carryover reminder is appended to the
turn's system prompt in `before_agent_start`; and an open-steps stop nudge is written to stderr on
`agent_settled`. Step state is read from the vendored `ticket` binary (invoked via `bash`, so it works
on a noexec mount). See `system/scripts/POLICY_HOOKS.md`.

The lifecycle extension now writes `model_state.json` instead of `minds_model_state.json`, matching the rename on the claude side: the plugin has no reason to be named for one of its clients.

- Reformatted `resources/mngr_pi_lifecycle_test.py` with ruff (no behavior change).

- Corrected the stale per-agent `npm install` cost comment in `plugin.py` (observed 45-55s, not ~1s; it delays the readiness sentinel by that long) and two stale `ON_CHANGE` model-bar comments in `resources/mngr_pi_lifecycle.ts` (all harnesses reconcile via `EAGER_THEN_RECONCILE`).

- Provisioning copies the home npm extension install into the per-agent dir, so pi's first boot no longer spends 45-55s in npm before the readiness sentinel (first boot now matches a warm boot, ~8-13s).

- The lifecycle extension writes the session file and model state BEFORE the readiness sentinel, so everything the chat surface needs at first paint is on disk when readiness fires; a failed model-state write now withholds readiness instead of signaling a half-ready agent.

- The pi lifecycle extension's flush/retract sentinel settle-gate is now bounded: a sentinel that has been deferred for `SENTINEL_SETTLE_MAX_TICKS` (10 drain ticks, ~2s at the 200ms cadence -- the same order as the dwt stop path's bounded message-lock wait) proceeds even while injected steers are still un-parked, so a `sendUserMessage` promise that never settles can no longer defer the abort forever (an unstoppable turn). Past the bound an un-parked steer may escape the retract/flush as a visible duplicate -- the class the queue-sweep series already accepts -- which is strictly better than a stop that never lands (stop wins, bounded: U2).

- `injectSteer` now assimilates the send's return value through `Promise.resolve(...)` before chaining `.catch`/`.finally`: the thenable guard only proves `.then` exists, so a then-only thenable used to throw synchronously in the chain, leaking a `pendingInjections` entry and deadlocking sentinel processing (U2).

- The tk workflow-discipline guards now export the resolved `TICKETS_DIR` (including the WORK_DIR fallback) into the environment of the spawned `ticket` and tk-standalone-checker children, matching the shell hooks they mirror -- previously a fallback-resolved dir was checked by the guard but never seen by the child.

- New U5 enforcement test in `resources/mngr_pi_lifecycle_test.py`: a native-vs-common transcript diff driven by a real pi session captured live from the actual pi binary (committed as `resources/test_fixtures/pi_session_two_turns.jsonl`, thinking signature scrubbed; excluded from the wheel). The session's message entries are replayed through the real lifecycle extension and the emitted common transcript is diffed against an independent enumeration of the session's user-visible turns, with every legitimately-excluded shape (session bookkeeping line types, unmodeled message roles, thinking blocks) named in explicit allowlists -- so every turn must surface exactly once, in order, with tool calls paired to their results, and an unlisted native shape from a future pi version fails loudly instead of hiding inside a vague filter. The enumeration was cross-validated against the common transcript the live probe agent actually emitted in-process.

The pi-coding agent type now accepts `output_style` and `append_system_prompt`, matching
codex. At provision, the appended-prompt blocks and the output-style file's body (resolved
from `.agents/output-styles/`, verbatim) are written to `APPEND_SYSTEM.md` in the per-agent
pi config dir, which pi appends to its system prompt every turn -- the pi analogue of codex
writing `developer_instructions` into `config.toml`. This unblocks creating pi chat agents
(`mngr create --type pi -t chat`), whose `chat` role sets `output_style`.

pi now applies model/effort switches from the chat model bar. The bar's resolver writes a
switch intent to `<state>/pi_control.json (single-slot mailbox)` ({model_id, thinking_level}); the lifecycle
extension now watches that file and applies the newest intent natively -- resolving the
`provider/model` slug through pi's model registry and calling pi's own `setModel` /
`setThinkingLevel`. Applying fires pi's `model_select` / `thinking_level_select` events,
which already write `pi_model_state.json`, so the bar reconciles with no extra wiring.
Because pi is a single client that both applies and records the change, this is fully
consistent (no separate-client desync). A switch to an unauthenticated provider, or to a
model pi can't resolve, is logged and skipped.

Pi agents now stamp a `pi_process_started` marker (and clear a stale `active` marker) on every launch/resume, mirroring `mngr_claude`'s `claude_process_started` and `mngr_codex`'s `codex_process_started`. This gives the system-interface activity indicator a restart boundary, so a pi agent killed mid-turn no longer shows a stuck "Thinking..." after it restarts.

Added a `share_home_npm_dir` option to the pi-coding agent type (default off).

When on, an agent's npm extension dir is a symlink to the shared `~/.pi/agent/npm`
instead of a per-agent copy. pi loads its extensions as TypeScript through jiti, whose
transpile cache is keyed by absolute path, so a per-agent copy (unique path) is a cache
miss and every newly created agent re-transpiles the whole extension set from scratch
(measured at ~16-24s for two extensions, on top of pi's ~10s baseline). Pointing all
agents at the one shared path lets that cache hit, cutting extension-heavy startup from
~30s to ~10s.

Only safe when every package pinned in `settings.json` is already installed in
`~/.pi/agent/npm` (e.g. baked into the image): the copy default exists precisely because
a shared `node_modules` that pi would write into (installing a missing package) can race
across concurrent agent startups and would mutate the user's home npm. Ignored on remote
hosts, which have no shared home npm.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-08-01

No behavior change. This project's recorded async/await ratchet count dropped because the shared rule no longer counts prose mentioning the `await-delivery` CLI subcommand as an async usage (see the `imbue_common` entry); the remaining count is genuine async code.

## 2026-07-23

Internal refactor only (no user-visible or behavior change): the shared agent `wait_for_ready_signal` parameter `is_creating` was renamed to `is_readiness_awaited` (part of the mngr Claude TUI-dialog hardening work). The pi agent still waits for its `pi_session_started` readiness sentinel exactly as before; only the parameter name changed.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-26

Added scope docstrings to this package's release tests so the TMR (test
map-reduce) harness can anchor each test's intended scope on its docstring
rather than on a tutorial block. Docstring-only; no test logic changed.

## 2026-06-19

No production behavior change. The Pi Coding agent-config merge test now exercises the unified overlay merge pipeline (`merge_models_via_overlay`) instead of the removed `AgentTypeConfig.merge_with` helper. The behavior it locks in is unchanged: an override's `cli_args` replaces the base.

Trimmed the README to user-relevant content and tightened it for concision.

Aligned the `pi-coding` config field descriptions (surfaced by `mngr config`/help) with the README settings table.

The config table now documents `version`, `update_policy`, `auto_allow_permissions`, and `preserve_on_destroy` (`version` / `update_policy` had been dropped from the table).

## 2026-06-18

Added a `version` field to the pi agent type that pins the installed pi CLI: installation runs `npm install -g @earendil-works/pi-coding-agent@<version>` and provisioning verifies the installed pi matches, erroring on a mismatch.

Added an `update_policy` field that governs pi's startup version check. `NEVER` sets `PI_SKIP_VERSION_CHECK=1` so pi does not phone home to compare against the latest release; `AUTO` leaves the check enabled; `ASK` behaves like `AUTO`. When unset, it defaults to `NEVER` (check disabled) -- set `AUTO` to re-enable it.

## 2026-06-17

The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

`PiCodingAgent` also declares the `HasUnattendedModeMixin` capability. pi has no tool-approval gate, so it gains an `auto_allow_permissions` config field pinned to True (setting it False is rejected, since pi cannot enforce a deny) -- making "runs unattended" code-detectable and uniform with the other agents.

`PiCodingAgent` now exposes a `waiting_reason` agent field (the `agent_field_generators` hook). pi has no tool-approval gate, so the reason is single-valued (END_OF_TURN when idle), but wiring it through the shared classifier makes it a real extension point and a code-detectable capability.

`PiCodingAgent` now declares `HasAutoInstallMixin` and routes its install-if-missing through the shared `ensure_cli_installed` helper (it now also prompts in interactive mode rather than only honoring `--yes`).

`PiCodingAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.

`PiCodingAgent` now also declares `InteractiveAgentMixin` -- the marker for agents that accept interactive messages, now that `send_message` is no longer a universal `AgentInterface` method. pi already implemented `send_message` (it appends to its extension inbox), so this only adds the marker. Behavior is unchanged.

Added session adoption for the `pi-coding` agent type: `mngr create pi --adopt <id-or-path>` makes the newly created agent resume an existing pi conversation instead of starting fresh. The flag was previously spelled `--adopt-session`, which is still accepted as an alias.

The session to adopt is resolved (by session id or absolute `.jsonl` path) across the user-native store (`~/.pi/agent/sessions/`), every live mngr agent, and every preserved (destroyed) agent. Each resolved session is copied into the new agent's store and its embedded working directory is rebound to the new agent's work_dir (so pi never stalls at its "working directory does not exist" dialog). `--adopt` may be passed more than once: every named session is made available in the new agent, and the last one is the session that resumes on launch. A bad or ambiguous `--adopt` id is now reported as a clean error before any host is created, rather than as a traceback during provisioning.

Internally, the plugin now reads the adopted session ids from the first-class `CreateAgentOptions.adopt_session` field instead of the previous `plugin_data["adopt_session"]` namespaced key.

A `--from <agent>` clone of a `pi-coding` agent resumes the source agent's pi conversation: the source's native session store is transferred into the clone, its most-recent session (chosen on the source, so it is unaffected by any `--adopt` sessions in the shared store) is rebound to the clone's work_dir, and the resume pointer is written. A `--from` clone whose source has no resumable pi session warns and starts the clone fresh (the clone carries the source's workspace; carrying its conversation forward is a bonus), whereas an explicit `--adopt` that cannot be resolved is still a hard error. `--adopt` and `--from` may now be combined: every `--adopt` session is made available and the clone's session is the one resumed.

When `auto_dismiss_dialogs` is set (also implied by `mngr create --yes`), mngr now launches pi with its native `--approve` flag, so pi auto-trusts the agent's project folder for the run and its "Trust project folder?" dialog never blocks the first message -- without the workspace needing any trust inputs of its own.

The pi-coding common-transcript emitter now emits `finish_reason` (was `stop_reason`, aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array on assistant records that preserves the source interleaving of text and tool-call blocks (`parts_ordered` true, since pi's native content array is ordered).

## 2026-06-16

The pi lifecycle extension now also writes per-message usage events (cost + tokens) for `mngr usage`, gated on a `pi_emit_usage` marker that the new `imbue-mngr-pi-coding-usage` package provisions. pi reports cost client-side, so each assistant `message_end` appends a `cost_snapshot` (reported cost, tokens, provider-qualified model) to `events/pi-coding/usage/events.jsonl`. Inert unless the gate marker is present, so behavior is unchanged for agents without the usage plugin installed.

pi-coding agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before a pi-coding agent's state directory is deleted on destroy, its raw and common transcripts and the recorded session-file pointer are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The pi-coding release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), extending the shared end-to-end coverage to this plugin.

- pi's native resumable session store (`plugin/pi_coding/sessions`) is now also preserved on destroy, so the conversation content itself survives -- previously only the recorded session-file pointer was kept, which dangled once the store was deleted. The credential `auth.json` is a path-separate sibling of the store and is excluded.

## 2026-06-12

Added the `pi` alias for the `pi-coding` agent type. `mngr create my-agent pi` is now equivalent to `mngr create my-agent pi-coding`.

Brought the `pi-coding` agent type up to real lifecycle parity with the mature
agent plugins. The plugin now provisions a single mngr-owned pi extension (loaded
with `pi -e`) that drives everything pi has no shell hooks for:

- `mngr list` now reports RUNNING vs WAITING for pi agents (an `active` marker
  maintained on pi's `agent_start`/`agent_end` events), and stays correct when an
  agent spawns a nested `pi` via its bash tool.
- `mngr transcript <agent>` now works for pi agents, and a raw pi message stream is
  captured under the agent state dir. New config: `emit_common_transcript`,
  `emit_raw_transcript` (both default on).
- `mngr stop` then `mngr start` now resumes the same pi session with full context.
  New config: `resume_session` (default on).
- Agent creation now waits on a real readiness signal (a sentinel the extension
  writes when pi's session loads) rather than only scraping the startup banner.
- On remote hosts (when allowed), auto-installs pi from npm
  (`@earendil-works/pi-coding-agent`); on local hosts it still defers to the user
  unless `--yes` is passed.
- Also sync the `agents/` resource dir from `~/.pi/agent/` into each agent's
  config dir (alongside skills/prompts/extensions/themes), so an installed
  subagent extension finds its agent definitions (pi has no built-in subagents).
  The `npm` dir is deliberately *not* synced: pi auto-installs the `packages`
  listed in the synced `settings.json` into each agent's `$PI_CODING_AGENT_DIR/npm`
  on startup, so npm-package extensions (e.g. `npm:pi-subagents`) are available
  without copying `node_modules`, at the cost of a ~1s per-agent install that
  needs network on first launch.
- Deliver messages by injecting them into the live session via the lifecycle
  extension (`pi.sendUserMessage`) rather than simulating tmux keystrokes: mngr
  appends each message to a per-agent inbox file and the extension's watcher
  injects it. The TUI stays viewable (attach with `mngr connect`), and delivery
  is more reliable than the old paste+Enter path (pi intermittently swallowed the
  first Enter) and behaves identically on local and remote hosts.
- Handle pi 0.79+'s "Trust project folder?" dialog: mngr pre-trusts the agent's
  workspace (seeding pi's `trust.json`) so the agent never stalls at the dialog,
  gated like the claude/antigravity agent types -- silent under `mngr create --yes`
  or the new `auto_dismiss_dialogs` config, an interactive prompt otherwise, and
  it extends the grant automatically when the source repo is already trusted.

Known gaps carried for follow-up (matching the other ports): session preservation
on destroy, scheduled-deploy file/env contributions, a `waiting_reason` listing
column, the live streaming snapshot, and a per-agent permission-gate (pi runs tools
without a confirmation gate by default).

Added a conformance test asserting that pi's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`), so the pi emitter and the shared
contract cannot drift. The release test now runs on the shared agent
release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`), holding pi to the
same lifecycle and transcript contract as every other agent.

## 2026-06-10

Improved the pi-coding plugin's unit tests: `on_before_provisioning` is now exercised against an isolated temp HOME and asserts the missing-credentials warning (plus a new positive case that verifies no warning fires when an auth file is present); the remote auto-install test now asserts that `npm install` actually runs; local config-dir symlink tests now verify link targets, not just that a symlink exists; and the abstract-method check now asserts the class is concrete via `inspect.isabstract`. The test conftest now registers the standard mngr plugin test fixtures via `register_plugin_test_fixtures(globals())` (the purpose-built plugin helper), so HOME isolation comes from the common autouse `setup_test_mngr_env` fixture rather than being set up by hand; a small `log_warnings` capture fixture is defined locally since it is not part of that standard set. The shared `pi_agent` fixture moved to `conftest.py`, and the stub host now records executed commands. No production behavior changed.

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-04

Fixed remote provisioning of pi resource directories (skills/prompts/extensions/themes) to transfer with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file (a full round-trip over the tunnel) and did not scale to large resource sets -- the same failure mode as github issue 1825.

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

Update pi-coding plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
