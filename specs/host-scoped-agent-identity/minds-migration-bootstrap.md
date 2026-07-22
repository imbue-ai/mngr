# Bootstrap: minds host_id workspace-identity migration

Orientation doc for the agent that will write and implement the **minds +
remote_service_connector** spec of the host-scoped agent identity program.
Read [spec.md](./spec.md) first for the overall identity model; this doc
carries the minds-specific audit inventory, the decisions already made, and
the dependencies on the mngr-side work.

## Mission

Migrate minds workspace identity from `agent_id` to `host_id` everywhere:
URLs, routes, Electron window identity, client registries, sync constraints,
and tunnel names. After this work, a cross-host duplicate `agent_id` (which
state copies legitimately produce) must be harmless in minds, and two
workspaces must never collapse into one row, record, or window.

## Decisions already made (user-confirmed, do not relitigate)

- A minds workspace is 1:1 with a host; `host_id` is the workspace identity.
  `agent_id` becomes a per-host detail that never appears in URLs, routes,
  window keys, persisted client state, or server-side uniqueness constraints.
- This is a **full migration**, including user-visible URL surfaces -- not
  just internal keying.
- **Restore = new URLs.** A restored workspace is a new host with a new
  identity; lineage flows through the existing `restored_from_host_id`
  column on the sync record. No redirects from old URLs.
- The RSC partial unique index `(user_id, agent_id) WHERE state='active'`
  (migration 013) is wrong under this model and gets dropped; `agent_id`
  stays on the row as informational metadata.
- `host_id` global uniqueness is minted (uuid4 per host creation), verified
  for all providers including the imbue_cloud slice/pool bake paths (each
  pool box runs its own `mngr create`; images are never cloned with mngr
  state). Rely on it.

## Dependency: the mngr-side work lands first

A separate spec covers mngr core + plugins. The parts minds consumes:

- Discovery aggregator / observer / event-replay maps re-keyed by
  `(host_id, agent_id)`; `AggregatorDelta` and agent-removed/destroyed
  events carry `host_id`. Until that lands, minds cannot even *see* two
  same-id agents as distinct -- `list_agents` output is fine (it is
  host-grouped) but the aggregated stream collapses duplicates.
- `mngr_forward` internal state (resolver, service-map cache, stream
  manager, reverse-tunnel teardown) becomes host-scoped, and the
  `OnAgentDestroyedCallback` contract gains `host_id`.
- The minds-facing gateway URL scheme (`/goto/<id>/`, `<id>.localhost`
  subdomains, cookie scoping) is **deliberately left to the minds spec** --
  coordinate with whatever the mngr spec does for generic forward routing so
  minds does not break in the window between the two (see open questions).

## Audit inventory (2026-07, branch `gabriel/sparkling-ibex`)

Every known agent_id global-uniqueness assumption in minds/RSC. Line numbers
drift; treat them as anchors, re-grep before editing. "First-match" = loop
returning the first `agent.agent_id == agent_id` hit across all hosts;
"collapse" = `dict[str(agent_id)]` last-write-wins across hosts.

### desktop_client/backend_resolver.py (central; fix first)

- First-match resolvers: `_discovery_workspace_display_name_locked`,
  `_discovery_host_name_locked`, `get_agent_label`, `get_workspace_color`,
  `get_agent_display_info` (the hinge -- it is the agent->host resolver used
  by api_v1, app.py landing, mind_liveness, report_collector,
  workspace_record_store), `get_system_services_agent_id`.
- Collapsed maps: `_services_by_agent`, `ssh_info_by_agent_id`,
  `_workspace_name_override_by_agent_id`.
- `set_workspace_color_locally` updates *every* agent with a matching id.
- `list_known/active/restorable_workspace_ids` can return the same id twice
  (two `is_primary` agents on two hosts) -- the direct cause of the
  landing-page duplicate/stale-row bug.
- Already host-scoped, preserve: `host_state_by_host_id`,
  `host_name_by_host_id`, `_host_state_override_by_host_id`,
  `_last_good_agents_by_host`, `get_host_state`.

### Workspace-scoped registries (re-key by host_id)

- `workspace_operations.py`: `record_by_agent_id`, `log_queue_by_agent_id`,
  `cancel_event_by_agent_id` -- shared op record/log/cancel across colliding
  workspaces; `start_if_idle` false-positives.
- `destroying.py`: marker dir is `destroying/<agent_id>/`; a colliding
  `start_destroy` *reuses the other workspace's record*. The host_id is
  already written inside the dir and the destroy command itself is
  host-scoped -- only the keying is wrong.
- `system_interface_health.py`: `_records` keyed by id; one workspace's
  STUCK state force-redirects the colliding one to recovery.
- `mind_liveness.py`: `compute_mind_liveness_by_agent_id` collapse.
- `latchkey/permission_overview.py`: `rules_by_agent` collapse (the
  underlying permission files are already per-host; trivial fix).

### Persisted/sync state

- `workspace_record_store.py`: on-disk store is correctly keyed by host_id;
  the first-match lookups on top (`find_active_record`,
  `_find_record_any_state`) are the problem. `associations_view` yields
  duplicate agent_ids.
- `session_store.py`: `get_account_for_workspace(agent_id)` first-match.

### REST surface

- `api_v1.py`: every route is `/{agents|workspaces}/<agent_id>/...`
  (destroy, ssh, restart, backups, sharing, report, ...) resolving host via
  first-match `get_agent_display_info` / `get_ssh_info` -- wrong-VM actions
  on collision. Routes are internal (frontend + Electron only): coordinated
  rename to host_id, no compat needed.
- `report_collector.py`, `sharing_handler.py`: same pattern via the
  resolver.
- `forward_cli.py` (`EnvelopeStreamConsumer`): builds the collapsed
  `ssh_info_by_agent_id` by flattening the correct `_ssh_by_host_id` --
  delete the flattening; `_services_by_agent`, `_resolver_snapshot_by_agent`
  collapse; membership-delta removal wipes the survivor's services.

### Frontend (no host_id exists anywhere today)

- `electron/main.js`: window/bundle identity (`findBundleForWorkspace`),
  `/goto/<id>/` URL builders, session-restore matching
  (`filterRestorableUrls`, regex `^agent-[a-f0-9]{1,64}$`), accent cache,
  `systemInterfaceStatusByAgent` map, restart/stop POSTs,
  `destroying_agent_ids` set.
- `electron/preload.js`, `content-relay-preload.js`: all IPC payloads carry
  `agentId` only.
- `static/chrome.js`, `sidebar*.js`, `backup_health.js`, `sharing.js`,
  `destroying.js`, `creating.js`, `workspace_settings.js`,
  `workspace_backups.js`, `templates/pages/Landing.jinja` (renders one row
  per tuple entry, `querySelector` only updates the first duplicate).

### remote_service_connector

- `migrations/013_workspace_sync.sql`: drop
  `workspace_records_one_active_per_agent_idx` (new migration); remove the
  `SyncActiveAgentConflictError` path in `app.py` `_put_record_once`.
- Tunnel/hostname naming (`truncate_agent_id`, `make_tunnel_name`,
  `make_hostname`): derive from host_id instead. Note: the 16-hex-char
  truncation can collide even on distinct inputs; with globally unique
  host_ids that becomes a rare operator-visible clash instead of a
  guaranteed one. Existing agent-named tunnels recreate on the next sharing
  toggle.
- The in-memory fake in `testing.py` mirrors the active-unique index; update
  it with the migration.

## Gotchas for the implementing agent

- Preserve everything listed as already host-scoped above -- the audit found
  a consistent pattern of correct-by-host_id storage with agent_id lookup
  layers glued on top; the fix is usually deleting the glue, not adding new
  state.
- The staging instance for real-app testing is `~/.minds-staging`
  (`just minds-start`), not `~/.minds` (packaged prod app). Window-restore
  and startup changes need a real quit/reopen check in the actual app, not
  unit replicas.
- The minds release process couples the app, the vendored mngr, and the
  default-workspace-template tag -- see `apps/minds/docs/release.md`. The
  api_v1 route rename, Electron changes, forward routing, and the RSC
  migration must ship together.
- Electron IPC id validation regexes (`^agent-...$`) exist in the preloads;
  they must move to `host-` prefixes or IPC silently drops events.
- `mngr exec` exports `MNGR_AGENT_ID` into agent environments; nothing in
  minds should start treating that as a global key while migrating.

## Open questions for the minds spec

- Upgrade (not restore) window loss: saved Electron session URLs are
  agent-id based and will fail the host-id match once. Accept the one-time
  drop, or ship a one-release mapping shim (resolver can map old agent-id
  URLs while topology is known)?
- RSC index-drop sequencing vs old clients: hard cut with the minds release,
  or keep the index server-side until the release is fully rolled out?
- Gateway routing ownership: whether the desktop client's subdomain
  middleware or the fronted `mngr_forward` server owns each piece of
  `/goto/` + subdomain routing was not fully disentangled by the audit --
  confirm before writing the spec, and coordinate the transition with the
  mngr spec's forward changes.
- Whether `associations_view` and any other sync-record surfaces should
  expose host_ids only, or `(host_id, agent_id)` pairs for display purposes.
