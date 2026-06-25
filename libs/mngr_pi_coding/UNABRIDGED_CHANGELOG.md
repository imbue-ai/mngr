# Unabridged Changelog - mngr_pi_coding

Full, unedited changelog entries for the `mngr_pi_coding` project, consolidated nightly from individual files in `libs/mngr_pi_coding/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
