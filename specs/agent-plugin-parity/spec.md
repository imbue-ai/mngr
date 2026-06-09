# Agent-plugin feature parity

A reference for bringing a new agent-type plugin (opencode, codex, pi-coding, or any
future CLI) up to the level of the two mature plugins, `mngr_claude` and
`mngr_antigravity` (`agy`). It enumerates every capability those two implement, how each
is implemented, and -- for each dimension -- the concrete questions a new plugin author
must answer about their target CLI.

The goal: **anything you can do with a Claude agent, you should be able to do with any
other agent.** Today `mngr_claude` is the gold standard and `mngr_antigravity` is a
recent, near-complete port; `codex`, `opencode`, and `pi-coding` are stubs or partial
implementations. This doc maps the target so the stubs can be brought up to scratch.

It is descriptive, not prescriptive about future architecture: it documents what the two
reference plugins *do*, with file:line citations, plus the gotchas they hit so the next
port does not rediscover them.

## Contents

- [How the plugin system works](#how-the-plugin-system-works) -- the contract, base classes, free vs per-plugin
- [Current state matrix](#current-state-matrix) -- what each plugin has today
- [Parity dimensions](#parity-dimensions) -- the feature-by-feature map (the bulk of this doc)
- [Recommended bring-up sequence](#recommended-bring-up-sequence) -- the order antigravity did it in
- [New-CLI investigation checklist](#new-cli-investigation-checklist) -- questions to answer before writing code

---

## How the plugin system works

A mngr agent-type plugin is a Python package exposing a `register_agent_type` hookimpl
(pluggy, `[project.entry-points.mngr]` in `pyproject.toml`). The hook returns a triple
`(name, AgentClass, ConfigClass)`:

- **name** -- the string used by `mngr create <agent> <name>` (e.g. `"claude"`, `"antigravity"`).
- **AgentClass** -- an `AgentInterface` implementation. You almost never subclass the bare
  interface; you subclass one of the concrete bases below. Returning `BaseAgent` directly
  gives you a config-driven shell command with no custom behavior (this is what the
  `codex`/`opencode`/`command` stubs do).
- **ConfigClass** -- a subclass of `AgentTypeConfig` (`libs/mngr/imbue/mngr/config/data_types.py:366`)
  declaring the agent's tunables. `None` falls back to the base `AgentTypeConfig`.

Registration flow: `load_agents_from_plugins` (`libs/mngr/imbue/mngr/agents/agent_registry.py:38`)
calls the hook and registers the class and config in two parallel registries. Built-in
non-claude types (`codex`, `command`, `headless_command`) are registered directly in
core; claude/antigravity/opencode/pi come from installed plugin packages.

The full hookspec surface lives in `libs/mngr/imbue/mngr/plugins/hookspecs.py`. Most
agent behavior is *not* dispatched through pluggy hooks -- it lives on **AgentInterface
methods** the host calls directly during create/start/stop/destroy. The pluggy hooks a
plugin may *also* implement (and which `mngr_claude` uses) are: `register_cli_options`,
`on_before_create`, `get_files_for_deploy`, `modify_env_vars_for_deploy`,
`agent_field_generators`, `offline_agent_field_generators`, and `on_before_host_destroy`.

### Base classes

| Base | Adds over parent | Use for |
|---|---|---|
| `BaseAgent` (`agents/base_agent.py:63`) | Everything host-filesystem-backed: state persistence, lifecycle detection, tmux send/capture, env/plugin/activity files, the create/destroy plumbing | A bare config-driven command |
| `InteractiveTuiAgent` (`agents/tui_agent.py:26`) | TUI readiness banner poll (`TUI_READY_INDICATOR`), paste-aware `send_message`, abstract `_send_enter_and_validate` | An interactive TUI agent (claude, agy, pi) |
| `BaseHeadlessAgent` / `StreamingHeadlessAgentMixin` | `output()` / `stream_output()` / `stage_initial_message()` | A non-interactive `--print`-style agent |

Capability mixins (opt in by inheritance): `HasTranscriptMixin`,
`HasCommonTranscriptMixin`, `HeadlessAgentMixin`, `StreamingHeadlessAgentMixin`
(`interfaces/agent.py:446-580`).

### Free vs per-plugin

**Free from `BaseAgent` / `InteractiveTuiAgent`** (inherit and forget):

- Lifecycle-state detection (tmux pane poll + `ps` + the `active` marker file ->
  `determine_lifecycle_state`), `is_running`, `runtime_seconds`.
- tmux interaction: `send_message` (literal keys + paste detection in the TUI variant),
  `capture_pane_content`, the message lock.
- All persistence under the agent state dir: certified `data.json` (command, labels, env,
  plugin data, messages), reported `status/` and `activity/` files.
- The whole `create` / start / stop / destroy orchestration, host locking, work-dir
  creation, provisioning sequencing, worktree cleanup, discovery events.
- `mngr transcript` rendering -- *given the common JSONL exists*.
- Config layering/merging, CLI parsing, `cli_args` splitting, the base `assemble_command`
  (command -> cli_args -> shell-quoted agent_args).

**Must be supplied per-plugin** (this is the parity work):

- A `ConfigClass` with the agent's tunables.
- `assemble_command` override to bake in HOME/auth isolation, resume logic, backgrounded
  helper launches, workspace setup.
- `get_expected_process_name()` if the binary's process name != command basename.
- **The `active` marker maintenance** -- the RUNNING/WAITING signal. Core only *reads*
  `$MNGR_AGENT_STATE_DIR/active`; nothing makes a plugin write it. **This is the
  highest-risk parity item: a plugin that never writes the marker reports WAITING
  forever, and one that clears it naively reports idle while a subagent works.**
- Provisioning: `provision()`, `get_provision_file_transfers()`,
  `on_before_provisioning()` / `on_after_provisioning()`, `preflight_check()`,
  `modify_env_vars()` as needed.
- Transcript scripts (raw + common) if implementing the mixins, plus launching them.
- For TUI agents: `TUI_READY_INDICATOR` + `_send_enter_and_validate`.
- `on_destroy()` cleanup of any external state (global trust entries, leases, etc.).
- The pluggy hooks for deploy, field generators, and offline preservation.

---

## Current state matrix

Y = implemented, partial = present but incomplete, - = absent.
(`codex` lives in mngr core at `agents/default_plugins/codex_agent.py`, not its own lib.)

| Dimension | claude | antigravity | pi-coding | opencode | codex |
|---|---|---|---|---|---|
| Custom agent class | Y | Y | Y (TUI) | - (BaseAgent) | - (BaseAgent) |
| Launch command isolation | Y | Y | partial | - | - |
| Lifecycle marker (RUNNING/WAITING) | Y | Y | - (inherited only) | - | - |
| Subagent-aware idle gating | Y (`SESSION_GUARD`) | Y (root_conversation + fullyIdle) | - | - | - |
| Readiness detection | Y (sentinel hook) | Y (TUI banner) | Y (TUI banner) | - | - |
| Auth / credential sharing | Y (keychain + file) | Y (token symlink) | Y (`sync_auth`) | - | - |
| HOME / config-dir isolation | Y (`CLAUDE_CONFIG_DIR`) | Y (per-agent `$HOME`) | Y (`PI_CODING_AGENT_DIR`) | - | - |
| Settings/resource sync | Y | Y | Y | - | - |
| Per-agent permissions | Y | Y | - | - | - |
| Auto-allow permissions | Y | Y | - | - | - |
| Trust / dialog handling | Y | Y | - | - | - |
| Onboarding NUX seed | Y | Y | - | - | - |
| Raw transcript | Y | Y | - | - | - |
| Common transcript | Y | Y | - | - | - |
| Conversation resume (stop/start) | Y | Y | - | - | - |
| Session preserve on destroy | Y (online + offline) | - | - | - | - |
| Streaming snapshot (live view) | Y | - | - | - | - |
| Deploy file/env contributions | Y | - | - | - | - |
| Field generators (waiting_reason) | Y (online) | - | - | - | - |
| Installation management | Y | - | Y | - | - |
| Extra agent subtypes | Y (guardian/fairy/headless) | - | - | - | - |

Notable observations:
- **`pi-coding` is the most advanced stub**: real TUI class, auth sync, HOME isolation,
  install management -- but no lifecycle marker, transcripts, resume, permissions, or
  trust handling.
- **`antigravity` is missing session-preservation-on-destroy, the streaming snapshot,
  deploy contributions, and field generators** relative to claude. These are the claude
  features no port has yet matched.
- **`codex`/`opencode` are pure `BaseAgent` shells**: they only get the free baseline.
  They will *appear* to work (`mngr create` succeeds, you can send messages) but will
  report WAITING forever, have no transcript, no resume, and no credential sharing.

---

## Parity dimensions

Each dimension below: **what it is**, **how claude does it**, **how antigravity does it**,
**gotchas**, and **the questions a new port must answer**. Implementation file:line
citations are to `libs/mngr_claude/imbue/mngr_claude/` and
`libs/mngr_antigravity/imbue/mngr_antigravity/` unless noted.

### A. Registration & agent class skeleton

The minimum to exist as an agent type. Both reference plugins subclass
`InteractiveTuiAgent` and a transcript mixin.

- **claude**: `ClaudeAgent(InteractiveTuiAgent, HasCommonTranscriptMixin)`; registers 5
  types (`claude`, `headless_claude`, `code-guardian`, `fixme-fairy`, plus the
  `SkillProvisionedAgent` base) -- `plugin.py:2719`.
- **antigravity**: `AntigravityAgent(InteractiveTuiAgent, HasCommonTranscriptMixin)`;
  registers 1 type -- `plugin.py:306`, `plugin.py:887`.

**Questions**: TUI or headless? One type or several (e.g. a skill-provisioned variant)?

### B. Launch command assembly (`assemble_command`)

The single string that `mngr start` replays on **every** start. Anything that must be
decided at launch time (resume id, HOME relocation) has to be **shell-evaluated inside
the command**, not computed in Python, because the stored command is replayed verbatim.

- **claude** (`plugin.py:1570`): backgrounds `claude_background_tasks.sh`; exports
  `MAIN_CLAUDE_SESSION_ID`; resume via a shell `find ... && claude --resume ... || claude
  --session-id ...` chain.
- **antigravity** (`plugin.py:787`): backgrounds the supervisor; `mkdir` log dir + `ln
  -sfn` workspace symlink; `cd` into the symlink; a `--conversation <id>` resume prelude
  read from `root_conversation`; `env HOME=<per-agent-home> agy ...`.

**Gotchas**:
- `BaseAgent.assemble_command` already `shlex.quote`s `agent_args` (post-`--`), but
  **list-form `cli_args` are not quoted** -- a model name like `"Gemini 3.5 Flash
  (Medium)"` passed as a raw `cli_args` list element will break the shell (PR #1927).
- Background helpers must be scoped with `&` so the *foreground* chain is the agent
  binary (otherwise readiness/liveness detection keys off the wrong process).

### C. Lifecycle / state detection (RUNNING vs WAITING)

The agent must report RUNNING while working and WAITING when idle. Core computes state
live (`base_agent.py:209` -> `determine_lifecycle_state`, `hosts/common.py:324`): tmux
pane alive + expected process name present -> `RUNNING if active-marker-present else
WAITING`. **The RUNNING/WAITING split is purely the presence of
`$MNGR_AGENT_STATE_DIR/active`.** Core never writes it; the plugin must.

- **claude** (`claude_config.py:515`): hooks write markers. `UserPromptSubmit` creates
  `active`; `Notification:idle_prompt` and `Stop` remove it. A separate
  `permissions_waiting` marker (created on `PermissionRequest`, removed on
  `PostToolUse`/`Stop`) lets `get_lifecycle_state` promote RUNNING->WAITING-on-permission
  (`plugin.py:1478`).
- **antigravity** (`antigravity_config.py:326`): `PreInvocation` -> `set_active_marker.sh`
  touches `active`; `Stop` -> `clear_active_marker_when_idle.sh` removes it.

**Questions**: Does the CLI emit pre-invocation / stop (or equivalent) hook events you can
attach scripts to? If not, you need another mechanism (TUI-state polling, a sentinel
file). Hook scripts must be POSIX `sh` and must not assume `jq` is present on remote hosts
(both reference plugins parse JSON payloads with `grep`/`sed`).

### D. Subagent-aware idle gating (the crux)

Naively, the failure mode is: when the agent spawns a subagent (or a nested invocation of
itself), the marker-touching events fire for that child too, and the child's "I'm done"
event clears the parent's `active` marker, so the parent flips to WAITING while still
working. **But whether this actually happens is entirely CLI-specific** -- it depends on
*which* of the CLI's lifecycle events fire for children vs only the root, and the two
reference plugins face genuinely different situations:

- **claude -- separate subagent event + a guard against nested processes.** Claude Code
  Task-tool subagents do **not** fire the `Stop` event; they fire a *distinct*
  `SubagentStop` event, which `mngr_claude` does **not** hook at all (it configures only
  `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `PostToolUse`, `Notification`,
  `Stop` -- `claude_config.py:555-665`). So Task subagents never touch the markers by
  construction. The `SESSION_GUARD = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '`
  prefix (`claude_config.py:512`) addresses a *different* case: a nested/recursive `claude`
  **process** that resumed a session (e.g. a code-guardian reviewer spawned as a
  subprocess) would fire the full `SessionStart`/`Stop`/... hook set against the same
  `$MNGR_AGENT_STATE_DIR`. The guard makes those firings exit early unless this is the
  main session (`MAIN_CLAUDE_SESSION_ID` is exported on mngr's launch line, `plugin.py:1615`).
  Separately, the `Stop` hook (`wait_for_stop_hook.sh`) *drains* concurrent sibling stop
  hooks (waits up to 120s, distinguishing process types via `/proc/<pid>/environ`:
  `CLAUDE_PROJECT_DIR` without `CLAUDECODE=1` = a stop hook; `CLAUDECODE=1` = a bash-tool
  task) before marking idle, so a still-running reviewer keeps the agent RUNNING.
- **antigravity -- root-conversation matching.** `agy` has **no** separate subagent-stop
  event: it runs the *same* `Stop` hook for the root conversation and every subagent
  conversation, and each subagent fires its own `"fullyIdle":true` Stop. So here the naive
  failure mode is real. The discriminator is the conversation id: `PreInvocation` records
  the turn's *root* conversation in `root_conversation` (only when the marker was absent --
  a new turn). `Stop` clears `active` **only** when the payload reports both
  `"fullyIdle":true` **and** a conversation id matching the recorded root. Subagent Stops
  carry a non-root id; interim Stops carry `fullyIdle:false`; either keeps the marker
  (`resources/set_active_marker.sh`, `resources/clear_active_marker_when_idle.sh`). A
  liveness fallback clears on `fullyIdle:true` if no root was ever recorded, so a failure
  to record the root can't strand the agent in RUNNING forever.

So the two plugins illustrate the spectrum: Claude's harness already isolates subagents
into a separate event class (mngr just declines to hook it) and only needs a guard for
recursive whole-process invocations; agy collapses everything onto one `Stop` event and
needs explicit root-vs-child id matching. **This is the single most important and
easiest-to-miss dimension** -- and you cannot assume either shape; you must check your CLI.

**Questions**: Which of the CLI's lifecycle events fire for Task-style subagents -- a
distinct event (like Claude's `SubagentStop`) or the same one as the root (like agy's
`Stop`)? What about a nested/recursive invocation of the CLI as a subprocess? Is there a
stable discriminator (a distinct event, an env var on the main process, a "fully idle"
flag, a root-vs-child conversation id)? How do you avoid both failure modes (parent goes
idle early; parent never goes idle)?

### E. Readiness detection

How `mngr create`/start knows the agent is ready to receive the first message.

- **claude**: a `SessionStart` hook writes a `session_started` marker, polled by
  `wait_for_ready_signal` (`plugin.py:1525`) -- a real sentinel.
- **antigravity**: no readiness sentinel exists (agy's hook events are execution-loop
  events with no "input prompt drawn" analog), so it falls back to the
  `InteractiveTuiAgent` banner poll on `TUI_READY_INDICATOR = "? for shortcuts"`
  (`plugin.py:323`). Note the splash banner ("Antigravity CLI ...") is deliberately *not*
  used -- it renders before OAuth completes and before the input row exists.

**Questions**: Is there a sentinel event for "input ready"? If not, what stable TUI string
appears only once the agent can actually accept input?

### F. Auth / credential sharing

Goal: log in once, have all agents authenticated, with minimal manual steps -- across
per-agent isolated config dirs.

- **claude** (`plugin.py:765-1133`): Claude hashes `CLAUDE_CONFIG_DIR` into keychain
  labels, so a per-agent dir wouldn't find default-label creds. On **macOS**, mngr copies
  the user's keychain entries to per-agent-suffixed labels, and installs a
  `Notification:auth_success` hook running `sync_keychain_credentials.py` that propagates a
  fresh login to the default entry *and every other per-agent entry*. On **Linux**, it
  symlinks (or copies) `~/.claude/.credentials.json` into the per-agent dir. It also
  pre-approves any `ANTHROPIC_API_KEY` in env into `customApiKeyResponses.approved` so the
  agent never blocks on the custom-key dialog.
- **antigravity** (`plugin.py:548`): far simpler. The per-agent oauth token file is a
  **write-through symlink** to the shared `~/.gemini/.../antigravity-oauth-token`, created
  even when the shared token doesn't exist yet (dangling symlink). Because `agy` writes the
  token **in place** (not temp-file+rename), the first login writes through the symlink to
  the shared path, authenticating all agents and propagating refreshes. On macOS the
  keychain isn't reliably readable from a relocated `$HOME`, so agy falls back to the file
  token -- exactly the shared mechanism (a harmless "keychain cannot be found" popup
  appears on first login).

**Gotchas**: whether the CLI writes its token **in place** vs atomic-rename decides whether
a symlink-to-shared works (in-place: yes; rename: the symlink gets replaced by a regular
file and sharing breaks). The macOS-keychain headache is specifically a consequence of
**relocating `$HOME`** (see [config isolation](#g-config-dir--home-isolation)): a macOS
keychain can't be reliably read from a relocated home, so antigravity has to lean on a
shared *file* token instead. A CLI isolated via a config-dir env var (claude, pi) keeps the
user's real `$HOME` and can still reach the system keychain (claude just has to mirror
entries to per-config-dir labels, since it hashes the config-dir path into the label) --
this is one more reason to prefer config-dir isolation over HOME relocation.

**Questions**: Where does the CLI store credentials (file? keychain? both)? Does it write
in place or atomically? Does it hash the config-dir path into the credential location
(like Claude's keychain labels)? What's the minimal "log in once" story, and what manual
steps remain?

### G. Config dir / HOME isolation

Each agent needs its own settings/permissions/transcripts, not the user's global state.
**Strongly prefer a config-dir override env var; relocating `$HOME` is a last resort.**
Pointing the CLI at a per-agent config dir (claude, pi) is surgical -- it isolates exactly
the agent's config and nothing else. Relocating `$HOME` (antigravity) is a blunt
instrument that drags *everything* an unscoped tool reads from `$HOME` into the per-agent
sandbox: credentials/keychains, caches, unrelated dotfiles. It exists only because `agy`
gives no finer-grained lever. Do it only if your CLI has no config-dir override.

- **claude** (`plugin.py:1436`) -- *preferred shape*: sets `CLAUDE_CONFIG_DIR` to
  `$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/` and `ORIGINAL_CLAUDE_CONFIG_DIR` to the
  user's real dir. Populates it by symlinking-or-copying `skills/agents/commands/plugins/`
  and rewriting plugin/marketplace install paths. A `use_env_config_dir` escape hatch
  shares the user's `$CLAUDE_CONFIG_DIR` (local-only, mngr writes nothing).
- **pi-coding** -- also preferred shape: sets `PI_CODING_AGENT_DIR` to a per-agent dir.
- **antigravity** (`plugin.py:513`) -- *the fallback*: `agy` has **no** config-dir env var
  and ignores per-workspace settings, so relocating `$HOME` to
  `<agent_state_dir>/plugin/antigravity/home/` (injected only on the agy process via
  `env HOME=...`) is the *only* lever for a per-agent `settings.json`. Per-agent
  `$HOME/.gemini` then holds settings/auth/hooks/sessions, and the fallout (no shared
  keychain, heavy caches re-downloaded) has to be patched back up by hand: the
  `ms-playwright-go` cache is symlinked to the user's real host cache, and auth falls back
  to a shared file token (see [Auth](#f-auth--credential-sharing)). This collateral cleanup
  is exactly the cost of HOME relocation and the reason to avoid it.

Both resolve the real host `$HOME`/OS over the host shell (not `Path.home()`/
`platform.system()`) so it works on remote hosts.

**Questions**: Does the CLI have a config-dir override env var? (Hope so -- use it.) If
*not*, can you relocate `$HOME`, and what collateral state (keychain, caches, dotfiles)
does that drag in that you'll have to re-share or re-seed? Where does it store global vs
per-project settings (you usually want to seed from the *global* scope only)?

### H. Permissions

Per-agent allow/deny/ask policy, plus an auto-approve-everything escape.

- **claude**: `auto_allow_permissions` adds a wildcard `PermissionRequest` hook emitting
  `{"decision":{"behavior":"allow"}}` (`claude_config.py:674`); unattended/remote agents
  get `skipDangerousModePermissionPrompt`/`bypassPermissionsModeAccepted` in settings.
- **antigravity**: a `permissions` block (`{allow,deny,ask}`, precedence Deny > Ask >
  Allow) inside `settings_overrides` flows into the per-agent `settings.json`;
  `auto_allow_permissions` appends agy's `--dangerously-skip-permissions` flag
  (`plugin.py:851`). Note: agy's `PreToolUse {"decision":"allow"}` hook does **not** gate
  the `run_command` confirmation dialog (verified live), so the flag -- not a hook -- is
  the only way to auto-approve.

**Questions**: Does a PreToolUse allow-decision actually suppress dialogs, or do you need a
skip-all flag? Is there a per-resource policy format?

### I. Trust / first-launch dialogs & onboarding NUX

CLIs gate first launch on a "trust this folder?" dialog and a one-time onboarding flow;
both must be handled so the agent isn't stuck at a prompt, **without silently running on
untrusted code**.

- **claude** (`plugin.py:1814`): trust/onboarding state lives in `~/.claude.json`; helpers
  add trust for the work dir, dismiss the effort callout, complete onboarding, etc. Gating:
  auto-approve mode -> silent; interactive -> per-dialog `click.confirm`; non-interactive
  without opt-in -> raise. A `_preflight_send_message` also scans the tmux pane for known
  dialog indicators before sending.
- **antigravity** (`plugin.py:629`): writes the **durable source-repo path** to the user's
  *global* `~/.gemini/.../settings.json` `trustedWorkspaces` (so re-trust isn't prompted
  across agents/worktrees) and the **transient per-agent workspace path** to the *per-agent*
  settings only. Gating matrix: already-trusted -> no-op; `--yes`/`auto_dismiss_dialogs` ->
  silent; interactive -> `click.confirm` (defaults False); non-interactive without opt-in or
  declined -> `SystemExit(1)` (clean exit). Onboarding NUX is skipped via a seeded
  `cache/onboarding.json` with all completion flags True (consumer + enterprise -- PR #2022).

**Gotchas**: trust dialogs are often keyed off an exact cwd match -- a symlinked or
relocated workspace path must be the one seeded. Don't write transient paths to shared
global state (they accumulate as dead entries). Hard-error (don't silently coerce) if the
trust list exists with an unexpected shape.

**Questions**: What gates the trust dialog, and is it keyed off an exact path? What gates
first-run onboarding -- a file or a settings key? Consumer vs enterprise onboarding?

### J. Transcript capture (raw + common)

`mngr transcript` reads a common, agent-agnostic JSONL, auto-discovered regardless of agent
type. Two layers, both opt-in via mixins (`interfaces/agent.py:446`):

- **Raw** (always provisioned): copy the CLI's native session JSONL verbatim to
  `$MNGR_AGENT_STATE_DIR/logs/<type>_transcript/events.jsonl`.
- **Common** (gated on `is_common_transcript_enabled`): convert to the shared envelope
  (`user_message`/`assistant_message`/`tool_result`) at
  `$MNGR_AGENT_STATE_DIR/events/<type>/common_transcript/events.jsonl`.

Both reference plugins provision streamer + converter shell scripts into `commands/` and
launch+supervise them from a backgrounded helper (`claude_background_tasks.sh` /
`antigravity_background_tasks.sh`), pidfile-deduped and restarted while the tmux session
lives. Shared helpers: `agents/common_transcript.py`.

**Gotchas**: scope the streamer to *this agent's* conversations (antigravity reads its
conversation-ids file). If the CLI's per-event index is conversation-scoped rather than
globally unique, you can't use the shared reconcile-offset helper -- dedupe by event id
downstream instead (antigravity's case).

**Questions**: Where does the CLI write transcripts, and in what schema? Are step/event
indices globally unique? Which conversations belong to this agent (root + subagents)?

### K. Conversation resume across stop/start

`mngr stop` then `mngr start` should resume the prior conversation with full context, not
start fresh. Automatic; no flag.

- **claude** (`plugin.py:1570`): `--resume "$MAIN_CLAUDE_SESSION_ID"` (read at runtime from
  a tracking file updated on `/clear`/compact), falling back to `--session-id <uuid>`.
- **antigravity** (`plugin.py:874`): a shell prelude reads the id from `root_conversation`
  and appends `--conversation <id>`. Crucially it uses the *root* conversation, not the
  last id in the conversation-ids file (which could be a subagent's).

**Gotchas**: the resume id must survive the hard kill that `mngr stop` performs (agy keeps
an incremental on-disk store; verify your CLI does too). Resume must be shell-evaluated in
`assemble_command` since the command is replayed.

**Known gap (both)**: *cloning* an agent does not yet carry the source's conversation
forward -- the clone path doesn't copy the source's session/conversation store into the new
agent or set its resume id. (The antigravity changelog attributes this to a "global"
conversation store, but that phrasing predates the per-agent `$HOME` work in the same PR
series: with HOME relocation, `ANTIGRAVITY_APP_DATA_DIR` -- where agy writes
`brain/<conv_id>/...` -- points into each agent's own home, so conversations are now
per-agent, not global. The real blocker is just that clone doesn't copy them across homes.)

### L. Session preservation on destroy

When an agent (or its host) is destroyed, its session/transcript files should be preserved
so they're not lost. **Only `mngr_claude` implements this; no port has matched it.**

- **claude** (`plugin.py:2333`, `2402`, `2612`, `2765`): `on_destroy` copies session JSONLs,
  raw + common transcripts, and session-id history to
  `<local_host_dir>/plugin/mngr_claude/preserved_sessions/<name>--<id>/` *before* the state
  dir is deleted (pulling remote files local via rsync). A separate
  `on_before_host_destroy` hookimpl handles the offline case (host destroyed without
  `on_destroy`) by reading session files directly off the host volume via the Volume API.
  Config: `preserve_sessions_on_destroy` (default True).

**Questions for a port**: Where do session files live? Do you need both an online
(`on_destroy`) and offline (`on_before_host_destroy`) path? (Claude does -- consider whether
your agent needs the offline path or if online suffices.)

### M. Streaming snapshot (live in-progress view)

An approximate live view of the agent's in-progress assistant text, for UIs that want to
show output before a message completes. **claude-only.**

- **claude** (`plugin.py:1112`, `resources/stream_snapshot.py`): when
  `streaming_snapshot_interval_seconds > 0`, a background watcher periodically
  `tmux capture-pane`s, reverse-maps the rendered text back to markdown, and writes
  `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer` (line 1 = id of last complete message;
  lines 2+ = in-progress text). Best-effort and approximate. Headless claude streams
  differently (`stream_output` tails `stdout.jsonl`).

A port only needs this if a consuming UI wants live streaming. It's the lowest-priority
parity item.

### N. Deploy / scheduling contributions

For `mngr schedule` (cloud/scheduled agents), the plugin bakes its files and env vars into
the deployment image. **claude-only**; antigravity is *not* wired into scheduled deploys.

- **claude** (`plugin.py:2838`, `2933`): `get_files_for_deploy` ships generated
  `settings.json`/`.claude.json` (+ creds/skills/plugins when user-settings are included,
  with plugin paths rewritten to a sentinel and resolved at runtime);
  `modify_env_vars_for_deploy` injects `ANTHROPIC_API_KEY` and `IS_SANDBOX=1`.

**Questions**: Does the agent need to run under `mngr schedule`? If so, which config/cred
files and env vars must be present in the remote image?

### O. Field generators (listing columns)

Extra plugin-namespaced fields surfaced in `mngr list`, online and offline.

- **claude** (`plugin.py:2759`): `agent_field_generators` -> `waiting_reason`, reading the
  `permissions_waiting`/`active` markers without SSH/tmux to report `PERMISSIONS` vs
  `END_OF_TURN`. Claude does **not** implement `offline_agent_field_generators`.
- **antigravity**: implements neither, and explicitly **cannot surface a permission-WAITING
  reason** -- agy fires no hook while blocked at a permission dialog, so there's no signal.

Note: core has no first-class "WAITING reason" -- WAITING is binary (marker absent); the
`waiting_reason` field is a plugin-specific embellishment.

### P. Installation management & version pinning

Check the binary is present and optionally install/pin a version.

- **claude**: `check_installation` (default True) and `version` pinning (`plugin.py` config).
- **pi-coding**: `_check_pi_installed`/`_install_pi` via npm, gated by
  local-vs-remote/auto-approve.
- **antigravity**: none -- assumes `agy` is on PATH (and documents a PATH-shadowing caveat
  with the desktop app's bundled shim).

### Q. Workspace path quirks

Some CLIs reject mngr's dotted work-dir path (`~/.mngr/worktrees/...`).

- **antigravity**: `agy` refuses any path with a dot-prefixed segment as a workspace and
  silently falls back to `$HOME`. Workaround: a per-agent non-dotted symlink at
  `/tmp/mngr_antigravity_workspaces/<id>` -> real work_dir, recreated via `ln -sfn` on every
  launch, with `cd` into the symlink (`plugin.py:856`).
- **claude**: no such issue.

**Questions**: Does the CLI accept a dotted (`~/.mngr/...`) path as its cwd/workspace?

### R. Process name

`get_lifecycle_state` matches the running process against `get_expected_process_name()`,
which defaults to the command basename. Override if the binary's process name differs.

- **claude**: `"claude"`. **antigravity**: `"agy"` (`plugin.py:325`). **pi-coding**: `"pi"`.

### S. Extra agent subtypes

`mngr_claude` ships task-specialized subtypes that subclass the base agent and inject a
SKILL.md / different launch mode: `code-guardian`, `fixme-fairy` (both
`SkillProvisionedAgent`), and `headless_claude` (`claude --print`, streams from
`stdout.jsonl`). A port doesn't need these for baseline parity but the pattern
(`SkillProvisionedAgent`, `BaseHeadlessAgent`) is available to reuse.

### T. Test scaffold

A new plugin needs a project-level `conftest.py` calling `suppress_warnings()`,
`register_conftest_hooks(globals())`, and `register_plugin_test_fixtures(globals())` (the
last pulls in the autouse `setup_test_mngr_env` that redirects `$HOME` to a temp dir so
tests can't touch real `~/.mngr`/`~/.claude.json`/`~/.gemini`). If the plugin shares a
credential, add a package-level fixture that seeds it into the isolated `$HOME` (see
antigravity's `isolated_home`). Hook/converter shell scripts get their own `*_test.py`.
pyproject sets `--cov`, `fail_under = 95`, pyright `strict`.

---

## Recommended bring-up sequence

This is the order `mngr_antigravity` was built in (PR numbers in parens). Each stage built
on the last; replicating it for a new CLI is a sane default.

1. **Agent type + launch + readiness + transcript + trust/workspace** (#1719). The
   baseline: register the type, get `assemble_command` right (HOME/workspace handling),
   pick the TUI ready indicator, stream the raw + common transcript, dismiss the trust
   dialog. At this stage the agent works but has no `active` marker (always WAITING).
2. **Lifecycle marker** (#1815). Wire pre-invocation/stop hooks to maintain `active` so the
   agent reports RUNNING/WAITING. Verify the CLI's hooks actually *execute* (antigravity
   discovered an earlier "hooks don't run" belief was obsolete).
3. **Conversation resume** (#1854). Capture conversation ids from hooks; append a resume
   flag in `assemble_command`.
4. **Per-agent isolation** (#1889). The biggest step: relocate config-dir/`$HOME`, shared
   but overridable auth, per-agent permissions + model, trust split into durable-global vs
   transient-per-agent. Resolve host paths over the host shell for remote correctness.
5. **Subagent-aware idle gating** (#1931). Track the root conversation; clear `active` only
   on the root's fully-idle stop. This closes the "parent goes idle while subagent works"
   gap.
6. **Correctness fixes** (#1927 shell-quoting, #2022 enterprise onboarding). Point fixes
   layered on top.

Then the claude-only features not yet ported anywhere: **session preservation on destroy**,
**deploy/scheduling contributions**, **field generators (waiting_reason)**, and the
**streaming snapshot** -- in roughly that priority order.

### Known open gaps (carried by antigravity, and by definition by every newer port)

- Clone does not carry the source conversation forward.
- No permission-specific WAITING reason (depends on the CLI exposing a dialog/idle event).
- No readiness sentinel (banner-poll only) unless the CLI has an "input ready" event.
- `auto_allow_permissions` may require a skip-all flag rather than a hook decision.
- List-form `cli_args` with spaces/parens are not shell-quoted.

---

## New-CLI investigation checklist

Before writing code, answer these about the target CLI (the answers determine almost every
implementation choice above):

**Config & auth**
- [ ] Is there a config-dir override env var? If not, can `$HOME` be relocated?
- [ ] Where are credentials stored -- file, OS keychain, both?
- [ ] Does it write the token **in place** or via atomic rename? (Decides symlink-to-shared.)
- [ ] Does it hash the config-dir path into the credential location?
- [ ] Global vs per-project settings scopes -- which holds model/permissions/trust?
- [ ] Any heavy per-HOME caches worth sharing via symlink?

**Lifecycle & subagents**
- [ ] Does it emit pre-invocation / stop (or equivalent) hook events? Do hooks actually execute?
- [ ] Does it fire stop/idle events for subagents separately from the root?
- [ ] Is there a discriminator (env var on main process, fully-idle flag, root vs child id)?
- [ ] Is there an "input ready" sentinel event, or only a TUI banner?
- [ ] Which hooks file does it actually *execute* vs merely display? (agy had two paths.)

**Permissions & trust**
- [ ] Does a PreToolUse allow-decision suppress dialogs, or is a skip-all flag required?
- [ ] Is there a per-resource permission policy format?
- [ ] What gates the trust dialog, and is it an exact-cwd match?
- [ ] What gates first-run onboarding -- a file or settings key? Consumer vs enterprise?

**Sessions & transcripts**
- [ ] Where are transcripts written, and in what schema?
- [ ] Are event indices globally unique or conversation-scoped?
- [ ] Does it support resume-by-id, and survive a hard kill?
- [ ] Does it keep an incremental on-disk session store?

**Misc**
- [ ] What's the literal process name (for `get_expected_process_name`)?
- [ ] Does it accept a dotted (`~/.mngr/...`) workspace path as cwd?
- [ ] Is the binary on PATH, or does it need install/version management?
- [ ] Will the agent run under `mngr schedule` (needs deploy file/env contributions)?

---

## Source references

- Plugin contract / hookspecs: `libs/mngr/imbue/mngr/plugins/hookspecs.py`
- Base classes: `libs/mngr/imbue/mngr/agents/base_agent.py`, `agents/tui_agent.py`
- State machine: `libs/mngr/imbue/mngr/hosts/common.py` (`determine_lifecycle_state`),
  `primitives.py` (`AgentLifecycleState`)
- Transcript helpers: `libs/mngr/imbue/mngr/agents/common_transcript.py`
- Plugin docs: `libs/mngr/docs/concepts/plugins.md`, `docs/concepts/agent_types.md`,
  `docs/concepts/idle_detection.md`
- Reference plugins: `libs/mngr_claude/`, `libs/mngr_antigravity/` (the antigravity README
  is the single best worked example -- it documents each parity decision inline)
- Stubs: `libs/mngr_opencode/`, `libs/mngr_pi_coding/`,
  `libs/mngr/imbue/mngr/agents/default_plugins/codex_agent.py`
