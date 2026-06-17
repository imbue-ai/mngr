# Agent Cloning Spec [future]

This document specifies how `mngr create --from <agent>` copies per-agent state from a
source agent into the new agent, and proposes generalizing the current Claude-specific
mechanism into an opt-in, plugin-registered facility.

For the agent state layout (certified vs. reported data) see [agent.md](./agent.md). For
the plugin hook surface see [the plugins concept doc](../docs/concepts/plugins.md).

## Purpose and Scope

`--from <agent>` is logically "make a copy of this agent." Today it reliably copies only
the **work dir** (project files, via git/rsync). Copying the agent's *state* -- the data
that makes the copy feel like a continuation rather than a blank agent -- is implemented
as a special case inside the Claude plugin and runs for no other agent type.

This spec covers:

- What state is and is not eligible to travel on a clone.
- A general mechanism that lets any plugin opt specific state into the clone.
- How the existing Claude session-adoption logic re-expresses itself on that mechanism.

It does **not** cover the work-dir transfer (git/rsync), which is unchanged, nor
`--adopt-session` beyond its interaction with cloning.

## Background: What Happens Today

When `--from` resolves to an agent, `create` computes the source agent's state directory
and threads it through the creation pipeline as
`CreateAgentOptions.source_agent_state_location` (`interfaces/host.py`). mngr **core does
nothing with this field itself** -- it is consumed only by the Claude agent class:

1. `ClaudeAgent.provision()` calls `_transfer_source_plugin_data()`, which rsyncs the
   source agent's **entire `plugin/` subtree** into the new agent's state dir. This runs
   before per-agent config setup, which then overwrites identity files (`.claude.json`,
   credentials) with fresh values.
2. `ClaudeAgent.on_after_provisioning()` calls `_adopt_cloned_session()`, which *rewires*
   the copied data: it renames the encoded project subdir from the source's `work_dir`
   encoding to the destination's, carries `claude_session_id_history` forward, selects the
   latest session JSONL, and writes `claude_session_id` so startup runs `claude --resume`.

Two consequences of this implementation are accidental rather than designed:

- **Clone copying is agent-type-gated.** It runs only because `ClaudeAgent` implements it.
  Cloning a Codex or OpenCode agent copies no state at all.
- **The copy is indiscriminate across plugins.** `_transfer_source_plugin_data` copies the
  whole `plugin/` tree, so every plugin's reported files ride along whenever the agent
  happens to be Claude -- including state a clone arguably should not inherit.

## State Eligibility

Per-agent state falls into three buckets. The mechanism below applies only to the third.

**Never copied (identity and liveness).** Copying these would corrupt the new agent's
identity or misrepresent a fresh agent as a running one:

- `data.json` core certified fields: `id`, `name`, `create_time`, `command`, `work_dir`.
- `url`, `status/`, `active`, `activity/`.
- Live session/process markers written by runtime hooks (e.g. Claude's `claude_session_id`,
  `session_started`, `claude_process_started`, `permissions_waiting`).

**Never copied (cost integrity).** `events/<agent_type>/usage/events.jsonl` and sibling
billing/usage event logs. Copying them would double-count the source's spend against the
clone.

**Eligible, but only when a plugin opts in (this spec).** Reported plugin files under the
plugin's own sandbox, `plugin/<plugin_name>/`. A plugin may register paths only within its
own sandbox; everything outside every sandbox is, by construction, never copied. This makes
the exclusion rules above trivial to enforce -- live markers and identity files live at the
state-dir root, outside any sandbox, so they cannot be registered.

Clone-safe state must therefore live under the plugin's sandbox. Concretely, Claude's
`claude_session_id_history` (clone-safe) migrates from the state-dir root into
`plugin/claude/`, while the live `claude_session_id` marker stays at the root and is never
copied.

Certified plugin keys (`data.json:plugin.<plugin_name>`) are **out of scope for the first
cut**: no current plugin wants its certified data cloned (kanpan's mute should reset; usage
must not copy), so a `certified_keys` surface would be dead. It can be added when a real
consumer appears, mirroring `reported_paths`.

The default is **copy nothing**: a plugin that registers nothing contributes nothing to a
clone. This is deliberately conservative -- silent inheritance of per-agent state (a mute
flag, a cached credential) is a worse failure than a clone that starts slightly emptier
than expected.

## Proposed Mechanism

Cloning is split into a declarative *what* and an imperative *how*, because the realistic
consumer (Claude) needs to transform copied data, not merely relocate it.

### 1. Declarative registration: what to copy

A new registration hook lets each plugin declare its clone-safe state:

```python
@hookimpl
def register_clone_state() -> CloneStateSpec:
    return CloneStateSpec(
        reported_paths=("plugin/claude",),       # paths within this plugin's sandbox
    )
```

- `reported_paths`: relative paths under the agent state dir whose contents core copies
  from the source to the destination (host-to-host, so cross-host clones work). Each path
  must fall within the registering plugin's own `plugin/<plugin_name>/` sandbox; core
  validates this and rejects out-of-sandbox paths. A plugin that wants its whole sandbox
  copied registers just `plugin/<plugin_name>`; one that wants only durable subsets can
  name narrower paths.

Core performs these copies for **every** `--from <agent>` clone regardless of agent type,
fixing the agent-type gating. Because each plugin registers only paths within its own
sandbox, the indiscriminate whole-`plugin/`-tree copy goes away.

### 2. Imperative rewire: how to fix up the copy

After core has copied all registered state, plugins that need to transform it implement a
rewire hook:

```python
@hookimpl
def on_clone_from_agent(
    source_location: HostLocation,
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
) -> None:
    ...
```

This hook receives both the source location and the fully-created destination agent, and
runs on the destination host. It is the single seam any plugin (not just the agent type)
can use to react to a clone. Claude's `_adopt_cloned_session` logic moves here verbatim:
re-encode the project subdir for the new `work_dir`, pick the active session, write
`claude_session_id`.

### Pipeline ordering

The copy must run after the destination state dir exists and before any provisioning that
would overwrite identity files. The concrete seam (e.g. immediately after
`on_agent_state_dir_created`, or a dedicated step before `provision()`) is an open
question below. The rewire hook runs after provisioning, mirroring Claude today.

### Claude after generalization

`ClaudeAgent` stops doing its own copy: `_transfer_source_plugin_data` is deleted, and the
Claude plugin registers `reported_paths=("plugin/claude",)`. The `claude_session_id_history`
file moves from the state-dir root into `plugin/claude/` so it travels with the sandbox; the
runtime hooks that write it (and the readers) update to the new path. `_adopt_cloned_session`
becomes the body of `on_clone_from_agent`. Claude is then the reference consumer of a
mechanism any plugin can use, rather than the sole hard-coded implementation.

## Resolved Decisions

- **Two hooks.** Declarative `register_clone_state` (core copies) plus imperative
  `on_clone_from_agent` (plugin rewires). The declarative half lets a future
  non-transforming plugin clone with registration alone and no code.
- **Strict per-plugin sandbox.** `reported_paths` may only name paths within the registering
  plugin's `plugin/<name>/` sandbox; core validates and rejects others. Claude's
  `claude_session_id_history` migrates into `plugin/claude/`.
- **Reported paths only for the first cut.** Certified-key copying is deferred until a real
  consumer exists.

## Open Questions

1. **Typed reported-file API vs. raw convention.** [docs/code divergence] The plugins
   concept doc presents `set_reported_plugin_file` / `get_reported_plugin_file` as *the* way
   to store per-agent plugin data, but Claude -- the heavy user of the sandbox convention --
   never calls them (it writes via shell hooks and config provisioning, copies via raw
   `copy_directory`). The strict-sandbox decision makes the *directory* the contract, so
   cloning works regardless. Remaining question: do we leave the typed API as an optional
   convenience (lower lean: simplest, matches reality) or push consumers onto it for a single
   well-defined access path? This does not block the cloning mechanism.

2. **Pipeline seam for the core copy.** Where exactly does the generic copy run relative to
   `on_agent_state_dir_created`, the agent's `provision()`, and the file-copy hooks? It must
   precede any provisioning that overwrites identity files (Claude relies on copy-then-
   overwrite ordering today).

3. **Harmonize `--adopt-session` with `--from` (goal, partially scoped).** The intent is for
   `--adopt-session` and `--from` to share machinery rather than be parallel special cases.
   They already converge on `_finalize_adopted_session`, and `--adopt-session`'s source
   resolution (`_resolve_adopt_session`) already searches agent sandboxes, so `--from <agent>`
   is nearly "adopt the latest session from agent X, plus carry the rest of the sandbox."

   **Feasible plan:** factor a single Claude-internal routine -- *given a source project dir
   (on some host) and a session selector (a named id, or "latest"), place it under this
   agent's encoded `projects/` dir and finalize* -- and route both paths through it:
   - `--from` = generic sandbox copy (via `register_clone_state`) + `on_clone_from_agent`
     invoking the routine with `selector=latest` against the just-copied sandbox.
   - `--adopt-session` = resolve source + named session, then invoke the same routine, with
     no generic sandbox copy.

   **Boundary:** `--adopt-session` cannot be expressed as pure registration. Its source is not
   always an agent (it may be the user's `~/.claude` or a loose `.jsonl`) and it selects a
   specific session rather than copying a whole sandbox -- both are Claude-domain concepts.
   So it remains a Claude CLI feature that *shares Claude's cloning internals* with `--from`,
   rather than folding into the cross-plugin `register_clone_state` mechanism, which stays
   "copy sandbox + call rewire hook." The mutual exclusion between the two flags is retained.

   **Known friction:** `--from` today bulk-copies the sandbox then *renames* the project
   subdir (avoiding a second copy), while adopt copies a specific dir; the shared routine must
   reconcile rename-in-place vs. copy (rename is an optimization when the data is already local
   in the sandbox).

4. **Idempotent re-create (`is_update=True`).** Cloning into an update of an existing agent
   is almost certainly nonsensical; confirm the copy is skipped (or rejected) when
   `is_update` is set.

## Resolved: Failure Semantics

A clone **warns and degrades** rather than failing the `create`. If a registered path is
missing on the source, or a rewire hook cannot complete (e.g. no source session to resume),
the new agent is still created -- with a partial copy, or a fresh session -- and a warning is
logged. This matches Claude's current behavior (it already degrades to a fresh session with a
warning when the source has no session). A half-built clone should never block bringing the
agent up; the user can re-clone or adopt explicitly if the degraded result is unacceptable.
