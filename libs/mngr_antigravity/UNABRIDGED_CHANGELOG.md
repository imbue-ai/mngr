# Unabridged Changelog - mngr_antigravity

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_antigravity/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

No production behavior change. The Antigravity agent-config merge test now exercises the unified overlay merge pipeline (`merge_models_via_overlay`) instead of the removed `AgentTypeConfig.merge_with` helper. The behavior it locks in is unchanged: an override's `cli_args` replaces (rather than concatenates onto) the base.

Trimmed the README to user-relevant content (removed internal implementation details and roadmap notes) and tightened it for concision.

`settings_overrides` now folds onto the base with the same principled merge as mngr_claude: a bare key assigns with a narrowing guard (errors if it would silently drop a non-empty list/dict/set from the base), and a top-level `__mngr_merge` map declares per-key `extend` (merge onto the base) or `assign` (replace without the guard).

```toml
[agent_types.my_antigravity.settings_overrides.permissions]
allow = ["command(git)"]
[agent_types.my_antigravity.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"
```

`__mngr_merge` is ignored by vanilla antigravity, so the generated `settings.json` stays clean. Raw `__extend` / `__assign` suffix keys are rejected in `settings_overrides`, and a `__mngr_merge` key in the synced home settings base is stripped. On a narrowing, the error prints the exact `__mngr_merge` patch to add (the full nested patch: `extend` for a dict that would drop a sibling key, `assign` for a replaced list/value). Previously `settings_overrides` replaced top-level keys wholesale with no narrowing guard.

## 2026-06-18

Added an `update_policy` field to the antigravity agent type that governs agy's background self-updater. `NEVER` sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the agent environment so the installed build stays put; `AUTO` leaves agy's self-updater enabled; `ASK` behaves like `AUTO`. When unset, it defaults to `NEVER` (auto-update disabled) -- set `AUTO` to leave the self-updater on.

Note: agy has no version-pinning capability -- Google's installer always installs the latest build (no version argument or env var) -- so there is no `version` field. The default `update_policy = "NEVER"` freezes whatever build was installed.

## 2026-06-17

The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (per-resource permission policy via the settings `permissions` block).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `agy` CLI is installed and installs it (`curl -fsSL https://antigravity.google/cli/install.sh | bash`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. A new `check_installation` config field (default `True`) disables the check when set to `False`.

The auto-allow permission apply-path (the `--dangerously-skip-permissions` flag) now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`AntigravityAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.

Antigravity agents can now adopt an existing agy conversation at create time, so a new agent resumes that conversation's full context instead of starting fresh. The conversation to adopt is given as a conversation id or an absolute path to the conversations store (a `<id>.db` file or a `conversations/` directory). A conversation id is resolved across the user-native agy store (`~/.gemini/antigravity-cli/conversations/`), every live local mngr antigravity agent, and every preserved (destroyed) antigravity agent; an id that matches in more than one place is rejected as ambiguous. The resolved store is copied into the new agent's home and recorded as its resume pointer.

Adoption is triggered by the shared `--adopt` CLI flag (e.g. `mngr create antigravity --adopt <id>`; `--adopt-session` is accepted as an alias). The flag is repeatable: every value's conversation store is copied into the new agent (each coexists as a separate `<id>.db`, so all stay available in agy's session switcher), and agy resumes the last value given. `--adopt` may now also be combined with `--from`: every named conversation plus the clone's conversation are made available, and the clone's conversation is the one resumed. Because agy resumes purely by conversation id and is directory-agnostic, adoption needs no working-directory rebind. A bad or ambiguous `--adopt` id is now rejected with a clean error before any host or worktree is created, rather than surfacing as a wrapped provisioning traceback.

Internally, the antigravity plugin now reads the adopt value from the first-class `CreateAgentOptions.adopt_session` field (and `OnBeforeCreateArgs.agent_options.adopt_session`) rather than the previous `plugin_data["adopt_session"]` namespaced key.

Cloning an antigravity agent with `--from <agent>` now carries the source agent's conversation forward: the clone transfers the source's agy conversation store and resumes the source's root conversation, so it recalls the original agent's context instead of starting fresh. Because `--from` is fundamentally a workspace clone, carrying the conversation forward is a bonus: if the source agent has no resumable conversation, the clone logs a warning and starts a fresh session rather than failing. (An explicit `--adopt` of an unusable session remains a hard error.)

Fixed the antigravity TUI-readiness detection for agy 1.0.9, which removed the "? for shortcuts" footer hint `mngr` polled to know the input row was drawn before sending a message. Without it, `mngr message`/`create --message` timed out with "Timeout waiting for TUI to be ready" even though agy was up. The readiness signal now matches the input box itself (a horizontal rule, the `>` prompt, and a second rule) via a regex, which -- unlike the splash banner -- is present on both a fresh start and a resume (it stays pinned on screen even as the conversation grows) and only appears once the input row is actually interactive.

The antigravity common-transcript converter now emits `finish_reason` instead of `stop_reason` on assistant records (aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array. Antigravity's native format records the text and tool calls separately with no relative ordering, so `parts[]` is a best-effort order (text, then the calls) and `parts_ordered` is false.

## 2026-06-16

Fixed a stale keyword argument in the antigravity submission path: the call to `send_enter_via_tmux_wait_for_hook` still passed `queue_log_path_template=None`, a parameter that was removed upstream when the queue-log fallback was dropped (the function now waits on the TUI hook signal, optionally alongside an acceptance marker). agy supplies no acceptance marker, so behavior is unchanged -- it still waits on its statusLine busy-signal alone. This reconciles the antigravity plugin with the current `tui_utils` signature so it type-checks.

The common-transcript converter's event-conversion logic lives in a standalone `common_transcript_convert.py` (provisioned alongside `common_transcript.sh` and invoked by it), so it is type-checked, linted, and unit-tested directly. Malformed raw-transcript lines, unreadable existing-output lines, non-string USER_INPUT content, and CODE_ACTION records with non-string content (e.g. JSON null) are dropped silently rather than emitting an empty event or crashing the converter.

The common-transcript watcher no longer echoes converter errors to the agent's pane: a genuine conversion error is recorded in the structured log only, instead of also being written to the watcher's stderr.

agy (antigravity) agents now preserve their transcripts on destroy, matching the claude plugin.

- New `preserve_on_destroy` config option (default `true`): before an agy agent's state directory is deleted on destroy, its raw and common transcripts and the conversation-id history (root conversation plus the full conversation-ids list) are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/`, mirroring the agent's state-directory layout. For remote agents the files are pulled to the local machine so they survive host destruction. Set to `false` to discard transcript data on destroy.

- Works for both online destroys and offline host destruction (where the agent state is read off the host's persisted volume).

- The agy release lifecycle test now asserts the transcripts are actually preserved on destroy (previously destroy was bare cleanup), so the feature is covered end-to-end against the real `agy` binary.

- agy's native resumable conversation store (the per-conversation SQLite files under `plugin/antigravity/home/.gemini/antigravity-cli/conversations/` that `agy --conversation` resumes from) is now also preserved on destroy, so the agent can be resumed or adopted. Only the `conversations/` subdir is preserved -- the agy oauth token, `settings.json`, and the macOS keychain symlink are excluded. Known limitation: on macOS the store is encrypted by the login-keychain "Antigravity Safe Storage" key, so a macOS-created store is readable on the same machine but not portable to a different machine or user (Linux uses a portable file-based store).

## 2026-06-15

Hardened the turn-end signal so consumers that read the common transcript on the WAITING
transition (e.g. an orchestrator harvesting the agent's final message) can no longer
outrun the converter. `statusline.sh` now, on the busy->idle edge, flushes the transcript
pipeline (a synchronous `--single-pass` of the raw streamer and common-transcript
converter, in pipeline order) before clearing the `active` marker -- so by the time the
agent reports WAITING the common transcript already reflects the final assistant message.
The flush is gated to the busy->idle edge so it costs at most one conversion pass per turn.

The flush and the converter's convert lock now come from the shared
`mngr_common_transcript_lib.sh` (see the `mngr` changelog) rather than being duplicated
per agent. The convert lock keeps the on-demand flush from racing the background 5s
daemon into duplicate events; its timeout is tunable via `MNGR_CONVERT_LOCK_TIMEOUT`.

## 2026-06-14

Fixed a macOS keychain barrier that blocked antigravity (`agy`) agents. agy embeds
Chromium, whose `os_crypt` stores its "Antigravity Safe Storage" key (which encrypts agy's
persisted conversation store) in the login keychain that macOS resolves at
`$HOME/Library/Keychains`. The per-agent `$HOME` relocation that isolates agy's config also
hid that directory, so agy found no keychain and macOS raised a modal "A keychain cannot be
found to store Antigravity Safe Storage" dialog -- which blocked agy until dismissed,
hanging any unattended run and popping on every fresh agent interactively.

Provisioning now symlinks the per-agent home's `Library/Keychains` to the user's real one
on macOS (Linux has no such keychain and Chromium falls back to its file-based store, so
nothing changes there). agy is already in the keychain item's ACL from interactive logins,
so it reads the key with no prompt. This mirrors the existing playwright-cache symlink --
another HOME-relative, machine-shared resource -- and the claude-style "straightforward on
Linux, keychain on macOS" split.

Also added the antigravity end-to-end release test (`test_antigravity_agent_e2e.py`) on the
shared agent release-lifecycle harness, which this fix unblocks.

Ported the antigravity transcript streamer to agy's new conversation store. agy 1.0.4
(2026-06-01) switched its interactive store from a per-conversation JSONL transcript (which
the old streamer tailed, and which agy no longer writes) to a protobuf SQLite `.db`, so the
streamer was capturing nothing on current agy. `stream_transcript.sh` is now a thin,
python3-guarded supervisor around a new self-contained decoder (`decode_agy_transcript.py`)
that reads new steps from each conversation `.db` and emits the same record shape the old
JSONL had, so the common-transcript converter is unchanged (it now also accepts agy's clean,
un-enveloped user text). The decoder needs no `protobuf` library or shipped schema -- it is a
small wire-walk keyed to the field map recovered from the binary's embedded descriptors;
`regenerating_protobuf_schema.md` documents that recovered schema and a repeatable process to
re-verify it after each (roughly weekly) agy release. Assistant tool calls (name + args) are decoded too,
so they surface on assistant messages. (Tool *results* are not yet captured as `tool_result`
events: agy records command output in step types the converter does not map, and file-edit
`CODE_ACTION` steps do not occur in practice -- a follow-up if needed.)

Added a release-marked test (`test_antigravity_proto_schema.py`) that mechanizes the
"re-verify the schema after each agy release" procedure from `regenerating_protobuf_schema.md`: it runs the
schema extractor against the installed `agy` binary and asserts every field number and enum
value the transcript decoder hard-codes still matches. It requires `agy` on PATH (a missing
binary is a hard failure, not a skip, since there is nothing to verify against without it).

Fixed ERROR_MESSAGE transcript decoding, which that verification surfaced: agy's
`CortexStepErrorMessage` carries no text directly -- the user-facing message lives in its
nested `error` field (a `CortexErrorDetails`), so the decoder, which read a non-existent
top-level text field, always produced empty content for error steps. It now descends into
`CortexErrorDetails.user_error_message` (falling back to `short_error` / `full_error`).

Lowered the antigravity full-lifecycle release test's wall-clock timeout from 1500s to 600s.
The 1500s was copied from sibling agent tests before this test had ever completed a run; a
healthy run measures ~25s. Also marked the test `flaky`: its post-resume "recall" step
occasionally hangs on agy's TUI message-submission signal (observed on agy 1.0.8).

Simplified the common-transcript converter's user-message handling to match agy's current
store: it now passes through the clean typed text agy records in `CortexStepUserInput.query`,
dropping the speculative `<USER_REQUEST>...</USER_REQUEST>` envelope stripping that existed
only for the retired agy-1.0.0 JSONL format.

Hardened the SQLite decoder against malformed/truncated protobuf so a single bad step can no
longer take down transcript capture. A `created_at` timestamp outside the platform range now
degrades to an empty timestamp instead of raising an uncaught error that aborted the entire
decode pass (which, since the offset never advanced, blacked out every conversation on every
cycle); such an out-of-range value comes from a corrupt or truncated payload, not from normal
agy releases, which are additive and keep the wire format valid. Truncated fixed-width
(32/64-bit) fields and unknown protobuf wire types are now detected as malformed and the step is
retried, matching the existing length-delimited handling, rather than silently yielding corrupt
data and advancing past the step. A corrupt per-conversation offset file now resets to the start
instead of crashing. Validated end-to-end by decoding real agy 1.0.8 conversation stores
(including the `ChatToolCall` name/args path the schema-verification test cannot reach).

## 2026-06-12

Added the `agy` alias for the `antigravity` agent type. `mngr create my-agent agy` is now equivalent to `mngr create my-agent antigravity`.

Made `mngr message` to an antigravity (`agy`) agent robust by switching from a blind best-effort Enter to a confirmed submission, and replaced the fragile lifecycle marker hooks with agy's `statusLine` mechanism.

A single mngr-owned `statusline.sh`, seeded into the per-agent `settings.json` as agy's `statusLine` command (applied last, so it always wins), is now the source of truth for agent lifecycle. agy invokes it on every agent-state change: it maintains the `active` marker that drives RUNNING vs WAITING (busy iff `agent_state` is not idle/initializing/authenticating), records the root conversation for resume, and fires the tmux signal that confirms a message was accepted. Because agy's top-level `agent_state` already aggregates subagent activity, this single check replaces the old `PreInvocation`/`Stop` marker-hook pair (`set_active_marker.sh` / `clear_active_marker_when_idle.sh`), which are removed.

`mngr message` now returns only after the agent has actually started processing the submission (not after a blind Enter), and the agent correctly reports RUNNING for the whole turn including while subagents run. The conversation-id capture hook is retained (it is the only place subagent ids surface, for transcript scoping). Readiness is still gated by the TUI banner poll, which is the correct precondition for sending input.

mngr's `statusLine` is lifecycle-only and prints nothing of its own (agy already shows working/idle), so the status row looks exactly as it would without mngr. agy allows only one `statusLine` command, so mngr's must be it; a user's own `statusLine` (in `settings_overrides` or the synced global settings) is preserved by **composing** it -- `statusline.sh` runs the user's command with the same payload and emits only its output, so the user's statusline renders verbatim. A `statusLine` that isn't a runnable command block is dropped with a warning.

## 2026-06-11

Strengthened the two-space-indent assertions in `antigravity_config_test.py`. The
previous `assert "  " in serialized` checks could not distinguish two-space from
four-space (or wider) indentation, so they did not actually verify the format the
serializers promise. They now assert that a top-level key line begins with exactly
two spaces.

## 2026-06-08

Fixed the antigravity onboarding seed so it also skips agy's first-run NUX for users authenticated through an enterprise account. The seed now marks `enterpriseOnboardingComplete` as `True` (previously `False`), which was leaving enterprise-authenticated users stuck in the enterprise onboarding flow on their first message.

Fixed: passing a model name (or any value containing spaces or parentheses) as an `agy` argument no longer breaks `mngr create`.

Passing `--model "Gemini 3.5 Flash (Medium)"` to an `antigravity` agent previously produced `agy --model Gemini 3.5 Flash (Medium) ...` in the shell-evaluated launch command, so bash word-split the value and parsed `(Medium)` as a subshell (`syntax error near unexpected token '('`). The underlying fix is in `mngr` (`agent_args` are now shell-quoted in `BaseAgent.assemble_command`); the `antigravity` plugin inherits it.

Note: the model is normally set via `settings_overrides` (a `model` key in the per-agent `settings.json`), which is the supported path and is unaffected. This fix covers the case where a model is instead passed explicitly as a CLI argument.

Standardized this plugin's test setup on `register_plugin_test_fixtures(globals())`
instead of `pytest_plugins = ["imbue.mngr.conftest"]`, so HOME isolation is wired
the same single way across all mngr plugins. Internal test-infrastructure change
only; no user-facing behavior change.

## 2026-06-05

`antigravity` agents now stay RUNNING while a subagent or backgrounded task they launched is still working, instead of flipping to WAITING the moment the root agent's turn ends.

- The `Stop` hook no longer clears the `active` lifecycle marker on any `fullyIdle:true`. agy runs the Stop hooks for *every* conversation -- the root agent and each subagent it launches share the same hook -- and a subagent fires its own `"fullyIdle":true` Stop when it finishes, which can arrive while the root agent is still working. Clearing on that would wrongly report WAITING mid-turn.
- `PreInvocation` now runs `set_active_marker.sh`, which touches the marker and records the turn's *root* conversation (the one that opened the turn, seen while the marker was absent) in `root_conversation`. `Stop` runs `clear_active_marker_when_idle.sh`, which clears the marker only when the payload's conversation id matches that root **and** reports `"fullyIdle":true`. The root is re-recorded at each turn boundary, so `/clear`, `/fork`, `/switch`, and resume stay correct.
- The marker drives `BaseAgent`'s RUNNING/WAITING detection (present => RUNNING, absent => WAITING).
- Conversation resume is centralized on the same root-conversation tracking: on `mngr start` an antigravity agent now resumes its *main* conversation from `root_conversation` rather than the last line of the conversation-ids file. Previously, because subagents also append to that file, a stop/start could resume a subagent's conversation instead of the agent's own. The conversation-ids file is now used only as the set of conversations to scope transcript streaming, and `capture_conversation_id.sh` records each distinct id once (order/recency no longer matter).
- Verified live against agy 1.0.5: a backgrounded shell task produced the interim `fullyIdle:false` then final `fullyIdle:true` root Stop; a user message sent mid-flight (while the background task ran) kept the marker held and the root conversation unchanged; and a subagent's own `fullyIdle:true` Stop (a different conversation id) did not clear the root's marker, which was cleared only by the root's final Stop.

Each `antigravity` agent now runs `agy` under its own per-agent `$HOME` (at `<agent_state_dir>/plugin/antigravity/home/`), giving each agent its own permission policy, model, and isolated config/transcript/session state instead of today's all-or-nothing `--dangerously-skip-permissions` and shared global `~/.gemini`. Two new agent-type config fields:

- `settings_overrides` (dict, default `{}`) -- a free-form blob merged last into the per-agent `settings.json`, covering `permissions` (`{allow, deny, ask}`, precedence Deny > Ask > Allow), `toolPermission`, and `model` (an `agy models` display name). Mirrors `mngr_claude`'s field of the same name.
- `sync_home_settings` (bool, default `true`) -- base the per-agent `settings.json` on a copy of the user's real (global) `settings.json`, with `settings_overrides` layered on top; `false` starts from an empty base. This copies only agy's *global* `settings.json` scope (in practice theme/telemetry/trust); the user's model, permission grants, and behavioral policies live in other agy config scopes (`config/config.json`, per-project `config/projects/<uuid>.json`) that are intentionally not read, so set per-agent model/permissions via `settings_overrides`.
- `symlink_oauth_token` (bool, default `true`) -- symlink each agent's `antigravity-oauth-token` to the shared `~/.gemini` token (enables write-through sharing/propagation, see below) vs copy it for full per-agent isolation.

Other changes:

- Trust now splits by what is persisted: the durable source-repo path goes to the user's global settings (so trust isn't re-prompted across agents/worktrees of the same repo), while the transient per-agent workspace path goes only into the per-agent settings. Consent gating is unchanged in spirit (interactive prompt / `--yes` / `auto_dismiss_dialogs`, else clean `SystemExit`); mngr never silently runs an agent on untrusted code.
- Lifecycle hooks now live at the per-agent `$HOME/.gemini/config/hooks.json` and execute directly -- the previous `--add-dir` + `/tmp` hooks-symlink workaround is removed.
- agy's first-run NUX is skipped via a seeded `cache/onboarding.json`. Each agent's `antigravity-oauth-token` is created as a symlink to the shared `~/.gemini/antigravity-cli/antigravity-oauth-token` (default) -- even when that shared token doesn't exist yet. Because agy writes the token in place (verified empirically -- it does not use temp-file + rename), the first agent's login writes *through* the symlink to the shared path, authenticating every other agent and propagating refreshes -- "log in once, anywhere", no manual token handling. This also resolves the spec's open "token-refresh clobbering" risk. `symlink_oauth_token = false` opts into per-agent isolation (copy if a shared token is present, else sign in per agent).
- Path resolution is host-aware (the user's real `$HOME` and OS are resolved on the host in one round-trip), so the token/settings/cache sharing works on remote hosts too. Heavy `ms-playwright-go` browser binaries are shared across agents by symlinking each agent's home cache to the user's real host cache; this (and the oauth-token symlink/copy) is set up at provision time via the shared `imbue.mngr.hosts.common.symlink_or_copy_on_host` helper, so the launch command no longer carries bespoke cache shell.

Auth note: this works on both Linux (no keychain -- the file token is native) and macOS (where agy stores the token in the login keychain, which a relocated per-agent `$HOME` can't reliably read, so the symlinked file token is the cross-agent mechanism there too). On macOS, signing in may surface a harmless system popup -- *"A keychain cannot be found to store \"antigravity.\""* -- because the relocated `$HOME` has no per-agent keychain; agy falls back to the file token and auth completes normally (documented in the README). See the package README.

Internal refactor (no behavior change): the per-agent source-repo trust resolution now delegates to the shared core helper `imbue.mngr.utils.git_utils.find_git_source_path` (extracted from the previously duplicated `mngr_claude` / `mngr_antigravity` methods).

## 2026-06-04

Stopped `antigravity` agents now resume their prior agy conversation on restart, instead of starting a fresh one.

- A `PreInvocation` capture hook records the agent's active agy conversation ID (read from agy's hook payload, which carries `conversationId`) to a per-agent file. On `mngr start`, the launch command resumes the most-recently-active conversation via `agy --conversation <id>`, so the agent keeps its full context across a stop/start. The resume is shell-evaluated at launch (the stored command is replayed on each start) and works under both bash and zsh.
- Resume relies on agy's own incrementally-written conversation store, which survives the hard process kill `mngr stop` performs. If the conversation was pruned, agy warns and starts fresh on its own, so mngr passes `--conversation` whenever an ID is recorded without stat-ing agy's store (keeping the launch command decoupled from agy's on-disk layout).
- Note: agy's `--conversation` only resumes an existing conversation; it cannot mint a caller-supplied ID. mngr therefore lets agy assign the ID and captures it via the hook.

The transcript streamer now discovers this agent's conversation IDs from the same capture-hook file rather than grepping agy's `--log-file`. This is the single source of truth for conversation IDs (shared with resume), and it removes a latent bug where resumed conversations were missed because their log line reads `Resuming conversation` (not the `Resumed conversation` the streamer matched).

Clone-resume (making a cloned antigravity agent continue the source's conversation) is not included here -- agy's conversation store is global rather than per-agent, so it needs separate handling and is left for a follow-up.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-01

The `antigravity` agent type now uses agy hooks to report lifecycle state (verified working against agy 1.0.3).

- mngr provisions a per-agent `hooks.json` and points agy at it with `--add-dir` (via a `/tmp` symlink, since agy rejects the dotted state-dir path), so the user's global `~/.gemini/config/` is untouched and each agent's state stays isolated.
- A `PreInvocation`/`Stop` hook pair maintains an `active` marker so antigravity agents now report RUNNING while working and WAITING when idle (previously they had no `active` marker and could not report RUNNING).
- `auto_allow_permissions = true` continues to use the `--dangerously-skip-permissions` CLI flag. agy's documented `PreToolUse` `{"decision": "allow"}` hook output does not actually gate the `run_command` confirmation dialog, so a hook can't replace the flag.

Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the hook execution engine never runs (it executes hooks only from `~/.gemini/config/hooks.json` and workspace `.agents/`; the TUI path is loaded for display only -- agy bug, reported as antigravity-cli#49). mngr writes its own per-agent file via `--add-dir` and does not rely on the TUI.

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

Rename `mngr_gemini` to `mngr_antigravity`; agent type `gemini` is replaced by `antigravity`. Google announced on 2026-05-19 that the Gemini CLI is being superseded by the Antigravity CLI (`agy`); the legacy request path turns off for paid-tier accounts on 2026-06-18. The plugin was never released, so this is a destructive rename with no shim. The new CLI is architecturally closer to Claude Code than to Gemini CLI: process name is `agy`, hook event names match Claude's (`SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Stop`, `Notification`), and `--dangerously-skip-permissions` is the documented auto-allow flag. `auto_allow_permissions=True` is wired through that CLI flag rather than a permission hook. The first-launch "Do you trust this folder?" dialog is dismissed Claude-style (mirroring `mngr_claude`'s `interactively_dismiss_claude_dialogs`): under `mngr create --yes` or `auto_dismiss_dialogs=True` (per-agent-type opt-in, default `False`) the agent's `work_dir` is silently appended to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces`; in interactive shells mngr prompts via `click.confirm` before mutating the file; non-interactive shells without either opt-in raise `UserInputError`. There is no `GEMINI_CLI_TRUST_WORKSPACE` env-var analog in agy 1.0.0, so the user-tier settings file is the only place to register trust. `emit_common_transcript=True` (default) wires the JSONL transcripts agy writes to `~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl` into mngr's common-transcript schema, scoped per-agent by grepping agy's own `--log-file` for `Created conversation <uuid>` lines. The readiness sentinel that `mngr_gemini` shipped is **not** re-introduced -- live testing showed agy loads `hooks.json` correctly but hook execution is gated behind the `json-hooks-enabled` experiment flag (Google-controlled); once the flag is GA the sentinel can come back.

- `AntigravityAgentConfig.merge_with` follows mngr's new assign-by-default semantics: an override's `cli_args` replaces the base's (rather than concatenating). To opt back into additive layering, use the `__extend` operator with an explicit list value, e.g. `cli_args__extend = ["--verbose"]`; the string-shorthand form that the bare `cli_args` field accepts (which the validator splits via shlex) is not accepted by the `__extend` resolver. See the `mngr` changelog entry for the full breaking-change writeup.

Update the Antigravity plugin to use the structured `TmuxWindowTarget` type for
tmux pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `antigravity_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit when an
Antigravity agent's session was killed but a sibling session whose name shares
this name as a prefix was still alive, leaking the transcript streamer and
common-transcript converter for stopped agents.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Add `specs/mngr-gemini-feature-parity/concise.md` mapping out a seven-PR plan to bring `mngr_gemini` closer to feature parity with `mngr_claude` (settings management, hook injection, lifecycle hookimpls, session adoption, headless variant, skill-provisioned subtypes). The two sibling-package PRs from an earlier draft (`mngr_gemini_usage`, `mngr_gemini_subagent_proxy`) are deferred.

Add `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py`, the foundation for the remaining PRs: read/write helpers for `~/.gemini/settings.json` and its workspace/system counterparts (atomic write with `.bak` backup, malformed-JSON-tolerant), env-var interpolation matching Gemini CLI's `$VAR` / `${VAR}` / `${VAR:-default}` syntax, two hook-config builders (`SessionStart` readiness sentinel and `BeforeTool` permission auto-allow), and merge helpers that skip duplicate matcher groups.

Wire the new readiness hook into `GeminiAgent`. `provision()` writes a small mngr-owned settings file to `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` containing the `SessionStart` hook, and `modify_env_vars()` points Gemini at it via `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (Gemini's documented system-tier override) plus `GEMINI_CLI_TRUST_WORKSPACE=true`. The previous `--skip-trust` default is dropped from `cli_args`. The user's workspace and `~/.gemini/` are not touched -- no `.gemini/` directory appears in the project, no merge with user files. After this change, `mngr` can detect a Gemini agent's readiness from `$MNGR_AGENT_STATE_DIR/session_started` instead of polling the rendered TUI.

Add an opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` (default `False`, mirroring `mngr_claude`). When enabled, `provision()` also installs a `BeforeTool` wildcard hook in the same system-settings file that auto-approves every tool call by emitting `{"decision":"allow"}` on stdout. Preferred over the `-y`/`--approval-mode yolo` CLI flag because the hook survives admin policies that disable yolo mode and shows up explicitly in Gemini's `--debug` hook-registry output.

Wire the readiness sentinel into `GeminiAgent.wait_for_ready_signal`. `mngr create gemini` and `mngr reconnect` now block on `$MNGR_AGENT_STATE_DIR/session_started` being touched by the `SessionStart` hook installed in PR2, instead of polling the rendered TUI banner exclusively. `assemble_command` prepends `rm -f` of the sentinel so a leftover from a previous run can't trick the ready-detection into succeeding before the new Gemini session has started. End-to-end smoke against Gemini CLI 0.42.0: the sentinel appeared ~2.4s after launch and ready-detection unblocked cleanly.

# Gemini agents now produce a common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

The gemini plugin also captures the *raw* gemini session JSONL verbatim into
`logs/gemini_transcript/events.jsonl`. This preserves every field gemini emits (model
metadata, internal blocks, etc.) and lives inside the agent state dir, so the transcript
survives cleanup of gemini's own `~/.gemini/tmp/` working directories.

`GeminiAgent` satisfies the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts` and
shipping the matching per-agent scripts.

## 2026-05-14

Add `gemini` agent type plugin (`imbue-mngr-gemini`) that wires Google's Gemini CLI into mngr.

## mngr_gemini-feature-parity: seven-PR plan toward parity with mngr_claude

Add `specs/mngr-gemini-feature-parity/concise.md` mapping out a seven-PR plan to bring `mngr_gemini` closer to feature parity with `mngr_claude` (settings management, hook injection, lifecycle hookimpls, session adoption, headless variant, skill-provisioned subtypes). The two sibling-package PRs from an earlier draft (`mngr_gemini_usage`, `mngr_gemini_subagent_proxy`) are deferred.

Add `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py`, the foundation for the remaining PRs: read/write helpers for `~/.gemini/settings.json` and its workspace/system counterparts (atomic write with `.bak` backup, malformed-JSON-tolerant), env-var interpolation matching Gemini CLI's `$VAR` / `${VAR}` / `${VAR:-default}` syntax, two hook-config builders (`SessionStart` readiness sentinel and `BeforeTool` permission auto-allow), and merge helpers that skip duplicate matcher groups.

Wire the new readiness hook into `GeminiAgent`. `provision()` writes a small mngr-owned settings file to `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` containing the `SessionStart` hook, and `modify_env_vars()` points Gemini at it via `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (Gemini's documented system-tier override) plus `GEMINI_CLI_TRUST_WORKSPACE=true`. The previous `--skip-trust` default is dropped from `cli_args`. The user's workspace and `~/.gemini/` are not touched -- no `.gemini/` directory appears in the project, no merge with user files. After this change, `mngr` can detect a Gemini agent's readiness from `$MNGR_AGENT_STATE_DIR/session_started` instead of polling the rendered TUI.

Add an opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` (default `False`, mirroring `mngr_claude`). When enabled, `provision()` also installs a `BeforeTool` wildcard hook in the same system-settings file that auto-approves every tool call by emitting `{"decision":"allow"}` on stdout. Preferred over the `-y`/`--approval-mode yolo` CLI flag because the hook survives admin policies that disable yolo mode and shows up explicitly in Gemini's `--debug` hook-registry output.

Wire the readiness sentinel into `GeminiAgent.wait_for_ready_signal`. `mngr create gemini` and `mngr reconnect` now block on `$MNGR_AGENT_STATE_DIR/session_started` being touched by the `SessionStart` hook installed in PR2, instead of polling the rendered TUI banner exclusively. `assemble_command` prepends `rm -f` of the sentinel so a leftover from a previous run can't trick the ready-detection into succeeding before the new Gemini session has started. End-to-end smoke against Gemini CLI 0.42.0: the sentinel appeared ~2.4s after launch and ready-detection unblocked cleanly.

## mngr-gemini-transcript: common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

The gemini plugin also captures the *raw* gemini session JSONL verbatim into
`logs/gemini_transcript/events.jsonl`. This preserves every field gemini emits (model
metadata, internal blocks, etc.) and lives inside the agent state dir, so the transcript
survives cleanup of gemini's own `~/.gemini/tmp/` working directories.

`GeminiAgent` satisfies the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts` and
shipping the matching per-agent scripts.
