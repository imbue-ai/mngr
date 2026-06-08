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

Host state **is** already reachable on the front end on demand — the
recovery/host-health page queries it (`app._run_host_health_probe` →
`_build_mngr_host_state_argv` → `mngr list`). So nothing needed for a future
"restore from backup" view is lost by hiding destroyed hosts from the active
list; mngr still persists the destroyed host for its window.

## Proposed fix (not yet implemented)

Thread `host_state` from discovery into `ParsedAgentsResult`, then treat a host
in a terminal `DESTROYED` state as gone:

1. Capture `DiscoveredHost.host_state` per host in the resolver snapshot.
2. Exclude `DESTROYED`-state agents from `list_known_workspace_ids()` (or at
   least from the Landing list + the `read_destroying` "still present" check).

Result: destroyed workspaces drop off the active list, `read_destroying` returns
`DONE` (no more bogus `FAILED`), and the destroyed-host info remains available
for restore via the existing host-state path.

Design choice to settle when implementing: filter `DESTROYED` centrally in the
resolver (everything downstream treats them as gone) vs. expose `host_state` and
let each consumer decide (Landing + destroy-check filter; a future restore view
explicitly includes destroyed hosts). The latter is preferred.

## Not the cause

- Not the gVisor/runsc restart issue (that wedged lima-4's *create/restart*, a
  separate problem).
- Not a leaked/orphaned VM — the Lima VM is genuinely destroyed.
- Not introduced by the host-setup consolidation branch; this is a pre-existing
  minds discovery/destroy-status interaction.
