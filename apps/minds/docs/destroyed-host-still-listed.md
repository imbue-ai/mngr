# Destroyed workspaces still showing up (and destroy reported as "failed")

## Symptom

After destroying a workspace, the minds desktop client may still show the row
and report the destroy as **failed** — even though the underlying host/VM is
genuinely gone (e.g. a Lima workspace whose VM is destroyed and absent from
`limactl list`, with `mngr list` showing `HOST STATE = DESTROYED`).

## Root cause

The destroy itself succeeds. The "failed" marker is a false negative in how the
front end derives destroy status.

- minds runs the destroy as a detached subprocess and **computes** the status
  rather than reading an exit code (`desktop_client/destroying.py:read_destroying`):
  - pid alive → `RUNNING`
  - pid dead **and agent no longer in discovery** → `DONE`
  - pid dead **but agent still in discovery** → `FAILED`
- "still in discovery" = `agent_id in backend_resolver.list_known_workspace_ids()`,
  which filters agents **only by labels** (`workspace` + `is_primary`) — it does
  **not** consider host state (`desktop_client/backend_resolver.py`).
- mngr intentionally keeps a just-destroyed host visible in discovery for a
  window (`default_destroyed_host_persisted_seconds` / per-provider
  `destroyed_host_persisted_seconds`) so you can see that a host *was* destroyed.
- So after the destroy subprocess exits (pid dead), the host is still listed as
  `DESTROYED` for that window → `agent_in_resolver = True` → minds computes
  `FAILED`, and the Landing list keeps rendering the row.

The same gap means the active workspace list can't distinguish a `DESTROYED`
host from a live one.

## Why the host state isn't used (today)

The steady-state discovery snapshot the front end runs on
(`ParsedAgentsResult`) keeps only `agent_ids`, `discovered_agents`
(`DiscoveredAgent` — which has **no** `host_state`), and ssh info. The discovery
stream *does* carry host state via `DiscoveredHost.host_state` (and it's in
`mngr list --format json`), but minds drops it from the snapshot.

Host state **is** already reachable on the front end — the recovery/host-health
page reads it from the resolver (`app._run_host_health_probe` →
`backend_resolver.get_host_state`, sourced from the same passive discovery
snapshot). So nothing needed for a future "restore from backup" view is lost by
hiding destroyed hosts from the active list; mngr still persists the destroyed
host for its window.

## Implemented fix

`host_state` is threaded from discovery into the front end, and a host in a
terminal `DESTROYED` state is treated as gone:

1. `ParsedAgentsResult` carries `host_state_by_host_id` (a `host_id -> HostState`
   map). `EnvelopeStreamConsumer` populates it from the full discovery
   snapshot's `hosts`, and keeps it fresh via the delta events
   (`HostDiscoveryEvent`, and `HostDestroyedEvent` which marks the host
   `DESTROYED` immediately). `parse_agents_from_json` also fills it from the
   `host.state` field of `mngr list --format json` when parsing a list payload
   (now used only in tests; the recovery host-health page reads host state from
   the resolver rather than re-listing).
2. The resolver exposes `get_host_state(host_id)` and a derived
   `list_active_workspace_ids()` that drops agents whose host is `DESTROYED`.
   `list_known_workspace_ids()` is left as the full set so a future
   restore view can still enumerate destroyed workspaces.
3. Every active surface (the Landing list, the workspace chrome list, the
   backup-status panel, and the destroy-status checks that feed
   `read_destroying`) calls `list_active_workspace_ids()`.

Result: destroyed workspaces drop off the active list, `read_destroying` returns
`DONE` (no more bogus `FAILED`), and the destroyed-host info remains available
for restore via the existing host-state path.

We chose to expose `host_state` and let each consumer decide (rather than
filtering `DESTROYED` centrally inside `list_known_workspace_ids()`), so a future
restore view can opt into the full set while every current surface opts into the
active-only set.

## Not the cause

- Not the gVisor/runsc restart issue (that wedged lima-4's *create/restart*, a
  separate problem).
- Not a leaked/orphaned VM — the Lima VM is genuinely destroyed.
- Not introduced by the host-setup consolidation branch; this is a pre-existing
  minds discovery/destroy-status interaction.
