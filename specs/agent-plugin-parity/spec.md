# Agent-plugin feature parity

A reference for bringing a new agent-type plugin (opencode, codex, or any future CLI)
up to the level of the mature plugins -- `mngr_claude`, `mngr_antigravity` (`agy`), and
now `mngr_pi_coding`. It enumerates every capability they implement, how each is
implemented, and -- for each dimension -- the concrete questions a new plugin author
must answer about their target CLI.

The goal: **anything you can do with a Claude agent, you should be able to do with any
other agent.** Today `mngr_claude` is the gold standard; `mngr_antigravity`,
`mngr_pi_coding`, `mngr_opencode`, and now `mngr_codex` are near-complete ports. pi-coding
uses a single in-process extension rather than shell hooks; opencode is a client-server app
driven over an HTTP API, with an in-process TypeScript plugin loaded only into its server
process; codex is a third **shell-hooks** port (like claude and antigravity), the closest CLI
yet to Claude Code -- a Claude-style hook system, a config-dir override env var, file auth, and
resume-by-id (see dimension F and the transcript/lifecycle dimensions for how each shapes the
implementation). There are no longer any `BaseAgent` stubs among the named agent types.

It is descriptive, not prescriptive about future architecture: it documents what the
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
  `command`/`headless_command` stubs do).
- **ConfigClass** -- a subclass of `AgentTypeConfig` (`libs/mngr/imbue/mngr/config/data_types.py:366`)
  declaring the agent's tunables. `None` falls back to the base `AgentTypeConfig`.

Registration flow: `load_agents_from_plugins` (`libs/mngr/imbue/mngr/agents/agent_registry.py:38`)
calls the hook and registers the class and config in two parallel registries. Built-in
non-claude types (`command`, `headless_command`) are registered directly in core;
claude/antigravity/opencode/pi/codex come from installed plugin packages.

`mngr_opencode` is the structural odd one out: opencode is a client-server app, so the
plugin runs each agent as a headless `opencode serve` plus an `opencode attach` TUI client
(see [dimension F](#f-input-delivery--submission-confirmation) and the launch dimension),
delivers input over the server's HTTP API rather than tmux keystrokes, and maintains its
marker/transcripts from an in-process TypeScript plugin loaded only into the server.

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

### Your lever: shell hooks vs an in-process extension vs an HTTP server API

Before any of the dimensions below, answer one question: **what mechanism does the CLI give
you to run your code at its lifecycle points, and to feed it input?** It shapes every
dimension downstream.

- **Shell hooks** (claude, agy, codex): the CLI invokes shell scripts on events
  (`SessionStart`/`Stop`/`PreToolUse`/...). mngr provisions scripts into `commands/` and the
  CLI runs them. This is the model most of this doc assumes. codex's hook system is the closest
  of the three to Claude's (`UserPromptSubmit`/`Stop`/`SubagentStart`/`SubagentStop`,
  `codex_config.py:415`).
- **An in-process extension/plugin** (pi, opencode): the CLI has no shell hooks; instead it
  loads code *into its own process* (pi loads a TypeScript module via `pi -e`; opencode
  auto-loads `$OPENCODE_CONFIG_DIR/plugin/*.ts` whose `event` hook sees the event bus). mngr
  provisions one extension that handles the lifecycle dimensions at once -- marker, readiness,
  transcripts. Many modern agent CLIs are in this camp, so expect it.
- **An HTTP server API** (opencode): the CLI is a client-server app -- a server owns the
  sessions and an event bus; TUI/CLI/HTTP clients talk to it. mngr runs the agent as a
  headless server (`opencode serve`) plus a foreground TUI client (`opencode attach`), and
  drives it over HTTP: input goes by `POST /session/{id}/prompt_async` rather than tmux
  keystrokes, and the attached client renders the result so `mngr connect` stays fully
  visible. This is orthogonal to the lifecycle lever -- opencode *also* uses an in-process
  plugin for the marker/transcripts (loaded only into the server, role-gated) -- but it
  replaces the tmux-keystroke input path of dimension F entirely, which is why it is a
  first-class lever: structured input over HTTP sidesteps the keystroke-paste races that pi
  hit, and behaves identically on local and remote hosts.
- **Nothing** (the bare `command`/`headless_command` `BaseAgent` shells): no event surface at
  all, so you can only use what the tmux pane + process table afford -- which is why those shells
  report WAITING forever. No named agent type is in this camp any more.

If your lever is an in-process extension, it brings a hazard class the shell-hook plugins
don't have, learned the hard way on pi:
- **It runs inside the agent's event loop, so a bug there can crash the agent.** Wrap every
  handler so it can never throw into the CLI's loop -- *and remember async*: an unhandled
  promise rejection from an `await`ed/fire-and-forget call escapes a synchronous try/catch
  and, on Node's default `--unhandled-rejections=throw`, kills the process. Attach a `.catch`
  to every promise you don't await.
- **Module loading is fragile.** pi loads extensions via jiti; importing anything from the
  CLI's own package can hit bare-specifier resolution failures under global installs. Import
  only language built-ins plus locally-declared structural types; depend on nothing.
- **You emit, rather than tail.** With structured in-process events you build the transcript
  (and other artifacts) directly from event payloads -- no file to copy -- which is more
  robust but means *you* own the record's shape and its idempotency across restarts (e.g.
  seed an id counter from the existing line count so ids stay unique after a resume).

---

## Current state matrix

Y = implemented, partial = present but incomplete, - = absent (a gap), n/a = not applicable.

| Dimension | claude | antigravity | pi-coding | opencode | codex |
|---|---|---|---|---|---|
| Custom agent class | Y | Y | Y | Y | Y (TUI + common-transcript) |
| Launch command isolation | Y | Y | Y | Y (serve+attach launch script) | Y (`env CODEX_HOME=` + resume prelude) |
| Lifecycle marker (RUNNING/WAITING) | Y | Y | Y (extension marker) | Y (in-process plugin, server-only) | Y (4 hooks + recompute-under-lock) |
| Subagent-aware idle gating | Y (`SESSION_GUARD`) | Y (root_conversation + fullyIdle) | n/a (no in-process subagents) | Y (root-session gating, load-bearing) | Y (subagent start/stop hooks + per-subagent files) |
| Readiness detection | Y (sentinel hook) | Y (TUI banner) | Y (`session_start` sentinel) | Y (launch-script HTTP sentinel) | Y (TUI banner; `SessionStart` fires lazily) |
| Input delivery & submission | Y (tmux paste+Enter) | Y (tmux paste+Enter) | Y (extension injection) | Y (HTTP `prompt_async` POST) | Y (tmux paste+Enter) |
| Auth / credential sharing | Y (keychain + file) | Y (token symlink) | Y (`sync_auth`) | Y (`auth.json` symlink, `symlink_auth`) | Y (`auth.json` symlink + `file` store pin) |
| HOME / config-dir isolation | Y (`CLAUDE_CONFIG_DIR`) | Y (per-agent `$HOME`) | Y (`PI_CODING_AGENT_DIR`) | Y (`OPENCODE_CONFIG_DIR`+`XDG_DATA_HOME`) | Y (`CODEX_HOME`) |
| Settings/resource sync | Y | Y | Y | Y (`sync_global_config`) | Y (`config.toml` rewritten each provision) |
| Per-agent permissions | Y | Y | - | Y (`permission` via `config_overrides`) | Y (`sandbox_mode` + `approval_policy`/`config_overrides`) |
| Auto-allow permissions | Y | Y | - | Y (`auto_allow_permissions`) | Y (`approval_policy = "never"`) |
| Trust / dialog handling | Y | Y | Y (`trust.json` seed) | n/a (no trust dialog) | Y (`[projects."<path>"] trust_level` seed + `check_for_update_on_startup = false`) |
| Onboarding NUX seed | Y | Y | n/a (no NUX) | n/a (no NUX) | Y (`.personality_migration` + `[notice]` suppressors) |
| Raw transcript | Y | Y | Y | Y (in-process, raw-seeded) | Y (tail rollout JSONL) |
| Common transcript | Y | Y | Y | Y (in-process, rebuilt on idle) | Y (converter, derived from raw) |
| Ordered assistant parts[] | Y | Y (best-effort order) | Y | Y | Y (text-only) |
| Usage tracking plugin | Y (`mngr_claude_usage`) | - (deferred; no cost/token source) | Y (`mngr_pi_coding_usage`) | Y (`mngr_opencode_usage`) | Y (`mngr_codex_usage`) |
| Conversation resume (stop/start) | Y | Y | Y (`--session`) | Y (`attach --session`) | Y (`codex resume <id>`) |
| Session preserve on destroy | Y (online + offline) | - | - | - | - |
| Streaming snapshot (live view) | Y | - | - | - | - |
| Deploy file/env contributions | Y | - | - | - | - |
| Field generators (waiting_reason) | Y (online) | - (blocked: no event) | n/a (no prompt) | Y (online) | Y (online) |
| Installation management | Y | - | Y | - (no version pinning) | partial (mngr-side update notify + opt-in auto-update; no pinning) |
| Extra agent subtypes | Y (guardian/fairy/headless) | - | - | - | - (app-server variant deferred) |

Notable observations:
- **`pi-coding` is now a near-`antigravity`-parity port**, not a stub: lifecycle marker,
  readiness sentinel, raw + common transcripts, conversation resume, and trust handling, on
  top of the auth/HOME/install baseline it already had. (It needs no subagent-aware idle
  gating: pi has no in-process subagent tool, so only one agent loop runs per process.) It carries the same deferred tail as antigravity (session preservation,
  streaming snapshot, deploy contributions, field generators) plus per-agent permissions.
  Two things make it the structural outlier: its entire dynamic surface is **one
  in-process TypeScript extension** (pi has no shell hooks), and it **delivers input by
  injection** rather than tmux keystrokes, so it subclasses `BaseAgent` directly rather
  than `InteractiveTuiAgent` (see dimension F).
- **`opencode` is now a real port too, at roughly `antigravity` parity** -- no longer a
  stub: lifecycle marker, readiness sentinel, raw + common transcripts, conversation resume,
  per-agent isolation, shared auth, and per-agent/auto-allow permissions. Its distinguishing
  trait is **architecture**: opencode is a client-server app, so each agent runs as a
  headless `opencode serve` plus an `opencode attach` TUI client, mngr drives it over the
  server's **HTTP API** (input is a `prompt_async` POST, not tmux keystrokes), and the
  marker/transcripts are maintained by an in-process TypeScript plugin loaded *only into the
  server* (role-gated). Unlike pi, opencode has real in-turn subagents (the task tool spawns
  child sessions, each firing its own idle), so its root-session idle gating is
  **load-bearing**, not a no-op. It carries the same deferred tail as antigravity (session
  preservation, streaming snapshot, deploy contributions, field generators) plus version
  pinning / install management.
- **`codex` is now a real port too -- the third shell-hooks port, at roughly `claude` shape**
  -- no longer a `BaseAgent` shell: lifecycle marker, subagent-aware idle gating, readiness,
  raw + common transcripts, conversation resume, per-agent `CODEX_HOME` isolation, shared
  file auth, per-agent permissions/model, and consent-gated trust. It is the closest CLI to
  Claude Code (a Claude-style hook system, a config-dir override env var, file auth,
  resume-by-id), so `mngr_codex` follows the `mngr_claude` shape, borrowing only antigravity's
  banner-poll readiness fallback. Its distinguishing trait is **dimension D**: codex subagents
  run *asynchronously* (the root's `Stop` fires while subagents are still running, with no
  `fullyIdle` signal), so it needs a third, distinct gating shape -- dedicated
  `SubagentStart`/`SubagentStop` hooks tracking one file per in-flight subagent, with the
  marker recomputed under a lock from a root-turn flag plus that file set (see dimension D). It
  carries the same deferred tail as antigravity (session preservation, streaming snapshot,
  deploy contributions, field generators) plus version pinning / install management, and an
  app-server-backed second agent type is a documented follow-up.
- **All five named agent types are now real ports** (claude, antigravity, pi-coding, opencode,
  codex) -- there is no remaining pure stub. Three of them are shell-hooks ports (claude,
  antigravity, codex); pi is an in-process extension; opencode is client-server / HTTP-driven.
- **`antigravity` is missing session-preservation-on-destroy, the streaming snapshot,
  deploy contributions, and field generators** relative to claude. These are the claude
  features no port has yet matched.
- **`antigravity` is the one port with no usage-tracking plugin** -- claude, codex,
  opencode, and pi each ship a `mngr_<harness>_usage` provider, but `agy`'s statusline
  payload exposes no cost / token / rate-limit data, so there is nothing to write (deferred;
  see the [agent-usage-plugins spec](../agent-usage-plugins/spec.md)). Consequently the
  `mngr plugin install-wizard` offers a per-agent usage provider for every agent type except
  antigravity.

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
- **opencode**: `OpenCodeAgent(BaseAgent, HasCommonTranscriptMixin)` -- subclasses `BaseAgent`
  directly, *not* `InteractiveTuiAgent`, because input is delivered over HTTP rather than tmux
  keystrokes (like pi, for the same reason -- see dimension F); registers 1 type
  (`plugin.py:198`, `plugin.py:490`).
- **codex**: `CodexAgent(InteractiveTuiAgent[CodexAgentConfig], HasCommonTranscriptMixin)` --
  back to the claude/agy shape (a real TUI driven over tmux); registers 1 type
  (`plugin.py:219`, `plugin.py:580`).

**Questions**: TUI or headless? One type or several (e.g. a skill-provisioned variant)? Does
input come in over the terminal (subclass `InteractiveTuiAgent`) or a programmatic channel
(subclass `BaseAgent`, as pi and opencode do)?

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
- **opencode** (`plugin.py:438`): the command is `env <isolation + MNGR_OPENCODE_*> bash
  opencode_launch.sh <user-args>`. The env carries the config/data isolation
  (`OPENCODE_CONFIG_DIR`/`XDG_DATA_HOME`), the opencode bin, an *ephemeral* port (`--port 0`,
  so co-resident agents never collide), the URL-encoded workdir (encoded in Python via the
  stdlib so the script can drop it straight into the session-create `?directory=` query), and
  -- when `emit_common_transcript` is on -- `MNGR_OPENCODE_EMIT_COMMON=1`. The launch script
  (`resources/opencode_launch.sh`) is where the heavy lifting lives: it starts `opencode
  serve` (role-tagged `MNGR_OPENCODE_ROLE=server`, scoped to that one command), polls the
  server log for the actual bound port, creates *or reuses* the root session via HTTP, writes
  the port + root-session-id + readiness-sentinel files, then `attach`es the TUI client in the
  foreground. Resume across stop/start is handled inside the script (it reuses the recorded
  root session id), so there is no resume flag in the Python-assembled command.
- **codex** (`plugin.py:500`): `( bash codex_background_tasks.sh <session> ) &` backgrounds the
  transcript supervisor; then `mkdir -p <CODEX_HOME> && cd <work_dir> && { <reset-marker-state>;
  <resume-prelude>; env CODEX_HOME=<home> codex --dangerously-bypass-hook-trust "$@" <args>; }`.
  Three codex-specific pieces are shell-evaluated in the command: (1) a **reset** that `rm -rf`s
  stale lifecycle-marker state (`active`, `codex_root_active`, `codex_subagents/`, the lock dir)
  left by a SIGKILL-mid-turn `mngr stop`, since a resumed agent is idle until a new turn and the
  killed subagents' `SubagentStop` hooks will never arrive (`plugin.py:559`); (2) the **resume
  prelude** reading the root `session_id` from `codex_root_session` into `set -- resume "$id"`
  (empty -> fresh start, `plugin.py:553`); (3) `--dangerously-bypass-hook-trust` *before* the
  subcommand so it applies to both `resume` and fresh start. codex accepts the dotted
  `~/.mngr/...` cwd, so there is no workspace symlink (cf. antigravity).

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
- **opencode** (`resources/mngr_opencode_plugin.ts:291`): no shell hooks -- the in-process
  plugin's `event` hook watches the server event bus. `session.status` `busy`/`retry` touches
  `active`; `idle` removes it, but **only when the root session goes idle** (dimension D). The
  plugin runs *only in the server process* -- it returns early (inert) unless
  `MNGR_OPENCODE_ROLE=server`, which the launch script sets exclusively on the `serve`
  invocation, so the marker has exactly one writer even though the `attach` client loads the
  same plugin file.
- **codex** (`codex_config.py:415`, `resources/codex_marker_state.sh:83`): four shell hooks,
  but the marker is **not** a plain touch/remove -- it is *recomputed* from tracked state under
  a lock. `UserPromptSubmit` -> `set_active_marker.sh` touches a root-turn flag
  (`codex_root_active`); `Stop` -> `clear_active_marker.sh` removes it (for the recorded root
  session); `SubagentStart`/`SubagentStop` register/deregister one file per in-flight subagent.
  Every hook then calls `codex_marker_recompute`, which enforces the invariant **`active`
  exists iff (`codex_root_active` exists OR `codex_subagents/` is non-empty)**
  (`codex_marker_state.sh:84`). This async-subagent shape is dimension D; the recompute lives
  here because it is what writes the marker core reads.

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
- **opencode -- root-session matching, and the gating is load-bearing.** Unlike pi (which has
  no in-process subagents, so its idle gating is a no-op), opencode has real in-turn child
  sessions: the task tool spawns child sessions, and **each fires its own idle**, exactly the
  naive failure mode. So opencode must discriminate root from child, like agy. The
  discriminator is the session's `parentID`: the plugin learns `parentBySession` from
  `session.created`/`session.updated` events (which carry the full `Session`), and on a
  `session.status:idle` (or the deprecated `session.idle`) it clears `active` **only if the
  session is root** -- `parentID` undefined/empty (`resources/mngr_opencode_plugin.ts:180`,
  `:296`, `:304`). Child-session idles leave the marker alone, so task subagents keep the agent
  RUNNING until the whole turn finishes. Like agy's fallback, an as-yet-unseen session
  hierarchy is treated as root so an idle can still clear the marker rather than strand RUNNING.
- **codex -- a *third* shape: dedicated subagent start/stop hooks + a recompute-under-lock.**
  codex is unlike both of the above. Its Task-style subagents (the `spawn_agent` multi-agent
  feature) run **asynchronously** -- the `spawn_agent` tool returns immediately and the children
  run as independent threads -- so the root agent's `Stop` hook fires (root model loop done)
  **while subagents are still running**, their `SubagentStop` hooks arrive later with **no
  ordering guarantee**, and codex emits **no `fullyIdle`-style signal**. So neither claude's
  "decline to hook the subagent event" nor agy's "match a root-vs-child id on one shared event"
  works: codex *must* hook the subagent events, and it *cannot* read idleness off any single
  event. mngr therefore tracks two pieces of state and recomputes the marker from them on every
  hook (`resources/codex_marker_state.sh:83`):
  - a **root-turn flag** `codex_root_active`, touched by `UserPromptSubmit` ->
    `set_active_marker.sh` (`set_active_marker.sh:76`) and removed by `Stop` ->
    `clear_active_marker.sh` (`clear_active_marker.sh:67`);
  - **one empty file per in-flight subagent** under `codex_subagents/`, named by the subagent's
    `agent_id`: `SubagentStart` -> `subagent_started.sh` creates it (`subagent_started.sh:40`),
    `SubagentStop` -> `subagent_stopped.sh` removes it (`subagent_stopped.sh:36`).

  Every hook ends by calling `codex_marker_recompute`, which sets `active` present **iff**
  `codex_root_active` exists **or** `codex_subagents/` is non-empty (`codex_marker_state.sh:84`).
  So whichever of (the root `Stop`, the last `SubagentStop`) fires *last* is the one that
  actually clears the marker -- the unordered, asynchronous events all converge on the same
  invariant rather than racing on a touch/remove. Because four hooks (plus possibly several
  concurrent subagent hooks) mutate this state, each one takes a coarse **mkdir-based lock**
  (`codex_marker_lock`/`codex_marker_unlock`, atomic on POSIX) around its read-modify-recompute,
  with a stale-lock break (`find -mmin +1` -> steal) so a crashed hook can't strand the marker
  (`codex_marker_state.sh:55`). The recursive-process case is handled separately, like claude's
  `SESSION_GUARD`: `set_active_marker.sh` records the root `session_id` only when the marker is
  *absent* (a fresh root turn, `set_active_marker.sh:57`), and `clear_active_marker.sh` clears
  the root-turn flag only when the `Stop`'s `session_id` matches that recorded root
  (`clear_active_marker.sh:60`), so a nested/recursive `codex` sharing the same `CODEX_HOME`
  can't flip the working root to WAITING; a missing recorded root falls through to a liveness
  clear so it can't strand RUNNING forever. (codex has no `fullyIdle` flag and no
  conversation-id-on-`Stop` discriminator to lean on -- the tracked-state recompute *is* the
  discriminator.)

So these plugins illustrate the spectrum: Claude's harness already isolates subagents
into a separate event class (mngr just declines to hook it) and only needs a guard for
recursive whole-process invocations; agy collapses everything onto one `Stop` event and
needs explicit root-vs-child id matching; codex's subagents are *asynchronous* with no idle
signal at all, so it hooks the subagent start/stop events explicitly and recomputes the marker
from per-subagent state under a lock. **This is the single most important and
easiest-to-miss dimension** -- and you cannot assume any one shape; you must check your CLI.

**Questions**: Which of the CLI's lifecycle events fire for Task-style subagents -- a
distinct event (like Claude's `SubagentStop`) or the same one as the root (like agy's
`Stop`)? What about a nested/recursive invocation of the CLI as a subprocess? Is there a
stable discriminator (a distinct event, an env var on the main process, a "fully idle"
flag, a root-vs-child conversation id)? How do you avoid both failure modes (parent goes
idle early; parent never goes idle)?

**Related failure mode -- background / detached work.** The `active` marker tracks the
agent's *conversational turn* (the model loop), not arbitrary processes that turn spawned. A
*foreground* tool call keeps the marker present until it returns -- including an awaited
subagent tool (pi has no built-in subagents, but an optional third-party subagent extension,
if installed, runs each child as a *nested process* that blocks the turn while it runs). The
marker only flips to WAITING with work still in flight when that work is *detached* from the
turn: the agent runs `cmd &` / `nohup` / starts a daemon, **or** the CLI offers a structured
"run in background" tool that returns a handle *before* the task finishes (Claude Code's
`Bash` `run_in_background` is the canonical example; pi has none -- its bash tool is
synchronous, and pi's `usage.md` lists "background bash" among features it deliberately
omits). For the manual-`&` case, WAITING is *correct* (the agent genuinely is idle, awaiting
input); but a first-class background-task tool can make the agent report WAITING while a task
it launched is still running.

Note that even claude does **not** currently solve this for backgrounded bash. Its `Stop`
hook (`wait_for_stop_hook.sh`) waits before going idle, but only for sibling *stop-hook*
processes (the recursive-reviewer case of dimension D), which it distinguishes from
bash-tool tasks via `/proc/<pid>/environ`: a stop hook has `CLAUDE_PROJECT_DIR` *without*
`CLAUDECODE=1`, whereas a `CLAUDECODE=1` process is a bash-tool task and is **excluded** from
the wait. So a still-running reviewer keeps the agent RUNNING, but a detached
`run_in_background` bash does not -- claude's marker stays turn-scoped here too.

This is the right hook to extend if you *did* want background work to count: the
`CLAUDECODE=1` tag already present on bash-tool tasks is exactly the per-task discriminator
that makes a descendant-liveness wait *safe* -- it distinguishes "a background task the agent
started" from incidental children (the agent's shell, language servers, watchers). A CLI that
exposes backgrounded work but provides **no** such discriminator cannot do a generic
descendant-liveness check without false-RUNNING (the agent would never go idle once it
started any long-lived child).

Be careful to separate two things, because the CLIs' idle signals *do* correctly gate
**in-loop** pending work; it is only **detached** work they miss. agy's `fullyIdle` stays
`false` through a turn's interim Stops and the root-vs-subagent id match ignores a subagent's
own idle ([dimension D](#d-subagent-aware-idle-gating-the-crux)), so the marker clears only on
the root's final, everything-done Stop -- not mid-turn, and not when a subagent it launched
finishes. pi reaches the same outcome differently: its foreground tools block the turn (and
an optional subagent extension, if installed, runs its children as nested processes that
likewise block), so `agent_end` fires only once the turn is fully complete. codex reaches the
same outcome via its tracked-state recompute: an in-flight `spawn_agent` subagent keeps a file
under `codex_subagents/` until its `SubagentStop` arrives, so the marker stays present across
the asynchronous subagent even though the root `Stop` already fired -- but an OS process the
agent backgrounds itself gets no codex hook at all, so it is invisible (the codex README calls
this out explicitly). What *none* of the four reflects is a process the agent **detaches
from its loop entirely** (`cmd &` / `nohup` / a CLI `run_in_background` tool that returns
before its task finishes): that is loop/turn-scoped for claude, agy, pi, and codex alike. So the
honest fallback, absent a per-task tag to wait on, is to scope the marker to the agent's
turn/loop (using whatever "fully done" signal the CLI gives -- agy's `fullyIdle:true`, pi's
`agent_end`, codex's root-turn flag plus its subagent file set) and *document* that detached
work is not reflected.

**Questions**: Does the CLI have a `run_in_background`-style tool (one that returns before
its work finishes)? If so, what identifies those tasks (a process tag like `CLAUDECODE=1`, a
task registry, a completion event) so the marker can wait for them -- and is waiting even
desired, or is turn-scoped WAITING the right semantics for your supervisor?

### E. Readiness detection

How `mngr create`/start knows the agent is ready to receive the first message.

- **claude**: a `SessionStart` hook writes a `session_started` marker, polled by
  `wait_for_ready_signal` (`plugin.py:1525`) -- a real sentinel.
- **antigravity**: no readiness sentinel exists (agy's hook events are execution-loop
  events with no "input prompt drawn" analog), so it falls back to the
  `InteractiveTuiAgent` banner poll on `TUI_READY_INDICATOR = "? for shortcuts"`
  (`plugin.py:323`). Note the splash banner ("Antigravity CLI ...") is deliberately *not*
  used -- it renders before OAuth completes and before the input row exists.
- **pi-coding**: the lifecycle extension writes a `pi_session_started` sentinel from pi's
  `session_start` event, polled by `wait_for_ready_signal` -- a real sentinel, like claude.
  The `"pi v"` startup banner is deliberately *not* used.
- **opencode** (`plugin.py:214`, `resources/opencode_launch.sh:90`): the *launch script*
  writes an `opencode_ready` sentinel once the server is up **and** the root session exists --
  i.e. the agent can accept the HTTP-delivered first message -- and `wait_for_ready_signal`
  polls it (clearing any stale one at startup so it can't return early). A real sentinel, like
  claude/pi. This replaced an earlier approach that scraped the attach client's TUI footer
  (`"ctrl+p commands"`): the launch script owns the true "server + session up" fact, so a
  signal from it is more reliable than reading the banner.
- **codex** (`plugin.py:229`): no readiness sentinel -- codex's `SessionStart` hook fires
  *lazily* on the first prompt, not at TUI launch (openai/codex #15269), so there is no
  pre-input event to write a marker from. Like agy, it falls back to the `InteractiveTuiAgent`
  banner poll, here on `TUI_READY_INDICATOR = "/model to change"` (a stable substring of codex's
  header box, which renders together with the input composer, verified live against codex
  0.138.0). Unlike agy there is no OAuth splash delay -- auth is a file -- so the header box is a
  safe indicator that appears only with the rendered, ready composer, not before.

**The failure mode to avoid:** gating readiness on a string that prints *before* input is
accepted (a splash/version banner) makes `create` return too early, so the first message is
sent before the agent can process it and is silently lost. Both agy and pi hit a version of
this -- it is why agy waits on the shortcuts row, not the splash, and why pi waits on the
`session_start` sentinel, not the `"pi v"` banner. The banner is the tempting wrong answer
because it appears first.

**Questions**: Is there a sentinel event for "input ready"? If not, what stable TUI string
appears only once the agent can actually accept input -- *not* a splash/version banner that
prints earlier? When unsure, send a probe message and confirm it actually started a turn
(dimension F) rather than trusting the readiness string alone.

### F. Input delivery & submission confirmation

How mngr gets a message *into* the running agent, and how it confirms the turn
actually started. The base classes make this look free -- `BaseAgent.send_message`
types literal keys and `InteractiveTuiAgent` adds paste-detection -- but it is a
real per-CLI decision, and the "free" path is not always reliable.

- **claude / antigravity**: `InteractiveTuiAgent.send_message` pastes into the tmux
  pane and submits with Enter (`_send_enter_and_validate`). Works because both TUIs
  reliably accept a bracketed paste + Enter.
- **pi-coding**: tmux keystrokes proved unreliable -- pi intermittently swallowed
  the first Enter after a paste (most often on a fresh session), with no stable
  "input cleared" placeholder to confirm submission. So pi does **not** use the tmux
  path at all: it subclasses `BaseAgent` (not `InteractiveTuiAgent`) and overrides
  `send_message` to append the message to a per-agent inbox file, which the
  lifecycle extension injects into the live session via pi's `pi.sendUserMessage`
  API (the TUI stays attachable with `mngr connect`). If your CLI exposes a
  programmatic input channel -- an inject API, an RPC `prompt` command, a control
  socket -- prefer it over terminal keystroke simulation: it is more robust and
  behaves identically on local and remote hosts.
- **opencode** (`plugin.py:287`, `:330`): also bypasses the terminal -- but over **HTTP**
  rather than an in-process inject API. `send_message` reads the recorded server port + root
  session id (written by the launch script) and POSTs the message as a JSON text part to
  `http://127.0.0.1:{port}/session/{id}/prompt_async` via `curl -fsS` on the host; the
  attached TUI renders the prompt and reply, so `mngr connect` stays fully visible.
  `prompt_async` enqueues without blocking on the reply (the marker tracks completion). This
  was a deliberate move *away* from typing into the TUI, which races opencode's post-launch
  input repaint (it drops keys), exactly pi's hazard.
- **codex** (`plugin.py:235`): back to the tmux path -- `InteractiveTuiAgent.send_message`
  pastes and submits with Enter, and `_send_enter_and_validate` is a best-effort Enter
  (`send_enter_best_effort`) because upstream `wait_for_paste_visible` already confirmed the
  message landed in the pane. codex's composer reliably accepts a bracketed paste + Enter
  (unlike pi/opencode), so it did not need a programmatic input channel. (The codex README notes
  the cleaner `app-server` JSON-RPC input path as a deferred second-agent-type follow-up.)

**Submission confirmation is the subtle half.** Keystrokes (or an inject call) can
silently fail to start a turn, so you need a positive signal that the message *was*
accepted. The dependable one is the lifecycle marker: the agent writes `active`
(dimension C) only when it begins processing, so `send_message` can poll for the
marker to appear as confirmation -- re-sending (if using keystrokes) until it does.
Scraping the pane for an echoed prompt is brittle and racy; avoid it.

opencode is a case where this confirmation step was *deliberately skipped*: `curl -fsS`
already fails loudly if the POST is dropped or the server rejects it (the real, observed
failure mode), and an accepted-but-never-started turn is not a demonstrated failure here, so
`send_message` does **not** poll the marker for a turn-start ACK (`plugin.py:287`). The
documented decision is to revisit (poll the `active` marker, as pi does) only if a
silent-accept failure ever manifests -- a structured-input channel that fails loudly on the
POST itself buys you a weaker but cheaper guarantee than the keystroke path needs.

**Questions**: Does the CLI reliably accept tmux paste + Enter, or does it drop
keystrokes? Is there a programmatic input API (inject / RPC / socket) that bypasses
the terminal? What is the unambiguous signal that a submitted message actually
started a turn (a lifecycle event, the marker, a status file)? What happens for a
steering message sent while the agent is already mid-turn?

### G. Auth / credential sharing

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
- **opencode** (`plugin.py:416`): the same write-through-symlink shape as agy. The per-agent
  `auth.json` (under the agent's `XDG_DATA_HOME/opencode/`) symlinks to the shared
  `~/.local/share/opencode/auth.json`, created even when the shared file doesn't exist yet.
  opencode writes `auth.json` in place, so the first agent's `opencode auth login` writes
  through to the shared path and authenticates every agent (refreshes propagate too). Toggle:
  `symlink_auth` (default True); False copies the shared file in (full isolation, no sharing).
  Because opencode isolates via a *config-dir/XDG* scheme rather than HOME relocation, there is
  no macOS-keychain headache here -- credentials are a plain file.
- **codex** (`plugin.py:384`): the same write-through-symlink shape. The per-agent `auth.json`
  (under `CODEX_HOME`) symlinks to the shared `~/.codex/auth.json`, created even when the shared
  file doesn't exist yet (dangling symlink). codex writes `auth.json` **in place** (verified
  against source: `O_TRUNC`, no atomic rename) and reloads-before-refreshing, so the first
  agent's `codex login` writes *through* to the shared path and authenticates every agent
  (refreshes propagate; concurrent agents don't clobber). The load-bearing extra is a config
  pin: `config.toml` sets `cli_auth_credentials_store = "file"` (`codex_config.py:187`), because
  codex's `keyring`/`auto` backends hash `CODEX_HOME` into the secret key, which would give each
  per-agent home a *different* entry and defeat the symlink. Like opencode, config-dir isolation
  (not HOME relocation) means no macOS-keychain headache.

**Gotchas**: whether the CLI writes its token **in place** vs atomic-rename decides whether
a symlink-to-shared works (in-place: yes; rename: the symlink gets replaced by a regular
file and sharing breaks). The macOS-keychain headache is specifically a consequence of
**relocating `$HOME`** (see [config isolation](#h-config-dir--home-isolation)): a macOS
keychain can't be reliably read from a relocated home, so antigravity has to lean on a
shared *file* token instead. A CLI isolated via a config-dir env var (claude, pi) keeps the
user's real `$HOME` and can still reach the system keychain (claude just has to mirror
entries to per-config-dir labels, since it hashes the config-dir path into the label) --
this is one more reason to prefer config-dir isolation over HOME relocation.

**Questions**: Where does the CLI store credentials (file? keychain? both)? Does it write
in place or atomically? Does it hash the config-dir path into the credential location
(like Claude's keychain labels)? What's the minimal "log in once" story, and what manual
steps remain?

### H. Config dir / HOME isolation

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
- **opencode** (`opencode_config.py`, `plugin.py:474`) -- also preferred shape, via *two*
  env vars (no `$HOME` relocation): `OPENCODE_CONFIG_DIR` -> a per-agent config dir holding
  `opencode.json` and the auto-loaded `plugin/*.ts`; `XDG_DATA_HOME` -> a per-agent data root
  under which opencode keeps `opencode/{opencode.db,auth.json,storage,log}`, so sessions
  (hence resume) and credentials are per-agent. The two are independent -- `OPENCODE_CONFIG_DIR`
  moves only config, not data. Both are injected only on the opencode processes (inherited by
  `serve` and `attach`), so tmux keeps the real environment.
- **codex** (`codex_config.py:73`, `plugin.py:546`) -- also preferred shape: a single env var
  `CODEX_HOME` (default `~/.codex`) from which codex resolves its *entire*
  config/auth/session/hook tree, pointed at a per-agent dir under the agent state dir and
  injected only on the codex process (`env CODEX_HOME=...`). No `$HOME` relocation. The per-agent
  tree holds `config.toml`, `hooks.json`, the `auth.json` symlink, the `.personality_migration`
  NUX marker, and codex-owned `sessions/`; mngr rewrites its own files each provision and leaves
  `sessions/` intact. The user's real `CODEX_HOME` is resolved over the host shell
  (`${CODEX_HOME:-$HOME/.codex}`, `plugin.py:276`) so it is correct remotely.
- **antigravity** (`plugin.py:513`) -- *the fallback*: `agy` has **no** config-dir env var
  and ignores per-workspace settings, so relocating `$HOME` to
  `<agent_state_dir>/plugin/antigravity/home/` (injected only on the agy process via
  `env HOME=...`) is the *only* lever for a per-agent `settings.json`. Per-agent
  `$HOME/.gemini` then holds settings/auth/hooks/sessions, and the fallout (no shared
  keychain, heavy caches re-downloaded) has to be patched back up by hand: the
  `ms-playwright-go` cache is symlinked to the user's real host cache, and auth falls back
  to a shared file token (see [Auth](#g-auth--credential-sharing)). This collateral cleanup
  is exactly the cost of HOME relocation and the reason to avoid it.

Both resolve the real host `$HOME`/OS over the host shell (not `Path.home()`/
`platform.system()`) so it works on remote hosts.

**Questions**: Does the CLI have a config-dir override env var? (Hope so -- use it.) If
*not*, can you relocate `$HOME`, and what collateral state (keychain, caches, dotfiles)
does that drag in that you'll have to re-share or re-seed? Where does it store global vs
per-project settings (you usually want to seed from the *global* scope only)?

### I. Permissions

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
- **opencode** (`opencode_config.py:216`): config-based, no flag. opencode's `permission`
  block (e.g. `{"bash": {"git *": "allow", "rm -rf *": "deny"}, "edit": "ask"}`) is merged
  into the per-agent `opencode.json` via `config_overrides` (the free-form blob applied last);
  `auto_allow_permissions` injects a wildcard `{"*": "allow"}` permission block (auto-approve
  everything not explicitly denied -- the config analog of a skip-all flag). Because the
  policy lives in the file the server reads, opencode (like agy) supports a per-resource
  policy, and (unlike agy) needs no separate skip flag for the auto-allow case.
- **codex** (`codex_config.py:280`, `plugin.py:364`): config-based, no flag -- and the policy
  has two independent axes. `sandbox_mode` (`read-only|workspace-write|danger-full-access`,
  default `workspace-write`) governs filesystem/network isolation; `approval_policy` governs the
  interactive approval dialog. `auto_allow_permissions` sets `approval_policy = "never"`, which
  suppresses every approval prompt while *keeping the sandbox on* (the right unattended default).
  Unlike agy (whose hook allow-decision does not gate the dialog), codex honors `approval_policy`
  in `config.toml` directly, so no skip-all flag is needed; finer per-tool policy can be set via
  `config_overrides` (the free-form blob merged last).

**Questions**: Does a PreToolUse allow-decision actually suppress dialogs, or do you need a
skip-all flag? Is there a per-resource policy format?

### J. Trust / first-launch dialogs & onboarding NUX

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
- **pi-coding** (`plugin.py`): seeds pi's per-canonical-cwd `trust.json` (`{realpath: bool}`)
  -- the **durable** grant for the git *source repo* in the user's global
  `~/.pi/agent/trust.json`, the **transient** grant for the per-agent workspace in the
  per-agent dir. Same gating matrix as agy (already-trusted -> no-op;
  `--yes`/`auto_dismiss_dialogs` -> silent; interactive -> `click.confirm`;
  non-interactive without opt-in or declined -> `SystemExit`). pi has no onboarding NUX.
- **opencode**: nothing to seed -- opencode has **no** first-run trust dialog (verified
  live), so there is no trust state to write and no onboarding NUX to skip. The whole
  dimension is a no-op for it, which is itself a result worth recording: per the gotchas
  below, you must *confirm this empirically against the running binary* rather than assume.
- **codex** (`plugin.py:414`, `codex_config.py:318`): codex gates first launch on a project's
  `trust_level`. mngr seeds `[projects."<canonical-work-dir>"] trust_level = "trusted"` into the
  per-agent `config.toml` (the transient workspace) and persists the **durable** grant for the
  git *source repo* in the user's *global* `config.toml` (so re-trust isn't prompted across
  worktrees), with the same gating matrix as agy/pi (already-trusted -> no-op;
  `--yes`/`auto_dismiss_dialogs` -> silent; interactive -> `click.confirm` defaulting False;
  non-interactive without opt-in or declined -> `SystemExit(1)`). The path key is **canonical**
  (resolved over the host shell via `pwd -P`, `plugin.py:288`) because codex canonicalizes the
  cwd before its trust lookup. codex-specific twist: this single consent *also* covers the
  `--dangerously-bypass-hook-trust` flag the launch command passes (codex requires command hooks
  to be trusted before they run), because trusting the workspace likewise lets codex load any
  repo-local `.codex/hooks.json` unreviewed -- so the prompt names both effects, and mngr never
  bypasses codex's hook review without the user's say-so. Onboarding NUX: the empty
  `.personality_migration` marker skips codex's personality-migration prompt, and the `[notice]`
  suppressors (`hide_full_access_warning`/`hide_world_writable_warning`/`hide_rate_limit_model_nudge`,
  `codex_config.py:222`) silence the first-run migration notices. **Codex also has a launch-time
  *update* dialog -- a blocking "Update available! ... 1. Update now / 2. Skip / 3. Skip until next
  version" prompt codex shows on startup, including on `codex resume`** (`codex_config.py:204-212`),
  exactly the dimension-J "first-launch dialog that intercepts the first message" failure mode: it
  takes over the TUI composer, so mngr's first pasted message lands in the update menu instead of the
  input (an Enter could even select "Update now" and run `brew upgrade`) -- this manifested as `mngr
  message` timing out with "Timeout waiting for pasted content to appear" after a stop/start resume.
  The plugin suppresses it by pinning `check_for_update_on_startup = false` (`codex_config.py:212`,
  `codex_config.py:287-311` -- written *unconditionally* by `build_codex_config`) alongside the
  `[notice]` suppressor allow-list, into the per-agent `config.toml` rewritten on **every** provision
  (`plugin.py:402-416`) under the durable per-agent `CODEX_HOME`, so it survives stop/start and applies
  on `codex resume`. Documented limitation: the `[notice]` suppressors are a *hardcoded allow-list* of
  known keys (an unknown-to-this-version key is inert, `codex_config.py:218-226`), so a *future* new
  blocking prompt under a different notice/migration key could reappear until the allow-list is
  extended -- the update prompt, by contrast, is pinned off by its own dedicated key, not the
  allow-list. (mngr surfaces updates on its own, well-behaved side instead -- see dimension Q.)

**Gotchas**: trust dialogs are often keyed off an exact cwd match -- a symlinked or
relocated workspace path must be the one seeded. Don't write transient paths to shared
global state (they accumulate as dead entries). Hard-error (don't silently coerce) if the
trust list exists with an unexpected shape. **Verify empirically what actually *triggers* the
dialog** -- it is not always "the project has instructions". pi 0.79's dialog fires only on a
`.pi` config dir in the cwd, or a `.agents/skills` dir in the cwd or any ancestor;
`CLAUDE.md`/`AGENTS.md` do **not** trigger it (they are context files pi loads *once
trusted*). The trust boundary guards config/extension *loading* -- a repo silently
reconfiguring the agent or running its extension code -- **not** prompt injection from
context files, which pi treats as accepted, unpreventable local-agent risk. (A first cut of
the pi release test "added a CLAUDE.md to trigger the dialog" and so never triggered it,
making the trust coverage vacuous -- caught only by reading pi's source and testing the real
binary.)

**Questions**: What *exactly* triggers the trust dialog -- which files/dirs, cwd-only or any
ancestor -- and did you confirm it against the running binary rather than the docs? Is it
keyed off an exact (realpath) path? What gates first-run onboarding -- a file or a settings
key -- if the CLI has one at all? Consumer vs enterprise onboarding?

### K. Transcript capture (raw + common)

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
lives. Shared helpers: `agents/common_transcript.py`. In both, the **common layer is
*derived* from the raw stream** (the converter reads the raw JSONL), which is why the raw
layer is foundational and always-on.

The common envelope's field vocabulary tracks the OpenTelemetry GenAI semantic conventions
(e.g. `finish_reason`, not a bespoke name); the canonical schema is
`agents/common_transcript_records.py`. Every assistant record carries an ordered `parts[]`
(text/tool_call segments, modelled on the OTel message `parts`) -- the agent-agnostic view the
reader renders -- with a `parts_ordered` flag. The order is faithful for claude, pi-coding,
opencode (all iterate their native ordered content) and trivially so for codex (text-only
assistant messages); only antigravity is best-effort (`parts_ordered=False`), because its native
format does not record where tool calls sat relative to the text. See
[`../common-transcript-standard/spec.md`](../common-transcript-standard/spec.md).

**pi-coding emits the two layers *independently*, not derived.** pi has no convenient
always-current flat session file to tail (its native store is tree-structured JSONL), so the
lifecycle extension writes both layers directly from pi's structured `message_end` events --
no backgrounded streamer, no separate converter. Two consequences for any structured-event
CLI: (1) because the common record is emitted independently rather than converted from raw,
the two can be gated separately (pi exposes `emit_raw_transcript` *and*
`emit_common_transcript`, where claude/agy only toggle common); and (2) the "raw" layer is
then something *you* assemble rather than copy -- pi wraps each native message verbatim in a
thin `{type, timestamp, message}` envelope, so it stays lossless and CLI-native, just
collected by the extension instead of tailed from a file.

**opencode also emits both layers in-process, but with a restart-survival gotcha worth
documenting.** Like pi, opencode has no flat session file to tail, so the same in-process
TypeScript plugin writes both -- no shell converter, no background supervisor
(`get_raw_transcript_scripts`/`get_common_transcript_scripts` return `{}`,
`plugin.py:249`/`:262`). The two layers are produced *differently*, though:
- **Raw** is *append-only*: each `message.updated`/`message.part.updated` event is appended
  verbatim (as `{type, properties}`) to `logs/opencode_transcript/events.jsonl`
  (`resources/mngr_opencode_plugin.ts:311`).
- **Common** is *rebuilt wholesale on idle*: the plugin keeps the latest message/part state in
  memory and, on root-session idle, rebuilds the entire common transcript from that state and
  writes it atomically (tmp + rename) (`mngr_opencode_plugin.ts:264`, `:299`). Rebuilding from
  full state once per turn is self-healing -- no message-completion detection, no streamer --
  and the live in-progress view is just the tmux pane.

**The gotcha (dimension K, opencode-specific): a wholesale rebuild from in-memory state must
seed that state from the persisted raw log, or a restart truncates pre-restart turns.** A
`mngr stop`/`start` gives a *fresh* `opencode serve` process with empty in-memory maps, and
opencode does **not** replay history through the plugin on `attach --session` resume
(verified). Since the common rebuild does a full atomic *overwrite*, the first post-restart
idle would otherwise clobber the common transcript down to only the new turn -- an asymmetric
loss, since the append-only raw log survives. The fix: at plugin startup the in-memory state
is **seeded by replaying the persisted append-only raw transcript**
(`mngr_opencode_plugin.ts:132`), which is idempotent with later live updates (keyed by id),
so the first rebuild reflects full history. The general lesson for any rebuild-on-idle scheme:
your overwrite is only as complete as the state you rebuild from, so seed it from the durable
append-only log on every (re)start.

**codex is back in the claude/agy "derive common from raw" camp**, because codex *does* keep a
convenient append-as-you-go session file. codex writes one rollout JSONL per session under
`$CODEX_HOME/sessions/.../rollout-*.jsonl` and hands its absolute path to every hook as
`transcript_path`; `set_active_marker.sh` records that path at each turn boundary in
`codex_transcript_path` (`set_active_marker.sh:66`). Two backgrounded shell scripts, supervised
by `codex_background_tasks.sh` (pidfile-deduped, restart-on-death, like claude/agy):
- **Raw** (always on): `stream_transcript.sh` re-reads the recorded path each cycle (it can
  change across `codex resume`) and tails it, appending new lines **verbatim** to
  `logs/codex_transcript/events.jsonl`, with a per-rollout offset file so it resumes after a
  restart (`stream_transcript.sh:97`).
- **Common** (gated on `emit_common_transcript`): `common_transcript.sh` reads the raw stream
  and converts `response_item` rows into the shared envelope -- `message`/user -> `user_message`,
  `message`/assistant -> `assistant_message`, `function_call` + `function_call_output` paired by
  `call_id` -> `tool_result` -- with `source = "codex/common_transcript"`
  (`common_transcript.sh:70`). It deliberately ignores `event_msg` display duplicates and
  bookkeeping rows. Since the rollout carries no global per-line id, event ids are synthesized
  from the line's 1-based index (`line-<n>-user` etc.), and the converter dedupes against the
  ids already in the output, so re-processing is idempotent across restarts.

**Gotchas**: scope the streamer to *this agent's* conversations (antigravity reads its
conversation-ids file). If the CLI's per-event index is conversation-scoped rather than
globally unique, you can't use the shared reconcile-offset helper -- dedupe by event id
downstream instead (antigravity's case).

**Questions**: Where does the CLI write transcripts, and in what schema? Are step/event
indices globally unique? Which conversations belong to this agent (root + subagents)?

### L. Conversation resume across stop/start

`mngr stop` then `mngr start` should resume the prior conversation with full context, not
start fresh. Automatic; no flag.

- **claude** (`plugin.py:1570`): `--resume "$MAIN_CLAUDE_SESSION_ID"` (read at runtime from
  a tracking file updated on `/clear`/compact), falling back to `--session-id <uuid>`.
- **antigravity** (`plugin.py:874`): a shell prelude reads the id from `root_conversation`
  and appends `--conversation <id>`. Crucially it uses the *root* conversation, not the
  last id in the conversation-ids file (which could be a subagent's).
- **opencode** (`resources/opencode_launch.sh:74`): the launch script records the root
  session id on first launch (it creates the session via HTTP and writes the id to
  `opencode_root_session`) and reuses it on every restart -- reading it back and re-attaching
  with `attach --session <id>`, while messages POST to that same id. So resume is handled
  inside the script, not via a flag computed in Python (the per-agent `XDG_DATA_HOME` SQLite
  store survives the stop, carrying the conversation).
- **codex** (`plugin.py:548`): the `UserPromptSubmit` hook records the *root* `session_id` in
  `codex_root_session` (codex assigns the id at session start, and there is no `--session-id` pin
  at fresh launch); `assemble_command`'s resume prelude reads it and shell-evaluates `set --
  resume "$id"` so a restart runs `codex resume <id>` (empty -> fresh start). Like agy it uses
  the *root* session, not a subagent's. codex's rollout JSONL is append-and-flush per line, so it
  survives the SIGKILL `mngr stop` performs and `codex resume` reconstructs history from it.

**Gotchas**: the resume id must survive the hard kill that `mngr stop` performs (agy keeps
an incremental on-disk store; verify your CLI does too). Resume must be shell-evaluated in
`assemble_command` since the command is replayed.

**Known gap (all ports)**: *cloning* an agent does not yet carry the source's conversation
forward -- the clone path doesn't copy the source's session/conversation store into the new
agent or set its resume id (opencode's per-agent `XDG_DATA_HOME` SQLite store and codex's
per-agent `CODEX_HOME/sessions/` rollouts have the same not-copied-on-clone limitation). (The antigravity changelog attributes this to a "global"
conversation store, but that phrasing predates the per-agent `$HOME` work in the same PR
series: with HOME relocation, `ANTIGRAVITY_APP_DATA_DIR` -- where agy writes
`brain/<conv_id>/...` -- points into each agent's own home, so conversations are now
per-agent, not global. The real blocker is just that clone doesn't copy them across homes.)

### M. Session preservation on destroy

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

### N. Streaming snapshot (live in-progress view)

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

### O. Deploy / scheduling contributions

For `mngr schedule` (cloud/scheduled agents), the plugin bakes its files and env vars into
the deployment image. **claude-only**; antigravity is *not* wired into scheduled deploys.

- **claude** (`plugin.py:2838`, `2933`): `get_files_for_deploy` ships generated
  `settings.json`/`.claude.json` (+ creds/skills/plugins when user-settings are included,
  with plugin paths rewritten to a sentinel and resolved at runtime);
  `modify_env_vars_for_deploy` injects `ANTHROPIC_API_KEY` and `IS_SANDBOX=1`.

**Questions**: Does the agent need to run under `mngr schedule`? If so, which config/cred
files and env vars must be present in the remote image?

### P. Field generators (listing columns)

Extra plugin-namespaced fields surfaced in `mngr list`, online and offline.

- **claude** (`plugin.py:2759`) -- **status: implemented (online).** `agent_field_generators` ->
  `waiting_reason`, reading the `permissions_waiting`/`active` markers without SSH/tmux to report
  `PERMISSIONS` vs `END_OF_TURN`. Claude does **not** implement `offline_agent_field_generators`.
- **antigravity** -- **status: should be done, but blocked on upstream.** agy *does* prompt
  interactively (its `ask` policy surfaces a `run_command` confirmation dialog that blocks the
  agent), so a `PERMISSIONS` reason would be meaningful in supervised mode. But agy fires no hook
  and emits no permission-dialog event while blocked (live-verified against agy 1.0.3), so mngr
  has no signal to read; implementing it needs an upstream event. (`END_OF_TURN` alone is
  derivable from the `active` marker but adds nothing over RUNNING/WAITING.)
- **pi-coding** -- **status: inapplicable (no need).** pi has no tool-approval gate at all: it
  runs tools, including shell commands, without a confirmation prompt by design
  (`mngr_pi_coding/README.md`). There is no blocked-on-permission state, so `PERMISSIONS` can
  never apply, and `END_OF_TURN` alone adds nothing over RUNNING/WAITING.
- **opencode** -- **status: implemented (online)** (both `PERMISSIONS` and `END_OF_TURN`), via
  `agent_field_generators`. opencode prompts interactively (its per-tool `ask` policy, e.g.
  `"edit": "ask"`) and -- unlike agy -- exposes the signal on the event bus: `permission.asked`
  fires when a tool blocks on approval (carrying the request `id`) and `permission.replied` when it
  is answered (carrying `requestID`). The in-process extension already subscribes to that bus (the
  `event` hook in `mngr_opencode_plugin.ts`), so it tracks the set of pending request ids and keeps
  a `permissions_waiting` marker present while any prompt is open -- the multi-session analog of
  codex's single touch/remove flag (opencode is a server with concurrent subagent sessions).
  Root-session idle clears any stranded marker as a safety net (codex uses the root `Stop`), and a
  fresh server clears a marker left by a prior killed/crashed server at startup (codex clears at a
  fresh root turn; claude has a startup reset). The gating rule lives in one shared
  `_classify_waiting_reason` routed through both the lifecycle promotion and the field generator, so
  the two cannot drift (mirrors codex). Notably opencode does **not** inherit codex's cancelled-dialog
  limitation: codex's hook model fires no terminal hook on Esc/No, stranding both markers until the
  next turn, but opencode's event bus emits `permission.replied` (on deny) and/or `session.idle` (on
  deny *and* abort), each of which clears the marker promptly -- verified live against 1.17.7.
  `OpenCodeAgent.get_lifecycle_state` promotes RUNNING -> WAITING while the marker is present,
  mirroring claude/codex; `END_OF_TURN` follows from the `active` marker being absent. Covered live
  by a release test (`test_opencode_waiting_reason_reports_permissions`): a real `bash: ask` agent
  blocks on an approval prompt and the marker appears -- the one check that exercises the real event
  wiring against the binary. (Caveat / **revisit**: the `@opencode-ai/sdk` type stubs are out of sync
  with the shipped binary -- they name the events `permission.updated`/`permissionID`, but the
  running server emits `permission.asked`/`requestID`, verified by inspecting the binary at **both**
  1.16.2 and 1.17.7. This is a known class of opencode bug -- the SDK permission types drifted from
  the binary in the permissions rework, e.g. opencode issue #7006 (a `permission.ask` plugin hook
  defined in the SDK but never triggered at runtime) -- and community plugins consume
  `permission.asked`/`permission.replied`, matching the binary, not the SDK's `permission.updated`.
  The plugin accepts **both** names since opencode self-upgrades; once the SDK and binary reconverge
  -- or opencode documents which is canonical -- the dead `permission.updated` branch can be dropped.)
  No upstream change was required.
- **codex** -- **status: implemented** (both `PERMISSIONS` and `END_OF_TURN`), via
  `agent_field_generators`. `PermissionRequest` touches a `permissions_waiting` marker (inline
  hook command) and `PostToolUse` clears it; the root `Stop` clears any stranded marker as a
  safety net. `CodexAgent.get_lifecycle_state` also promotes RUNNING -> WAITING while the
  marker is present, mirroring claude. Note codex has **no** `PostToolUseFailure` event (claude
  does), so cleanup is `PostToolUse` + `Stop` only. `END_OF_TURN` follows from the `active`
  marker (OR of `codex_root_active` and a non-empty `codex_subagents/`, recomputed under lock).
  Verified live against codex 0.139.0 with the exact production inline hook commands. Codex
  does **not** implement `offline_agent_field_generators`.

Note: core has no first-class "WAITING reason" -- WAITING is binary (marker absent); the
`waiting_reason` field is a plugin-specific embellishment that surfaces *why* a WAITING agent is
blocked (PERMISSIONS vs END_OF_TURN). Implementing `PERMISSIONS` needs two things: (a) the CLI
actually prompts interactively for tool approval, and (b) it exposes a signal mngr can read while
the agent is blocked. claude, codex, and opencode have both (implemented): opencode's `ask` policy
prompts and the event bus emits `permission.asked`/`permission.replied`. agy has (a) but not (b):
it prompts but fires no event while blocked, so it is blocked on an upstream signal. pi has neither
-- no tool-approval gate at all -- so `PERMISSIONS` is inapplicable. In the remaining unimplemented
cases `END_OF_TURN` alone is derivable but adds nothing over the existing RUNNING/WAITING state.

### Q. Installation management & version pinning

Check the binary is present and optionally install/pin a version.

- **claude**: `check_installation` (default True) and `version` pinning (`plugin.py` config).
- **pi-coding**: `_check_pi_installed`/`_install_pi` via npm, gated by
  local-vs-remote/auto-approve.
- **antigravity**: none -- assumes `agy` is on PATH (and documents a PATH-shadowing caveat
  with the desktop app's bundled shim).
- **opencode**: none yet -- assumes `opencode` is on PATH, with no version pinning. opencode
  self-upgrades, so the installed version is a moving target (verified against 1.16.2); the
  integration is written to *tolerate* old/new event shapes (it handles both `session.status`
  and the deprecated `session.idle`). Version pinning / install management is a natural
  follow-up.
- **codex**: no install/version *pinning* (assumes `codex` is on PATH), but -- unlike opencode --
  it *does* surface upstream CLI updates **mngr-side**, as the well-behaved replacement for codex's
  own blocking startup update prompt (which the plugin disables, see dimension J). At provision,
  `_maybe_check_for_codex_update` (`plugin.py:527-554`) runs a **network-free** check: it reads the
  user's real `~/.codex/version.json` -- the `latest_version` codex itself records on its own ~20h
  throttle (`codex_config.py:109-121`) -- and compares it to `codex --version`, printing a
  non-blocking notice when outdated, with opt-in auto-update (`auto_update` runs `codex update`, which
  self-detects brew/npm/standalone; off by default since it mutates the user's *global* install --
  `plugin.py:236-243`). It is best-effort, never fatal: an outdated codex still runs, and a corrupt
  or unusable `version.json` cache is tolerated -- `_parse_latest_codex_version` returns None on blank
  or malformed JSON (warning-logged, then skipped) so a bad cache just skips the check rather than
  breaking provision (`plugin.py:583-600`). Contrast: claude's `version` *pins* an install; pi's npm
  *install/check* installs the binary; codex neither installs nor pins -- it only *notifies* about an
  upstream update of an already-present binary (with optional self-update). Version pinning / install
  management proper is still a natural follow-up, like opencode.

### R. Workspace path quirks

Some CLIs reject mngr's dotted work-dir path (`~/.mngr/worktrees/...`).

- **antigravity**: `agy` refuses any path with a dot-prefixed segment as a workspace and
  silently falls back to `$HOME`. Workaround: a per-agent non-dotted symlink at
  `/tmp/mngr_antigravity_workspaces/<id>` -> real work_dir, recreated via `ln -sfn` on every
  launch, with `cd` into the symlink (`plugin.py:856`).
- **claude**: no such issue.
- **opencode**: no such issue -- the work dir is passed (URL-encoded) as the session-create
  `?directory=` query, with no dotted-segment rejection, so no symlink workaround is needed.
- **codex**: no such issue -- codex accepts the dotted `~/.mngr/worktrees/...` path as its cwd
  (`assemble_command` just `cd`s into the real work dir, `plugin.py:545`), so no symlink
  workaround is needed.

**Questions**: Does the CLI accept a dotted (`~/.mngr/...`) path as its cwd/workspace?

### S. Process name

`get_lifecycle_state` matches the running process against `get_expected_process_name()`,
which defaults to the command basename. Override if the binary's process name differs.

- **claude**: `"claude"`. **antigravity**: `"agy"` (`plugin.py:325`). **pi-coding**: `"pi"`.
  **opencode**: `"opencode"` (`plugin.py:208`) -- both `opencode serve` and `opencode attach`
  report `opencode`, and the foreground `attach` client is what lifecycle detection keys off.
  **codex**: `"codex"` (`plugin.py:231`) -- a single Rust binary; `ps`/tmux show the literal
  name, matching the command basename, but it is overridden explicitly for clarity.

### T. Extra agent subtypes

`mngr_claude` ships task-specialized subtypes that subclass the base agent and inject a
SKILL.md / different launch mode: `code-guardian`, `fixme-fairy` (both
`SkillProvisionedAgent`), and `headless_claude` (`claude --print`, streams from
`stdout.jsonl`). A port doesn't need these for baseline parity but the pattern
(`SkillProvisionedAgent`, `BaseHeadlessAgent`) is available to reuse. codex documents a
deferred second agent type in this spirit: an **app-server-backed** variant driving `codex
app-server` over JSON-RPC (programmatic messaging + a `codex --remote` TUI viewer + clean
`initialize`-based readiness) -- mirroring claude's `claude` + `headless_claude` split -- with
its design and an OpenAI-ToS caveat (identify honestly, no `codex-tui` spoofing) recorded in the
plugin README.

### U. Test scaffold

A new plugin needs a project-level `conftest.py` calling `suppress_warnings()`,
`register_conftest_hooks(globals())`, and `register_plugin_test_fixtures(globals())` (the
last pulls in the autouse `setup_test_mngr_env` that redirects `$HOME` to a temp dir for
*every* test, so tests can't touch the real `~/.mngr`/`~/.claude.json`/`~/.gemini`). This
`$HOME` redirect is the universal test-harness sandbox -- not to be confused with
antigravity's per-agent HOME *relocation*; it applies to config-dir-isolated plugins too.
If the plugin shares a credential, add a package-level fixture that seeds it where the
plugin reads the **shared** (user-side) credential from -- i.e. into that test-redirected
`$HOME` (e.g. `~/.gemini/.../oauth-token`, `~/.claude/.credentials.json`), so the plugin's
sharing logic finds it. antigravity's `isolated_home` is one example. Hook/converter shell
scripts get their own `*_test.py`. pyproject sets `--cov`, `fail_under = 95`, pyright
`strict`.

---

## Recommended bring-up sequence

A suggested order, by what each milestone depends on. The ordering is driven by
dependencies, not preference: you can't maintain a lifecycle marker until the agent runs,
can't do per-agent isolation meaningfully until you know what config/auth it touches, and
so on.

1. **Baseline: it runs and you can read it.** Register the agent type and config; get
   `assemble_command` right (workspace handling, backgrounded helpers); pick the readiness
   signal (sentinel if the CLI has one, else a TUI banner string); stream the raw + common
   transcript; handle the trust/onboarding gate so the first message isn't intercepted. At
   this point the agent works end-to-end but has no `active` marker, so it always reports
   WAITING.
2. **Lifecycle marker -- subagent-aware from the start.** Wire the CLI's pre-invocation/stop
   (or equivalent) events to maintain `active`, so the agent reports RUNNING vs WAITING; and
   in the same breath make it robust to subagents/nested invocations. Treat these as one
   piece of work, not two: a marker that flips to idle the moment a child finishes is just a
   broken marker, and you should design the clear-condition correctly the first time rather
   than ship a marker that's wrong under concurrency and patch it later. First confirm the
   CLI's hooks actually *execute* (not just load), then figure out the discriminator your
   CLI needs (see [dimension D](#d-subagent-aware-idle-gating-the-crux)) -- a distinct
   subagent event, an env-var guard, a root-vs-child conversation id, a fully-idle flag.
   Note that if the discriminator is a conversation/session id, this overlaps with step 3
   (the id usually comes from the same hooks), so the two are naturally done together.
3. **Conversation resume.** Capture the conversation/session id and append a resume flag in
   `assemble_command` so stop/start keeps context.
4. **Per-agent isolation.** Usually the biggest step: a per-agent config dir (or, only if
   the CLI forces it, a relocated `$HOME`), shared-but-overridable auth, per-agent
   permissions + model, and trust split into durable-global vs transient-per-agent. Resolve
   host paths over the host shell so it works remotely.

Then the claude features no port has matched yet, in roughly descending value: **session
preservation on destroy**, **deploy/scheduling contributions**, and the **streaming
snapshot**. (`waiting_reason` is matched by codex and opencode; still worth doing for agy once
it exposes a permission signal; inapplicable to pi, which has no approval prompt. See dimension P.)

Correctness hardening (shell-quoting of args, onboarding edge cases, etc.) is continuous,
not a milestone -- expect it throughout. For a concrete worked example of this whole
sequence, `mngr_antigravity` went through it stage by stage; its `UNABRIDGED_CHANGELOG.md`
walks each step in order.

### Known open gaps (carried by antigravity, and by definition by every newer port)

- Clone does not carry the source conversation forward.
- No permission-specific WAITING reason (depends on the CLI exposing a dialog/idle event).
- No readiness sentinel (banner-poll only) unless the CLI has an "input ready" event.
- `auto_allow_permissions` may require a skip-all flag rather than a hook decision.
- List-form `cli_args` with spaces/parens are not shell-quoted.

---

## New-CLI investigation checklist

Before writing code, answer these about the target CLI (the answers determine almost every
implementation choice above). **Verify each answer against the running binary, not just docs
or source** -- pi's trust trigger and the claude `Stop`-hook background-bash behavior both
read differently than they behave, and a wrong assumption here silently produces vacuous
tests or lost first messages.

**Mechanism & input delivery**
- [ ] What lever does the CLI give you for lifecycle code -- shell hooks, an in-process
  extension, or nothing? (Shapes every dimension below; see "Your lever" above.)
- [ ] Does it reliably accept tmux paste + Enter, or drop keystrokes? Is there a programmatic
  input channel (an inject API, an RPC `prompt` command, a socket) to use instead?
- [ ] What is the unambiguous signal that a submitted message actually started a turn (the
  `active` marker, a lifecycle event) -- so `send_message` can confirm delivery?

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
- [ ] Does it have a `run_in_background`-style tool (returns before the task finishes)? If so, how are those tasks identified so the marker isn't cleared while one runs (see dimension D)?

**Permissions & trust**
- [ ] Does a PreToolUse allow-decision suppress dialogs, or is a skip-all flag required?
- [ ] Is there a per-resource permission policy format?
- [ ] What *exactly* triggers the trust dialog -- which files/dirs, cwd-only or any ancestor
  -- confirmed against the running binary, not assumed from "the project has instructions"?
  Is the saved decision keyed off an exact (realpath) path?
- [ ] What gates first-run onboarding -- a file or settings key -- if the CLI has one at all?
  Consumer vs enterprise?

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

**Packaging & distribution** (don't forget once the plugin works)
- [ ] Registered in `PLUGIN_CATALOG` (`libs/mngr/imbue/mngr/plugin_catalog.py`) with its entry
  point, package name, a signal check that detects the CLI binary, and `is_recommended` if it
  should be offered by default? A bare config-shell stub usually predates real support with a
  catalog entry that lacks the signal check and `is_recommended`, so re-check both when a stub
  graduates to a real port. (opencode's entry now carries an `opencode --version`
  `OpenCodeSignalCheck` and `is_recommended=True`, `plugin_catalog.py:62`,`:128`; codex's
  likewise carries a `codex --version` `CodexSignalCheck`, `package_name = "imbue-mngr-codex"`,
  and `is_recommended=True`, `plugin_catalog.py:68`,`:143`.)
- [ ] Is the package publishable -- i.e. *not* listed in `UNPUBLISHED_PACKAGES` -- so the
  release tooling and `mngr extras` will actually offer it?

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
- Other real ports: `libs/mngr_pi_coding/` (in-process extension), `libs/mngr_opencode/`
  (client-server / HTTP-driven, in-process server plugin), `libs/mngr_codex/` (third shell-hooks
  port; async-subagent gating; see its README and
  `specs/agent-plugin-parity/codex-investigation.md` for the source-verified investigation)
- Remaining bare `BaseAgent` shells (no named CLI behind them): the built-in `command` /
  `headless_command` types in `libs/mngr/imbue/mngr/agents/default_plugins/`
