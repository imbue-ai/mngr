# Host-scoped agent identity

Status: **Proposed**

Audience: developers implementing or reviewing changes in `libs/mngr`, the
plugin libraries (especially `mngr_forward`, `mngr_latchkey`, `mngr_usage`),
`apps/minds`, and `apps/remote_service_connector`.

Related specs: [workspace-sync](../workspace-sync/spec.md),
[workspace-server-forwarding](../workspace-server-forwarding/spec.md),
[detached-destroy-flow](../detached-destroy-flow/spec.md),
[host-backup](../host-backup/concise.md).
See also the entry in [uncertainties.md](../uncertainties.md) recording the
conflict with workspace-sync's identity model.

## Purpose

`AgentId` is only guaranteed unique **within a host**: the true unique key for
an agent is `(host_id, agent_id)`. IDs are uuid4-minted at creation, so random
collision never happens, but IDs are duplicated whenever agent state is copied
wholesale: `mngr create --id`, copying agent state directories between hosts
(the minds state-migration incident), and any image/state-cloning flow. An
audit (2026-07, this branch) found a large number of places that implicitly
assume `agent_id` is globally unique; the worst collapse silently (wrong-host
routing, cross-host destroys) rather than erroring.

This spec defines the target identity model and the phased work to make the
codebase genuinely indifferent to cross-host `agent_id` duplicates:

1. **Phase 1** -- mngr core: re-key discovery/observation/resolution state by
   `(host_id, agent_id)` and make ambiguous bare-ID references explicit errors.
2. **Phase 2** -- plugins: propagate host-scoped keys through `mngr_forward`,
   `mngr_latchkey`, `mngr_usage`, `mngr_notifications`,
   `mngr_claude_subagent_proxy`, and `mngr_mapreduce`.
3. **Phase 3** -- minds + remote_service_connector: a **full migration of
   workspace identity to `host_id`** (URLs, routes, window identity,
   registries, tunnel names), with backup/restore handled through the existing
   host-lineage field. Restore produces new URLs by design.

### Non-goals

- Re-minting `agent_id` when state is copied. Rejected: it cannot cover
  out-of-band copies, and minds restore intentionally copies agent state.
- Making `agent_id` globally unique by construction or enforcement.
- Multi-workspace-per-host support in minds. A minds workspace remains 1:1
  with a host (one primary agent plus the system-services agent).
- Changing the on-host state layout (`<host_dir>/agents/<agent_id>/...` is
  already host-scoped and stays as is).

## Identity model

### Invariants (after this work)

- `(host_id, agent_id)` uniquely identifies an agent. `agent_id` alone does
  not, anywhere, for any purpose.
- `host_id` is globally unique. This is minted-unique, not enforced: every
  creation path calls `HostId.generate()` (verified for the local, docker,
  lima, and VPS providers; for imbue_cloud, each slice VM gets
  `HostId.generate()` at carve time in `providers/slice_provider.py`, and each
  bare-metal pool host's id comes from the real `mngr create` run against that
  box in `bake/pool_bake.py` -- pool bakes install onto each box individually
  and never clone a disk image carrying mngr state). Discovery already errors
  on duplicate host matches (`filter_one_host`); that detection stays.
- A bare `agent_id` (or agent name) in a CLI address is a *query*, not a key.
  If it matches agents on more than one host, the command errors and lists the
  matches in `NAME@HOST.PROVIDER` form; it never picks one and never acts on
  all of them. `ID@HOST[.PROVIDER]` is the unambiguous form (the address
  grammar already supports it).
- In minds, **`host_id` is the workspace identity**. `agent_id` is a per-host
  implementation detail that never appears in URLs, routes, window keys,
  persisted client state, or server-side uniqueness constraints.
- Backup/restore lineage flows through `restored_from_host_id` on the sync
  record (already in the schema), not through a shared `agent_id`. A restored
  workspace is a new host with new URLs.

### Composite key plumbing

Add to `libs/mngr/imbue/mngr/primitives.py`:

```python
class HostScopedAgentId(FrozenModel):
    """The globally unique identity of an agent: its host plus its per-host id."""

    host_id: HostId
    agent_id: AgentId

    def as_string(self) -> str:
        return f"{self.host_id}/{self.agent_id}"

    @classmethod
    def from_string(cls, value: str) -> "HostScopedAgentId": ...
```

- Used as the dict/set key wherever agents from more than one host can meet in
  one structure. Purely single-host structures (anything inside a `Host`,
  agent state dirs, per-host persisted files) keep plain `AgentId`.
- The string form is for JSONL/JSON payload keys and log lines. `/` is safe
  because neither id type can contain it, and it is not used in filesystem
  paths (paths use separate `<host_id>/<agent_id>` components or an explicit
  `{host_id}--{agent_id}` single component where a flat dir is required).
- Event payloads and callback signatures that today carry only `agent_id` gain
  an explicit `host_id` field alongside it rather than embedding the composite
  string, so existing consumers can migrate incrementally.

## Phase 1: mngr core

The spine. Everything downstream (plugins, minds) reads the discovery event
stream and the aggregated topology, so these must stop collapsing duplicates
first.

### Discovery event replay and resolution (`api/discovery_events.py`)

`_ResolutionMaps.provider_by_agent_id` / `name_by_agent_id` /
`host_id_by_agent_id` are last-writer-wins across providers, and
`_apply_provider_snapshot_to_maps` "forgets" an id when the provider it is
currently attributed to stops reporting it -- with a duplicate, ownership of
the id flaps between providers on every snapshot and agents transiently
vanish.

- Re-key the maps by `HostScopedAgentId` (equivalently: nest by host_id).
- Identifier resolution (`resolve_provider_names_for_identifiers` and the
  agent-identifier fast path) returns the **union** of providers/hosts that
  have ever matched the identifier, not a single attribution.

### Discovery fast path (`api/discover.py`)

Today an identifier-scoped discovery narrows to the single recorded provider
and `_all_identifiers_found` short-circuits on the first hit, which defeats
the multi-match detection in `find.py` -- a bare duplicate ID silently
resolves to whichever provider last wrote the stream. After the map fix the
narrowed provider set contains every candidate provider, so all duplicates
surface and `filter_one_agent` can do its job. `_all_identifiers_found` keeps
its meaning ("each identifier matched at least once somewhere") -- it only
gates the full-scan fallback, and the ambiguity error below handles
multi-match.

### Aggregator and observer (`api/discovery_aggregator.py`, `api/observe.py`)

- `DiscoveryStateAggregator`: `_agent_by_id`, `_provider_name_by_agent_id`,
  `_unknown_agent_ids`, `_last_event_time_by_agent_id` re-keyed by
  `HostScopedAgentId`. `AggregatorDelta.added_agent_ids` /
  `removed_agent_ids` become sets of composite keys (string form), and
  `get_agent_by_id` is replaced by a host-qualified lookup.
- `AgentObserver`: `_last_tracked_state_by_id`, `_last_known_details_by_id`,
  `_watchers` re-keyed the same way. `AgentDetails` already carries `host_id`,
  so this is a key change, not a data change. `AgentRemovedEvent` gains
  `host_id` (`AgentDestroyedEvent` already has it).

### CLI semantics

- `filter_one_agent` (`api/find.py`): keep the multi-match error, fix the
  message for the ID case (it currently says "Multiple agents found with
  name" and advises "use the agent ID directly", which is nonsense when the
  duplicate *is* an ID -- advise `ID@HOST.PROVIDER` instead).
- Bulk commands (`destroy`, `stop`, `start`, `archive`): a single identifier
  that matches agents on multiple hosts is an error (same message as above)
  unless the address carries a host qualifier. `--all`-style selection is
  unaffected (it enumerates real agents per host, not identifiers).
- `mngr cleanup` TUI (`cli/cleanup.py`): `selected_ids` keys by
  `(host_id, agent_id)` so selecting one duplicate cannot select the other.
- `mngr create --id`: error if the requested id already exists on the *target*
  host (per-host uniqueness is a hard invariant); duplicates on other hosts
  are allowed and merely logged.
- `rename.py` name-conflict check: compare `(host_id, agent_id)`, not bare id,
  when deciding "is this the same agent".
- Shell completion caches (`cli/complete_names.py`): key by composite; on
  ambiguous completion, emit the `NAME@HOST.PROVIDER` form.

### Preserved agents (`api/preservation.py`)

Preserved state from all hosts lands in one local namespace keyed
`{agent_name}--{agent_id}`, so a clone's preservation overwrites the
original's. New layout: `preserved/{host_id}/{agent_name}--{agent_id}`.
Readers (usage preservation walker, `subagent_wait`) check the new layout
first and fall back to the flat legacy path; no migration of existing dirs.

### Ratchet

Add a ratchet (via `/writing-ratchet-tests`) counting `dict[AgentId` /
`set[AgentId` / `frozenset[AgentId` occurrences outside `hosts/` and
single-host modules, to prevent new cross-host id-keyed structures.

## Phase 2: plugins

All of these consume the Phase 1 stream/callback changes; the pattern is the
same everywhere: carry `host_id` next to `agent_id`, key composite.

- **`mngr_forward`** (internal state only in this phase; URL scheme moves in
  Phase 3): `resolver.py` maps (`_services_by_agent`, `_ssh_by_agent`,
  `_known_agent_ids`), `service_map_cache.py` persisted JSON,
  `stream_manager.py` `_events_services`, and the
  `OnAgentDestroyedCallback = Callable[[AgentId], None]` contract (which drops
  `host_id` exactly where its discovered-callback counterpart keeps it) all
  become host-scoped. `ssh_tunnel.remove_reverse_tunnels_for_agent` takes the
  host key too -- today it filters the entire cross-host tunnel registry by
  bare agent_id, so tearing down one agent kills the duplicate's tunnels.
- **`mngr_latchkey`**: `discovery.py` `_pending_remote_agents` keyed
  composite; the destroyed callback and `_tear_down_stopped_agent` carry
  `host_id` (the host-side permission files are already per-host and stay).
- **`mngr_usage`**: `api.py` per-agent event grouping and the live-vs-preserved
  dedup key by composite; preservation reads use the Phase 1 layout.
- **`mngr_notifications`**: `watcher.py`
  `was_running_before_unknown_by_agent_id` keyed by the composite string from
  the (now host-qualified) agent-states events.
- **`mngr_claude_subagent_proxy`**: reap currently destroys every agent
  anywhere whose `mngr_claude_subagent_proxy_parent_id` label matches the
  parent's `MNGR_AGENT_ID`. Scope the `find_terminal_children` match to the
  parent's own host (children are always spawned on the parent's host; the
  parent knows its host via `MNGR_HOST_DIR`/host data). Keep the label format
  unchanged.
- **`mngr_mapreduce`**: orchestration maps (`all_hosts`, `agent_id_to_info`,
  `pending_ids`, `timed_out_ids`) keyed composite. Low urgency (ids are
  freshly minted) but cheap once the pattern exists.

## Phase 3: minds and remote_service_connector

Full migration of workspace identity to `host_id`. A workspace URL, window,
registry entry, or sync constraint never mentions `agent_id` again.

### Resolver (`desktop_client/backend_resolver.py`)

- The workspace enumeration (`list_known/active/restorable_workspace_ids`)
  returns **host_ids** (one per host carrying an `is_primary` agent).
  Duplicate rows become structurally impossible.
- `get_agent_display_info` and every first-match `agent.agent_id == agent_id`
  loop become host_id lookups; the resolver resolves host -> primary agent
  internally where an agent-level operation (SSH exec, chat) needs it.
- Collapsed maps (`_services_by_agent`, `ssh_info_by_agent_id`, rename
  overrides, colors) re-key by host_id. SSH info is already tracked per host
  in `forward_cli.py` (`_ssh_by_host_id`) and then flattened to agent_id --
  delete the flattening.
- `get_system_services_agent_id` already filters by host_id + name once it has
  the host; it now takes host_id directly.

### Workspace-scoped registries

Re-key by `host_id`: `workspace_operations.py` (op records, log queues, cancel
events), `destroying.py` (marker dir becomes `destroying/<host_id>/`; the dir
already stores the host_id in a file -- it becomes the key),
`system_interface_health.py` (`_records`, probe targets),
`mind_liveness.py`, `latchkey/permission_overview.py` (`rules_by_agent` --
the per-host permission data is already there). `workspace_record_store`'s
storage is already host-keyed; delete the `find_active_record(agent_id)` /
`_find_record_any_state(agent_id)` first-match lookups in favor of host_id
lookups, and `session_store.get_account_for_workspace` takes host_id.

### URL surfaces and routing

- `api_v1.py`: all `/{agents|workspaces}/<id>/...` routes take **host_id**.
  The route regex/validation changes from `agent-` to `host-` prefixed ids.
  These routes are internal (frontend + Electron only), so this is a
  coordinated rename, not a compatibility problem.
- Gateway routing. Two surfaces use agent-keyed URLs today and both re-key:
  - The minds workspace gateway (the `/goto/<id>/` redirect and the
    `<id>.localhost` subdomain-per-workspace scheme from
    [workspace-server-forwarding](../workspace-server-forwarding/spec.md),
    including its subdomain-scoped auth cookies): becomes `/goto/<host_id>/`
    and `<host-id>.localhost`, routing to that host's workspace server (one
    workspace server per workspace/host). Which process serves each piece
    (the desktop client's middleware vs the `mngr_forward` server it fronts)
    is an implementation detail to confirm during this phase; both re-key the
    same way.
  - `mngr_forward`'s generic agent-service forwarding (usable outside minds):
    the subdomain label becomes `<agent-id>--<host-id>` (single DNS label;
    both components are fixed-format prefixed hex ids, so splitting on `--`
    is unambiguous). Bare `<agent-id>` labels remain accepted only when they
    resolve unambiguously; an ambiguous label gets an error page listing the
    qualified forms, never a guessed backend.
- Electron (`main.js`, preloads, static JS): window/bundle identity, IPC
  payloads, accent/status caches, and session-restore URL matching move from
  `agentId` to `hostId` (`/^host-[a-f0-9]{1,64}$/`). Saved session state from
  older versions contains agent-id URLs; on first launch after the migration
  those entries fail the host-id match and are dropped (windows simply do not
  restore once -- acceptable, matches "restore = new URLs" posture).
- Landing/sidebar/templates: rows keyed `data-host-id`; SSE payloads carry
  `host_id`.

### remote_service_connector

- **Drop the `(user_id, agent_id) WHERE state='active'` partial unique
  index** (`workspace_records_one_active_per_agent_idx`, migration 013). The
  PK `(user_id, host_id)` is the real identity; with the index gone, two
  hosts carrying the same agent_id can both hold active records, which is the
  whole point. `agent_id` stays on the row as informational metadata. New
  migration + removal of the `SyncActiveAgentConflictError` path.
- **Tunnel/hostname naming**: `make_tunnel_name` / `make_hostname` derive
  from `host_id` instead of `agent_id`. Keep the 16-hex-char truncation for
  hostname-length reasons but note it in the code: truncation collisions are
  now operator-visible name clashes on globally unique inputs (2^64 space)
  rather than guaranteed clashes on duplicated ids. Existing tunnels named
  from agent ids are torn down/recreated on the next sharing toggle; no
  in-place rename.

### Backup/restore ("special handling")

The sync record already models this correctly: a restore leases/creates a
**new host** (new `host_id`, new record row) with `restored_from_host_id`
pointing at the source, and the source row is tombstoned (`state=destroyed`).
Phase 3 makes the client honor that model:

- Restore copies the workspace's display metadata and secrets to the new
  record (already the case) and the client navigates to the new host's URLs.
  Old URLs/windows for the tombstoned host dangle by design ("restore = new
  URLs"); the landing page shows only the new row because enumeration is by
  host.
- The copied agent state still contains the old `agent_id` -- that is now
  harmless everywhere, which is the acceptance criterion for the whole spec.
- No redirect from old host URLs to the successor. If that ever becomes a
  want, `restored_from_host_id` supports building it later.

## Testing strategy

The recurring fixture: **two mock hosts, each carrying an agent with the same
`agent_id` (and same name)** -- exactly what a state copy produces.

- **Unit (Phase 1)**: run the duplicate fixture through
  `_ResolutionMaps` replay, `DiscoveryStateAggregator`, and `AgentObserver`;
  assert both agents remain visible and stable across alternating provider
  snapshots (no flapping, no cross-attribution), and that removing one emits
  a removal for only that composite key. Address resolution: bare duplicate
  ID errors listing both `@HOST.PROVIDER` forms; `ID@HOST` resolves; bulk
  destroy/stop with a bare duplicate ID errors before acting. Preservation
  path: both duplicates preserve without overwriting; legacy flat path still
  readable.
- **Unit (Phase 2)**: forward resolver + stream manager with the duplicate
  fixture (distinct service URLs per host survive; destroying one agent
  removes only its routes/tunnels); usage grouping keeps the two streams
  apart; subagent-proxy reap matched only against same-host agents.
- **Integration (Phase 3)**: minds resolver + landing enumeration with the
  duplicate fixture renders two distinct workspace rows (host-keyed);
  operations registries track the two independently (start a restart op on
  one, `start_if_idle` on the other succeeds); destroying marker for one does
  not mark the other.
- **Acceptance**: one end-to-end minds flow -- create a workspace, produce a
  duplicated-agent-id second workspace by copying the agent state dir onto a
  second host (the local provider has exactly one host, so this needs a
  docker host and the corresponding pytest marks), verify both appear and are
  independently operable, destroy one, verify the other is untouched.
  Restore flow: restored workspace appears under a new host_id URL and the
  old row is tombstoned.
- Per repo policy these live as unit tests (`*_test.py`) plus
  `@pytest.mark.acceptance` for the end-to-end flows; the ratchet from
  Phase 1 guards regressions structurally.

## Rollout and compatibility

- Phases land in order; each is independently shippable. Phase 1 changes the
  discovery event payload shapes (additive `host_id` fields) -- consumers in
  the same repo update in lockstep; the JSONL streams are not a public API.
- Phase 3 is a breaking client migration gated on a minds release: api_v1
  routes, Electron IPC, forward routing, and the RSC index migration ship
  together (the release-coupling mechanics follow
  `apps/minds/docs/release.md`). Old saved windows drop once; synced records
  need no data migration (already host-keyed).
- The forward's persisted `service_map_cache` files re-key; stale agent-keyed
  cache files are ignored and rewritten (cache is advisory).

## Open questions

- Naming: `HostScopedAgentId` vs `AgentKey` vs `GlobalAgentId`. Proposal:
  `HostScopedAgentId` (self-describing, ugly enough to discourage casual
  use where plain `AgentId` suffices).
- Whether the generic forward subdomain form `<agent-id>--<host-id>` should
  instead always be host-first (`<host-id>--<agent-id>`) for lexical grouping
  in logs and cookies. Proposal: agent-first reads better in the common case;
  either is fine, pick one and keep it.
- Whether Phase 1 should also emit a startup/discovery warning when a
  duplicate `host_id` is observed across providers (minted-unique should make
  this impossible, but a wholesale host-state copy would clone it). Proposal:
  yes, log a warning; `filter_one_host` already errors on direct reference.
- Whether `mngr create --id` should warn (not error) when the id exists on
  another host, to keep noise down for the minds restore path which
  legitimately recreates known ids. Proposal: log at info, no warning.
