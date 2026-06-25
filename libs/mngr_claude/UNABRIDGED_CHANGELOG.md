# Unabridged Changelog - mngr_claude

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-24

**Bug fix:** in shared mode (`isolate_local_config_dir = false`), mngr no longer forces `CLAUDE_CONFIG_DIR=~/.claude` into the agent environment when your shell did not already have it set. A previous fix injected `CLAUDE_CONFIG_DIR` unconditionally, but exporting it -- even to claude's own `~/.claude` default -- is not equivalent to leaving it unset: claude reads its global `.claude.json` (onboarding state, theme, trust, history) from `$CLAUDE_CONFIG_DIR/.claude.json` when the variable is set, but from `~/.claude.json` (beside the dir) when it is unset. The forced value pointed claude at an inner stub file lacking your onboarding state, so every new shared-mode agent re-showed the theme/onboarding screen.

mngr now propagates `CLAUDE_CONFIG_DIR` in shared mode only when your own shell already exported it (in which case your config already lives inside that dir and sharing stays consistent). When unset, it stays unset so claude resolves its real `~/.claude.json`. The launch command's session-file lookup no longer depends on the variable being exported; it falls back to `$HOME/.claude` via `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`.

Renamed the claude agent type's `use_env_config_dir` option to `isolate_local_config_dir` and flipped its meaning: it now defaults to `true` (each local agent gets its own per-agent Claude config dir, so mngr never touches your default config). Set `isolate_local_config_dir = false` to share the user's `$CLAUDE_CONFIG_DIR` instead (the old `use_env_config_dir = true` behavior). The flag only affects local agents; remote agents always use an isolated config dir and ignore it.

The old `use_env_config_dir` key is still accepted as a deprecated option (it is the inverse of `isolate_local_config_dir`), so existing config files keep working -- mngr emits a deprecation warning when it is set. Setting both keys to contradictory (non-inverse) values is an error.

**Bug fix:** in shared mode (`isolate_local_config_dir = false`), mngr now injects `CLAUDE_CONFIG_DIR` into the agent environment, pointing at the user's shared config dir (`$CLAUDE_CONFIG_DIR`, or `~/.claude` when unset). Previously it was left unset, so the launch command's session-file lookup saw an empty `$CLAUDE_CONFIG_DIR` and claude could not find the user's config or sessions.

**New warning:** when creating a local claude agent on macOS, if mngr detects that you authenticate Claude Code with a claude.ai subscription (OAuth credentials) and config-dir isolation is enabled, it now warns that the isolated agent's copy of your credentials will go stale as the subscription refreshes them, and prints the exact command to disable isolation (`mngr config set agent_types.claude.isolate_local_config_dir false --scope user`).

Shared Claude config mode (`isolate_local_config_dir = false`) now dismisses the
cosmetic startup dialogs (trust, onboarding, effort callout, cost threshold)
directly in your default Claude config so they no longer intercept automated
input. Previously shared mode left the config untouched, so a fresh `~/.claude.json`
re-triggered the trust/onboarding screens on every agent.

mngr writes these dismissals into the file claude actually reads
(`$CLAUDE_CONFIG_DIR/.claude.json`, or `~/.claude.json` when the var is unset), and
honors `auto_dismiss_dialogs` in this mode. It never accepts bypass-permissions
mode via the global config -- that remains governed by `settings.json` -- and still
does no per-agent settings.json or keychain provisioning.

Also fixed: in shared mode, mngr's hooks are now installed when shared mode is set
via the current `isolate_local_config_dir = false` flag (previously they were only
installed when set via the deprecated `use_env_config_dir = true` alias).

## 2026-06-19

Fixed mngr's Claude hooks leaking into "normal" (non-mngr) Claude config. mngr previously wrote its readiness/credential/permission hooks into the project's `.claude/settings.local.json`, which plain `claude` runs in that directory also read -- so the hooks (e.g. an activity-event `mkdir`) fired outside mngr where `$MNGR_AGENT_STATE_DIR`/`$MNGR_HOST_DIR` are unset, producing errors like `mkdir: cannot create directory '/events': Permission denied`.

mngr now bakes all of its own hooks into the per-agent config-dir `settings.json` (`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/settings.json`) -- the "user" settings layer Claude reads from `$CLAUDE_CONFIG_DIR`, which a plain `claude` run in the work dir never reads (it reads `~/.claude`). The hooks are built fresh on every provision, so there is no cross-version accumulation and nothing lands in a file plain `claude` reads.

`settings_overrides` is now applied as a **config-consistent patch** folded onto that `settings.json` (replacing the previous deep-merge-by-default). A bare key assigns (and the narrowing guard hard-errors if it would silently drop a non-empty sibling aggregate from the home base, unless `allow_settings_key_assignment_narrowing` is set); a `key__extend` merges onto the home value (lists concatenate, sets union, dicts merge), and nesting `__extend` merges deeper. So a `permissions__extend = {allow__extend = [...]}` override merges its `allow` while preserving a `permissions.defaultMode` from the home settings (#1647), whereas a bare `permissions = {allow = [...]}` that would drop `defaultMode` now warns instead of silently dropping it. The narrowing guard is recursive: even a bare key nested inside an `__extend` value (e.g. `permissions__extend = {allow = [...]}` over a non-empty home `permissions.allow`) is now caught, not just top-level bare keys. A stray `__extend` in your own home `settings.json` is normalized away (it has nothing below it to extend), and because the fold's `finalize` collapses every node into a plain value, no `__extend` marker can survive into the final file by construction.

`settings_overrides` now **accumulates across config scopes and `parent_type` inheritance** rather than a higher/child scope replacing the entire lower/parent value: per-key, non-overlapping keys from every scope survive and same-key `__extend`s combine (a higher bare key still wins over a lower marker for the same key). For example, a user scope setting `permissions__extend.allow__extend` and a project scope setting `model` now both apply, instead of the project scope wiping out the user scope's permissions.

A `settings_overrides` key can now use `key__assign` to assign a value **without** triggering the narrowing guard -- the per-key opt-out (e.g. `permissions__assign = {allow = [...]}` deliberately replaces the home `permissions`, dropping `defaultMode`, with no warning and without the global `allow_settings_key_assignment_narrowing` flag). It also works nested inside an `__extend` (`permissions__extend = {allow__assign = [...]}`). A bare `key` and `key__assign` for the same field in one layer raises a clear error. A `Static*`-wrapped override value (`StaticList` / `StaticDict` / `StaticTuple`) is likewise treated as a whole-value replacement and exempt from the narrowing guard.

A user-supplied `--settings` (in an agent type's `cli_args` or passed through on the `mngr create` command line) now passes through to `claude` verbatim. mngr injects no `--settings` of its own, and Claude natively layers the user's `--settings` over the config-dir `settings.json` (deep-merging dicts, concatenating same-event hooks), so the user's hooks and mngr's both fire with no mngr merge code and nothing to collide.

Reduced-support limitation in `use_env_config_dir` mode: there is no per-agent config dir to bake hooks into, so mngr loads its hooks (and the resolved `settings_overrides` patch) from the private managed `--settings` file (`$MNGR_AGENT_STATE_DIR/plugin/claude/mngr_managed_settings.json`), which Claude layers (highest precedence) over the user's shared config. `settings_overrides` is applied here too (folded onto mngr's hooks as the base, so its narrowing guard only catches an override dropping mngr's own hooks, not the shared-config values Claude layers itself). Because mngr already passes its own `--settings` in this mode, a user-supplied `--settings` (in `cli_args`/`agent_args`) is now rejected at provision with a `UserInputError` (mngr can't reliably merge a second `--settings` -- its value may be inline JSON, not a file); put those settings in `settings_overrides` or set `use_env_config_dir=False`. This mode is not yet used in production.

Note: this stops *new* leaks; it does not remove hooks already written into existing `settings.local.json` files by a prior mngr -- clean those up manually if present.

`mngr create` no longer requires the project's `.claude/settings.local.json` to be gitignored across the board. mngr writes its own hooks to the per-agent config dir, so that requirement now applies only when the `claude_subagent_proxy` plugin (PROXY mode) actually needs to rewrite user-defined Stop hooks in `settings.local.json` -- enforced by that plugin, at the point it writes.

Internal: the `settings_overrides` fold now imports the config-merge primitives from the new standalone `overlay` library rather than from `imbue.mngr.config`, and the fold itself now runs on `overlay`'s typed-node algebra (`lift`/`merge_narrowing_allowed`/`finalize`) instead of the string-suffix engine. The user-visible behavior is unchanged with one intentional fix: a deferred `key__assign` inside `settings_overrides` now reliably suppresses the narrowing guard at provision (previously the no-warn intent could be silently dropped during config-load, leaving it narrowing-checked).

Cross-scope `settings_overrides` narrowing is now caught at config-load. A claude agent type's `settings_overrides` accumulates across config scopes (user < project < local) and env-var layers, so previously a higher scope whose `settings_overrides` *bare* key replaced a non-empty aggregate set by a lower scope dropped the lower scope's entries silently (the provision-time guard only caught drops of the *home* `settings.json` base, not of a lower config scope). The config loader now surfaces these cross-scope drops through the same flag-gated narrowing error used for every other field: e.g. a local-scope `[agent_types.my_claude.settings_overrides.permissions] allow = ["B"]` that drops a project-scope `allow = ["A"]` raises, escapable via `allow_settings_key_assignment_narrowing = true` or by using `allow__extend` (accumulate) / `allow__assign` (replace without warning). Purely additive cross-scope `settings_overrides` (adding new keys) loads unchanged.

Internal (no user-facing behavior change): deduped `resolve_shared_claude_config_dir` to delegate to `get_claude_config_dir` (identical resolution), and `_claude_json_has_primary_api_key` now reads via the shared `read_claude_config` helper instead of hand-rolling the JSON read (corrupt-config tolerance unchanged). The managed-settings write in `_configure_agent_hooks` now routes through `write_json_dict_via_host`.

Trimmed the README to user-relevant content (removed internal implementation details and roadmap notes) and tightened it for concision.

`settings_overrides` now expresses merge intent with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes, so the same overrides behave consistently whether mngr or vanilla Claude Code reads them.

The suffixes leaked into the generated `settings.json` as keys Claude does not recognise; they are now rejected in `settings_overrides`. Declare merge intent in a top-level `__mngr_merge` map (`__mngr_merge = {"permissions.allow" = "extend"}` -- or `"assign"`) that vanilla Claude ignores. A bare key assigns with the narrowing guard, and the narrowing error prints the exact `__mngr_merge` patch to add -- the full nested patch in one error (a dict that would drop a sibling key is suggested as `extend`, a replaced list/value as `assign`). A `__mngr_merge` key found in your home `settings.json` is stripped (it is a no-op there).

## 2026-06-18

Added an `update_policy` field to the claude agent type that governs Claude Code's background auto-updater. `NEVER` sets `DISABLE_AUTOUPDATER=1` in the agent environment so the installed (optionally `version`-pinned) binary stays put; `AUTO` leaves the auto-updater enabled; `ASK` behaves like `AUTO` (claude has no interactive update flow). When unset, it defaults to `NEVER`.

**Behavior change:** claude agents now disable Claude Code's auto-updater by default (local and remote). Previously mngr did not disable it on local agents -- the per-agent config inherited your `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated. Set `update_policy = "AUTO"` to opt back into the auto-updater. The policy is ignored in `use_env_config_dir` (shared) mode, where mngr leaves your claude environment alone.

Pin a specific version with `version` to control exactly what gets installed.

- The Claude response-streaming snapshot watcher now captures the agent's tmux pane by the configured primary window name (`tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so response streaming works regardless of the user's tmux `base-index`.
- Internal refactor: the Claude agent now builds its tmux session name via the shared `AgentInterface.session_name` helper instead of hand-rolling the `prefix + name` string, keeping it consistent with mngr's centralized session-name construction.

Internal: renamed the example custom agent type in an `on_before_create` test from a personal-config name to a neutral `coder`. No behavior change.

## 2026-06-17

`ClaudeAgent` now declares the `HasStreamingSnapshotMixin` capability mixin (it already implemented `get_stream_buffer_path`), so the live in-progress response-streaming view is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact.

`ClaudeAgent` also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config).

`ClaudeAgent` also declares `HasVersionManagementMixin` (version pin, else auto-update).

The auto-allow permission apply-path now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`ClaudeAgent` now declares `CliBackedAgentMixin` (marking it as wrapping a specific external CLI, which scopes the CLI-only capability-matrix rows) and `HasSessionAdoptionMixin`. Its session-adoption logic moved from `on_after_provisioning` into an `adopt_session` method (called by `on_after_provisioning`), so `--adopt-session` / `--from` session resumption is now a code-detectable `session_resume` capability. Behavior is unchanged.

Split the Claude agent into a shared `ClaudeCoreAgent` base and the interactive `ClaudeAgent(ClaudeCoreAgent, InteractiveTuiAgent, ...)` subclass. The core holds everything not tied to the interactive TUI (config-dir setup, credentials, transcripts, session preservation, auto-install, version management, the provisioning flow); the TUI subclass holds the keystroke send/readiness pipeline, the streaming snapshot (`HasStreamingSnapshotMixin`), session adoption (`HasSessionAdoptionMixin`), and start-dialog dismissal + pre-provisioning validation. `HeadlessClaude` extends `ClaudeCoreAgent` directly, so the headless variant no longer structurally inherits those interactive-only capabilities -- in the capability matrix `headless_claude` is now `n/a` (not `Y`) for `session_resume`. One user-visible change: `--adopt-session` is now rejected for `headless_claude` with a clear error, instead of being silently accepted and never resumed (headless runs `claude --print`, not `--resume`).

Post-split cleanup of now-redundant scaffolding: dropped the empty `NoPermissionsClaudeAgent` intermediate base (its no-op overrides became core defaults; `HeadlessClaude` extends `ClaudeCoreAgent` directly), the no-op `ClaudeCoreAgent.on_before_provisioning` (it just restated `BaseAgent`'s no-op default), and `HeadlessClaude`'s vestigial `_preflight_send_message` override (headless claude is not an interactive agent: the send-message flow, including `_preflight_send_message`, now lives only on the interactive `SendKeysAgent` side, and messaging a headless agent is refused at the call layer by `require_interactive_agent`). `HeadlessClaude` now carries one explicit `is_unattended_enabled` override that makes its `ClaudeCoreAgent` (config-driven) vs `BaseHeadlessAgent` (always-True) diamond choice deliberate (behavior unchanged: config-driven, as before the split).

The entire start-dialog-dismissal concern is now TUI-only: the shared `ClaudeCoreAgent.provision()` calls a single `_dismiss_start_dialogs` seam that is a no-op on the core and carries the real block (auto-dismiss or interactive prompt) only on the interactive `ClaudeAgent`. Previously the interactive prompt was already skipped for headless, but the `auto_dismiss_dialogs=True` path still ran for headless; now a headless claude does no start-dialog handling at all (it has no TUI pane to protect from intercepted input).

`ClaudeCoreAgent` now installs claude through the shared `ensure_cli_installed` helper (consent-gated locally, config-gated remotely; claude's `get_install_command` pins the version) instead of its own bespoke install block, then calls `reconcile_installed_version` to verify the present binary matches any pinned `version` (raising on mismatch). User-visible change: install / version-mismatch failures now raise `AgentInstallationError` (the shared installer's error type) rather than `PluginMngrError`.

The session-adoption create option and its agent-agnostic validation moved out of the claude plugin into core, since session adoption is now a capability shared by every interactive agent. The option is now `--adopt` (with `--adopt-session` kept as an accepted alias). Claude's `register_cli_options` no longer declares the option, and its `on_before_create` retains only the claude-specific fail-fast pre-resolution of named session ids. Claude now reads the adopted session ids from the first-class `CreateAgentOptions.adopt_session` field instead of the old `plugin_data["adopt_session"]` namespaced key (in both `on_before_create` and the `adopt_session` method). Claude's session-store scanner now routes through the shared `iter_agent_session_paths` helper. No change to claude's user-facing adoption behavior other than the option rename.

Claude now ships a release test (`test_claude_agent_e2e.py`) built on the shared agent release-test harness, so it is held to the same end-to-end lifecycle arc as the other agent types (create -> WAITING -> message -> RUNNING -> transcript -> stop/start resume -> destroy -> preserve -> adopt the preserved session into a fresh agent -> recall). The profile turns on the richer assertions (RUNNING marker, forced tool call, token usage), resolves adoption by the preserved session JSONL's path, and pins the agent to the `haiku` model tier (the seed/recall turns don't need a frontier model). Skipped unless `claude` is on PATH and `ANTHROPIC_API_KEY` is set.

Removed the now-redundant `test_adopt_session_brings_context_from_mngr_claude_agent_session` release test (and its `_create_mngr_claude_session` helper): adopting an mngr-managed agent's own preserved session into a fresh worktree is now exercised by the shared harness arc above. `test_adopt_session.py` retains the vanilla-`claude`-CLI adoption case, which the harness does not cover.

Fixed `--adopt A --from X` (combined explicit-adopt plus clone): the explicit session is copied into the destination's encoded project dir first, so the clone's rekey now merges the source-encoded subdir's files into that pre-existing dir instead of refusing on a whole-directory clobber. The merge is non-destructive and refuses (raising `AgentStartError`) only on a genuine per-file collision (the same session-id filename present in both), so distinct sessions coexist cleanly in one project dir while real data loss is still prevented.

A `--from` clone whose source has no resumable session now warns and adopts nothing (rather than raising): `--from` is a workspace clone, so carrying the source's conversation forward is a bonus, not a requirement. The agent still starts (falling back to the last `--adopt` session, or a fresh start). Explicit `--adopt` failures and the per-file merge collision remain hard errors.

`mngr create --yes` now dismisses claude's first-run *dialogs* (onboarding, effort callout, work-dir trust) in the per-agent config -- previously these were only auto-dismissed for a remote/unattended agent or the explicit `auto_dismiss_dialogs` config, so a local `--yes` create relied on the global `~/.claude.json`. `--yes` deliberately does *not* accept bypass-permissions mode (tool auto-allow stays governed by `auto_allow_permissions`/unattended): it auto-approves prompts, it does not silently widen tool permissions.

The claude release test now gitignores `.claude/settings.local.json` in its own `.gitignore` in both the seed and adoption worktrees, which mngr's claude preflight requires (a repo-local rule, not a global one) before it will write per-agent hooks. Without it the test could not get past `create`.

The claude release test no longer seeds a `~/.claude.json` to dismiss first-run dialogs and pre-approve the API key. It now relies entirely on the product behavior: `mngr create --yes` dismisses the dialogs and trusts the work dir, and the plugin's `approve_api_key_for_claude` pre-approves `ANTHROPIC_API_KEY`. The test builds its subprocess env with the plain shared `get_subprocess_test_env`, matching the sibling agent release tests.

Fixed resuming a Claude agent and immediately sending it a message. The TUI-ready indicator is now the input-prompt glyph (`❯`) instead of the "Claude Code" welcome banner. The banner only renders on a fresh start, not when resuming a saved session, so resumes previously skipped the readiness wait and could drop the message into a still-replaying transcript.

Adapted the Claude agents to the unified live-output contract.

`ClaudeAgent` (TUI) now inherits `SupportsLiveOutputMixin` directly (instead of the removed `HasStreamingSnapshotMixin`), exposes its streaming snapshot via `get_live_output_path()`, and supplies a `SnapshotDeltaReader` from `make_live_output_reader()`. The stream_buffer snapshot parsing/diffing (`compute_stream_delta` and friends, previously in `mngr_robinhood`) moves into the new `imbue.mngr_claude.stream_buffer` module alongside that reader, since it is the Claude watcher's format.

`HeadlessClaude` keeps streaming `claude --print` stream-json output via `stream_output()`, but the tail loop is now the shared one in mngr; the agent only supplies a `StreamJsonReader` plus its startup-grace "finished" check and stderr-augmented error reporting. No user-visible behavior change.

The claude common-transcript converter now emits `finish_reason` (was `stop_reason`, aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array on assistant records that preserves the source interleaving of text and tool-use blocks (`parts_ordered` true, since Claude's native content blocks are ordered).

## 2026-06-16

The common-transcript converter's event-conversion logic moved out of an inline `python3` heredoc in `common_transcript.sh` into a standalone `common_transcript_convert.py` (provisioned alongside the shell script), so it is type-checked, linted, and unit-tested directly. Malformed raw-transcript lines, unreadable existing-output lines, and transcript lines whose `message` is `null` (rather than an object) are dropped silently rather than aborting the conversion run.

The common-transcript watcher no longer echoes converter errors to the agent's pane: a genuine conversion error is recorded in the structured log only, instead of also being written to the watcher's stderr.

Internal refactor (no behavior change): the claude plugin's session-preservation-on-destroy now uses the shared `preserve_agent_state` / `preserve_host_agents_on_destroy` helpers in mngr core instead of its own inline copy. The preserved file set, the `preserve_sessions_on_destroy` config option, and the online/offline behavior are unchanged. The offline host-destroy path now also filters discovered agents by agent type.

Fixed: the synchronous transcript flush at turn end (which keeps a WAITING-signal consumer
from outrunning the common-transcript converter) now runs on *every* turn-end path.
Previously it lived in `wait_for_stop_hook.sh`'s `run_post_completion`, which is skipped on
the no-`/proc` fast path (macOS / local agents, where the Claude-ancestor PID lookup fails)
and on the SIGTERM/SIGINT handler -- so on those paths the marker was cleared without
flushing and the converter race remained. The flush now lives in `mark_inactive`, which
every path calls before clearing the `active` marker.

The flush's lock-acquire wait -- its only potentially-slow step -- is now bounded by an
explicit per-call timeout, so the SIGTERM/SIGINT handler can't block on it: interrupts cap
the wait at 2s (`HOOK_FLUSH_LOCK_TIMEOUT_SIGNAL`) while normal turn-end paths use 30s
(`HOOK_FLUSH_LOCK_TIMEOUT`). The bound is a portable `MNGR_CONVERT_LOCK_TIMEOUT` handed to
each converter pass rather than a `timeout(1)` wrapper, which macOS lacks.

## 2026-06-15

Hardened the turn-end signal so consumers that read the common transcript on the WAITING
transition (e.g. an orchestrator harvesting the agent's final message) can no longer
outrun the converter. `wait_for_stop_hook.sh` now flushes the transcript pipeline (a
synchronous `--single-pass` of the raw streamer and common-transcript converter, in
pipeline order) before clearing the `active` marker -- so by the time the agent reports
WAITING the common transcript already reflects the final assistant message.

The flush and the converter's convert lock now come from the shared
`mngr_common_transcript_lib.sh` (see the `mngr` changelog) rather than being duplicated
per agent. The convert lock keeps the on-demand flush from racing the background 5s
daemon into duplicate events; its timeout is tunable via `MNGR_CONVERT_LOCK_TIMEOUT`.

The `waiting_reason` field in `mngr list` is now more robust against a stranded `permissions_waiting` marker. The `PERMISSIONS` reason is gated on the agent's `active` (in-turn) marker: a `permissions_waiting` file that outlived its turn (e.g. a denied/cancelled dialog whose cleanup hook was missed) now reports `END_OF_TURN` instead of wrongly showing `PERMISSIONS`. This matches the behavior of `ClaudeAgent.get_lifecycle_state`, which only consults the permission marker when the base state is RUNNING.

## 2026-06-14

`mngr create --adopt-session` now validates the session ID up front, before any host or worktree is created. Passing an unknown (or ambiguous) session ID fails fast with a clean `Error: ...` message instead of crashing mid-provisioning with a full "Unexpected error" traceback.

The "session not found" message is also concise now: it no longer enumerates every searched directory (which included one per local mngr agent, often hundreds of paths).

Internal: the existence/ambiguity check (`_resolve_adopt_session`) now also runs in the `on_before_create` hook, which executes outside `provision_agent`'s `ConcurrencyGroup`. Previously the only check happened in `on_after_provisioning` (inside that group), where the group's exit wrapped the `UserInputError` in a `ConcurrencyExceptionGroup` -- no longer a `ClickException` -- so it was reported as an unexpected error. The session source is always local, so the early result matches the provision-time resolution.

# Shared, typed Claude stream-json envelope

Added `imbue.mngr_claude.stream_json`, a single typed boundary for the Claude partial-message
stream-json envelope (`message_start` / `content_block_start` / `content_block_delta` /
`text_delta` / `content_block_stop` / `message_delta` / `message_stop`, plus the `assistant`
summary's inner message). It is defined against the `anthropic` SDK's discriminated
`RawMessageStreamEvent` union and `anthropic.types.Message`, so the protocol vocabulary is owned
upstream instead of hand-rolled as bare string literals. The consume side validates into the union
and dispatches with an exhaustive `assert_never` match, so a future `anthropic` release that adds an
event variant fails the type check and names exactly what we must handle.

- `mngr ask`'s headless reader (`headless_claude_agent.py`) now parses partial-message events and
  the `assistant` summary through this boundary. Behavior is unchanged for well-formed `claude`
  output; an event variant or content-block type newer than the installed `anthropic` package
  degrades gracefully (it is skipped / falls back to a lenient text scan rather than dropping the
  response).
- Added `anthropic` as a dependency (kept unpinned; imported for its typed models only -- mngr
  still drives the `claude` CLI and makes no API calls).

## 2026-06-12

`mngr create --adopt-session <session-id>` now resolves a bare session ID against more locations. In addition to the current and user-scope Claude config dirs (`$CLAUDE_CONFIG_DIR/projects/` and `~/.claude/projects/`), it now also searches every live local mngr agent's per-agent config dir and the preserved session files of destroyed agents (see `preserve_sessions_on_destroy`). Passing a full `.jsonl` path is unchanged. Only the local host dir is scanned for mngr agent and preserved sessions.

Clarified the `--adopt-session` help text and behavior: the option is repeatable, but when multiple sessions are named, every named session is made available in the new agent while only the last one is resumed on startup (Claude can only resume one session at a time).

Internal: routed the plugin's `host_dir / "agents"` path constructions through the shared `get_agents_root_dir` / `get_agent_state_dir_path` helpers (now defined in `imbue.mngr.hosts.common`). No behavior change.

The `.claude/settings.local.json` gitignore preflight/provisioning check now delegates to the shared `check_path_gitignore_status` helper in `mngr.api.git` rather than implementing the git-check-ignore logic inline. No user-visible behavior change.

Added a conformance test asserting that claude's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`), so the claude emitter and the shared
contract cannot drift apart.

## 2026-06-11

### Fixed

- Fixed: Provisioning a local Claude agent no longer creates self-referential symlink loops inside the user's shared `~/.claude/` (e.g. `~/.claude/skills/skills -> ~/.claude/skills`, `~/.claude/commands/commands`, `~/.claude/plugins/cache/cache`). `_sync_user_resources` used plain `ln -sf` as an idempotent command; on the second and later provisions the destination was already a symlink-to-directory, so `ln` dereferenced it and nested a new link inside the shared source. All sync symlinks (directory, child, and individual-file, including credentials and `keybindings.json`) now use `ln -sfn` (`--no-dereference`), which replaces the existing destination symlink instead of following it.

### Changed

- Changed: A skill-provisioned agent's primary skill (e.g. `code-guardian`, `fixme-fairy`) is now installed into that agent's own per-agent config dir (`$CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md`) instead of the user's global `~/.claude/skills/`. Previously the local install wrote to global `~/.claude/skills/`, which `_sync_user_resources` then symlinked into every local agent's config dir, so a `code-guardian`/`fixme-fairy` skill leaked into the skill list of every local agent. To support this, `skills/` is now synced via child-level symlinks (one symlink per skill, mirroring how `plugins/` is already handled) so the agent's own skill can live as a real file alongside the symlinked user skills without leaking back into the shared source. The local install is now silent (no interactive install/update prompt), matching the always-silent remote install.

## 2026-06-10

Test-quality hardening across the mngr_claude test suite (no user-visible behavior change). Replaced assertions that passed without verifying correctness with ones that check real effects:

- Seven `on_before_provisioning` tests that asserted nothing ("did not raise") now assert observable effects (config left untouched, missing-credentials warning present/absent, untrusted worktree rejected).
- `does-not-extend-trust` provisioning tests now assert the exact set of trusted projects instead of the presence of a key the test itself wrote.
- Transcript-converter truncation tests now assert exact lengths and the ellipsis marker (not just an upper bound), and the "skips event" tests now prove only the bad event is dropped (a known-good event survives) plus cover the missing-`timestamp` branch.
- Command-assembly and install-command tests now assert on shlex-parsed tokens / load-bearing flags instead of hand-rebuilt exact shell strings.
- Skill-install skip tests assert file content is unchanged instead of relying on mtime equality; added remote-install coverage. Custom-agent-type resolution test now sets a non-default field to prove it survives the merge.
- Grace-period headless test asserts the poll actually re-checked; removed dead `_patch_agent_as_stopped` calls and a fragile wall-clock timing assertion.
- claude_config no-op tests assert content is byte-for-byte unchanged; effort-callout check test isolated to the effort dialog.
- Removed an introduced `unittest.mock.patch` of the function under test in favor of the real no-credentials environment, and a duplicate local `temp_source_dir` fixture (now inherited from the shared modal conftest).
- Release/acceptance tests: the Modal provisioning test destroys the agent and asserts a non-empty preserved session JSONL; the adopt-session and modal tests drop brittle "Done." log-string checks; the no-dialog send_message test matches the specific downstream timeout; the background-tasks prefix-collision test asserts the script reached its gone-session exit; magic `sleep` literals replaced with a named constant.

- `ClaudeAgent` now supplies its own "message accepted" probe to the shared submission-confirm path: a shell command that reads the latest `enqueue` event from Claude's transcript event log (`logs/claude_transcript/events.jsonl`) and prints its ISO-8601 timestamp. This is the Claude-specific knowledge that previously lived (hardcoded) in the shared `tui_utils` module; moving it into the plugin keeps `tui_utils` agent-neutral while preserving the existing fast-confirm-on-enqueue behavior for Claude agents.

Raised the stale coverage floor from 24% to 80% to match the coverage CI already measures (~83%), and removed the now-obsolete comment referencing per-package offload coverage drift (the offload bug that caused that drift has since been fixed).

## 2026-06-09

- Readiness hooks: a Claude agent restarted/resumed mid-turn no longer reports the RUNNING lifecycle state forever. The `active` marker is set on UserPromptSubmit and only removed by the Stop / idle Notification hooks, so a turn abandoned by an abnormal exit (container restart, OOM, crash) left it stale and the agent stuck at RUNNING. The SessionStart hook now clears `active`/`permissions_waiting` on `startup`/`resume` (a fresh, not-mid-turn process), so the lifecycle state self-heals on the next (re)start; `compact` is excluded because auto-compaction fires mid-turn while Claude is genuinely active. The same hook touches a new `claude_process_started` marker whose mtime gives consumers a restart boundary to compare transcript timestamps against (any transcript event older than it belongs to a turn the current process did not run). The shared "clear active markers and emit an activity event" shell snippet is extracted into a constant reused by the Notification idle hook and the new SessionStart hook so the two stay byte-identical.

Claude session preservation on destroy was rewritten onto the new shared
`preserve_agent_data` machinery in core mngr. Behavior is unchanged in substance -- session
JSONLs, the raw and common transcripts, and the session-id history are still preserved before
the agent state directory is deleted, and `projects/` is still skipped in `use_env_config_dir`
mode -- but the implementation is now a single declarative list of files preserved through one
code path for both online and offline (volume-backed) hosts, replacing the previously
duplicated SSH and Volume implementations.

The on-disk layout of preserved sessions changed: files now live at
`<local_host_dir>/preserved/<agent-name>--<agent-id>/` and mirror the agent state directory
verbatim (e.g. `plugin/claude/anthropic/projects/...`, `logs/claude_transcript/...`,
`events/claude/common_transcript/...`, `claude_session_id_history`), instead of the old
`<local_host_dir>/plugin/mngr_claude/preserved_sessions/<agent-name>--<agent-id>/` location
with renamed subdirectories. This is a switch-forward change; previously preserved sessions in
the old location are left in place.

## 2026-06-08

Internal refactor (no behavior change): the `claude` agent's `assemble_command` now shell-quotes its extra `agent_args` via the shared `quote_agent_args` helper instead of an inline `shlex.quote` loop. This is the same quoting the plugin already performed; it now shares one implementation (and one explanatory comment) with `BaseAgent`, so the two cannot drift.

Standardized mngr_claude's test setup on `register_plugin_test_fixtures(globals())`
for HOME isolation (matching every other mngr plugin), keeping
`pytest_plugins = ["imbue.mngr_modal.conftest"]` only to share mngr_modal's test
fixtures (`modal_subprocess_env`, etc.). Removed the now-redundant
`enabled_plugins` override. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-06

Added approximate response streaming for Claude agents, driven by watching the agent's tmux pane.

- New `streaming_snapshot_interval_seconds` (float, default `0.0`) on the `claude` agent type config. When `> 0`, a background watcher polls the agent's tmux pane every N seconds and writes the in-progress assistant text to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`. When `<= 0` (the default) the watcher is neither provisioned nor run, so existing behavior is unchanged.
- `stream_buffer` format: line 1 is the `uuid` of the last *complete* assistant message (empty string if none yet, read from `logs/claude_transcript/events.jsonl`), and lines 2+ are the in-progress assistant text reverse-mapped from the terminal rendering back into markdown. It is written atomically (temp file + `mv`), cleared on watcher startup, and emptied (body cleared, id line kept) when the agent goes idle.
- New self-contained watcher script `resources/stream_snapshot.py` (stdlib-only, like `sync_keychain_credentials.py`). It captures the pane with `tmux capture-pane -e -J` (ANSI codes preserved, soft-wraps rejoined), identifies the latest assistant-text block by the `●` marker's color (assistant markers are the achromatic default text color; chromatic tool-call and mid-gray status markers are ignored), and reverse-maps bold/italic, inline code, links (OSC 8 hyperlinks), blockquotes, lists, code blocks, and tables (box-drawing back to pipe syntax). The pure parsing functions are unit-tested directly against real captured fixtures.
- The body is strict-append within a message: successive visible-pane snapshots are overlap-stitched (longest suffix/prefix line match) so the full message is reconstructed as it scrolls; a stale shorter snapshot that is a prefix of the accumulated body is ignored, and a genuinely non-overlapping snapshot resets to a new message. A trailing table is held back until its raw form is stable across polls, then rendered, and each successive render is a superset of the previous one so the rendered body never shrinks or duplicates.
- The poll interval is provisioned to a per-agent file (`plugin/claude/stream_interval`) that the watcher reads at runtime, rather than relying on env-var propagation into the background-tasks subshell. `claude_background_tasks.sh` launches and restarts the watcher whenever the script is present (the provision-time presence check is the single gate, like the common-transcript converter). Provisioning fails fast if streaming is enabled but the host lacks `python3`.
- Added `ClaudeAgent.get_stream_buffer_path()` so other code (e.g. `mngr robinhood`) can locate and read the buffer.

Not in scope: perfect markdown fidelity, heading-level or code-block-language recovery, reasoning/"thinking" block streaming, streaming for non-claude agent types, and any new top-level `mngr` CLI command to read the buffer.

## 2026-06-05

Internal refactor (no behavior change): `ClaudeAgent._find_git_source_path` now delegates to the shared core helper `imbue.mngr.utils.git_utils.find_git_source_path` instead of inlining the `find_git_common_dir` + parent logic, which was duplicated in the `antigravity` plugin.

Updated changelog references following the `mngr_uncapped_claude` plugin rename:
mentions of the `mngr uncapped-claude` command in this project's changelog now
read `mngr robinhood`. No code changes.

## 2026-06-04

Replaced the module-local `_get_local_host` helper with the shared `get_local_host` from `imbue.mngr.api.providers` (deduplication; no behavior change).

## 2026-06-02

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

Fixed `--adopt-session` rejecting valid Claude agent subtypes. It now accepts any agent type that resolves to a Claude agent (including config-defined templates like `write-plus` whose `parent_type` chain reaches `claude`), instead of only the literal `claude` type name. The check routes through the centralized `resolve_agent_type` registry rather than a string comparison.

# Simplify `--adopt-session` agent-type validation

- Now that `CreateAgentOptions.agent_type` is always set (it became a
  required field), the `--adopt-session` `on_before_create` validation no
  longer special-cases an unset type: it simply requires the agent type
  to be `claude`. No behavior change for users, since the CLI already
  requires a concrete agent type.

Updated the `on_before_create` hook implementation (used for `--adopt-session` validation) to accept the new `mngr_ctx` parameter now passed by mngr.

## 2026-05-28

# Adopt-session test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The `mngr_claude`
adopt-session tests hand-roll a trusted-subprocess profile and load it, so the
`trusted_subprocess_env` fixture now writes `is_allowed_in_pytest = true` into
that profile's settings.local.toml. Test-only change; no user-facing behavior
change.

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

- `ClaudeAgentConfig.merge_with` follows mngr's new assign-by-default semantics: an override's `cli_args` replaces the base's (rather than concatenating). To opt back into additive layering, use the `__extend` operator with an explicit list value, e.g. `cli_args__extend = ["--verbose"]`; the string-shorthand form that the bare `cli_args` field accepts (which the validator splits via shlex) is not accepted by the `__extend` resolver. See the `mngr` changelog entry for the full breaking-change writeup.

Update Claude plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` and `_preflight_send_message` now
take `tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `claude_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit
when a Claude agent's session was killed but a sibling session whose name
shares this name as a prefix was still alive, leaking the transcript
streamer and common-transcript converter for stopped agents.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

`resolve_shared_claude_config_dir()` (used when a claude agent opts into `use_env_config_dir=True`) now falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset, instead of raising. The fallback matches claude's own default, so callers can treat that flag as a pure "don't touch the config dir" knob even on machines where the user never sets `CLAUDE_CONFIG_DIR`. Also drops `ORIGINAL_CLAUDE_CONFIG_DIR` from the agent env in the `mngr robinhood` flow so credential sync reads from the live `$CLAUDE_CONFIG_DIR` (matters when robinhood is invoked from inside another mngr claude agent).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`ClaudeAgent` now satisfies the new `HasTranscriptMixin` and
`HasCommonTranscriptMixin` mixins on `AgentInterface` (introduced to give every
agent type a shared transcript-capture contract). The user-visible behavior of
`mngr transcript <claude-agent>` is unchanged.

## 2026-05-14

- Fixed: a cloned claude agent now actually resumes the source agent's conversation (the model sees and acts on the source's history), not just inherits the session JSONL on disk. Previously, after #1598's cross-host plugin/ rsync, claude on the destination would still start fresh because the JSONL was filed under the *source's* encoded work_dir, the rsynced ``sessions-index.json`` pointed at source paths, and ``claude_session_id`` was wrong. ``_adopt_cloned_session`` now renames the project subdir to the destination's realpath-resolved encoding (handles the ``/mngr/projects/agent-X`` → ``/__modal/volumes/<vol-id>/projects/agent-X`` symlink on Modal), drops the stale index, writes ``claude_session_id`` to the JSONL filename's stem (the ground truth — the source's own ``claude_session_id`` file holds the agent UUID from the SessionStart hook default rather than the real id), and carries forward ``claude_session_id_history``. The ``--adopt-session`` flow shares the same finalize step.

Add a `use_env_config_dir` option on the `claude` agent type config. When set
to `true`, local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead of
provisioning a per-agent config dir, and mngr does not write to the user's
Claude config (no trust additions, dialog dismissal, per-agent settings, or
keychain provisioning). Only supported for local hosts; `$CLAUDE_CONFIG_DIR`
must be set. The user is responsible for one-time interactive `claude` setup.
See `libs/mngr_claude/README.md` for details.

## 2026-05-06

- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
