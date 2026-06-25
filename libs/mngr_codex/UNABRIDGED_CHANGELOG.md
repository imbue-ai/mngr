# Unabridged Changelog - mngr_codex

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
