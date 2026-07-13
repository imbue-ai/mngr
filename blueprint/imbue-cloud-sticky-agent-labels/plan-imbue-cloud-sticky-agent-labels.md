# Plan: imbue_cloud sticky agent labels (husk fix)

## Refined prompt

Provider-side fix for the imbue_cloud husk bug: persist last-known agent identity (name + certified_data) per host under the provider's existing `hosts/<host_id>/` state dir, and re-attach it when discovery falls back to lease-only synthesis, so transiently-unreachable workspaces keep `is_primary` and never vanish from the sidebar or 404 on restart.

* Persist to disk (survives app/forward relaunch — the observed 54c9 failure mode); labels/certified_data only, not the full raw listing
* Re-attach in both fallback sites: outer-SSH-unreachable (CRASHED and UNAUTHENTICATED) and the empty-agents synthesis on a successful pass
* Host state stays truthfully CRASHED/UNAUTHENTICATED — cached data restores identity, not liveness (confirmed: no consumer treats certified_data presence as reachability)
* Cache deleted on destroy via the existing `_cleanup_local_host_state` rm-tree
* No minds-side changes — minds trusts providers
* The unreachable-host fallback emits ALL agents cached from the last good pass (not just the lease's single agent), each with its cached certified_data — also preserving the system-services agents minds tracks per host
* Cached certified_data carries a synthetic `"stale": true` marker key so consumers can distinguish cached identity from a live listing
* Cache file is `hosts/<host_id>/last_known_agents.json` (agent_id -> {name, certified_data}), written with `atomic_write` on every successful discovery pass
* Tested with unit tests in `instance_test.py` (existing stub pattern) plus an integration-level test

## Overview

- imbue_cloud discovery currently emits a label-less "husk" agent whenever the outer SSH to a leased host is unreachable (or a successful listing returns zero agents); every consumer that filters on labels — most importantly minds' `is_primary` sidebar/restart guard — silently drops the workspace.
- Fix at the provider so all consumers (`mngr list`, the forward's `--agent-include`, minds) are covered in one place: remember each host's agents from the last successful listing and re-attach that identity in the fallback paths.
- Identity is persisted to disk in the provider's existing per-host state dir so it survives process and app relaunches — the exact scenario (fresh app start into a flaky network window) observed in production.
- Liveness reporting is unchanged: fallback hosts still surface as CRASHED/UNAUTHENTICATED with `failure_reason`; only the agents' identity (names + labels + data.json fields) is restored, marked stale.
- No mngr-core or minds changes; the fix is contained in `libs/mngr_imbue_cloud`.

## Expected behavior

- While a leased host is transiently unreachable (sleep/wifi blip, box outage), discovery keeps emitting the full set of agents last seen on that host, each with its last-known certified_data (labels, type, work_dir, ...) plus `"stale": true`, instead of one bare lease-stub agent.
- minds consequences (no minds changes needed): the workspace keeps `is_primary`, so it stays in the sidebar, `list_known_workspace_ids` keeps it, and the restart endpoint no longer 404s — a restart attempted while the host is truly unreachable now fails at the stop/start step with a real, retryable error message instead of "Unknown workspace".
- System-services agents (and any other non-primary agents on the host) also survive the unreachable window, so per-host tracking built on them keeps working.
- The same re-attachment applies when outer SSH works but the listing contains zero agents (stopped container / empty data.json), and when the failure is an auth rejection (UNAUTHENTICATED) rather than CRASHED.
- `mngr list` during an unreachable window shows the host as CRASHED/UNAUTHENTICATED with its real agents (marked stale via certified_data) rather than a single synthetic agent named by its id.
- First-ever discovery of a host with no cache yet behaves exactly as today (bare lease stub) — no behavior change until one successful pass has been observed.
- Destroying a workspace removes the cached identity along with the rest of the per-host state dir; a later host with a reused name cannot inherit stale identity (host_ids are not reused).
- Cached identity never expires by time: labels are semi-static, and the truthful CRASHED/UNAUTHENTICATED host state remains the liveness signal.

## Changes

- `libs/mngr_imbue_cloud` provider (`providers/instance.py`):
  - After each successful outer-listing pass, persist the discovered agents' identity (agent_id, name, certified_data) to `hosts/<host_id>/last_known_agents.json` via `atomic_write`, every pass.
  - In the outer-SSH-unreachable fallback, replace the single lease-stub agent with all cached agents (name + certified_data + `"stale": true`); keep the lease stub only when no cache exists. Host state logic unchanged.
  - In the empty-agents synthesis after a successful pass, do the same cache-backed substitution.
  - The rich-details path needs no separate change: `_build_offline_details_from_lease` already builds `AgentDetails` from the passed refs, so cached identity flows through automatically.
  - Destroy/delete cleanup is already covered by `_cleanup_local_host_state` removing the host state dir.
- Tests:
  - Unit (in `providers/instance_test.py`, existing stub pattern): successful pass then unreachable pass carries prior labels and full agent set; persistence across a fresh provider instance (relaunch case); UNAUTHENTICATED behaves like CRASHED; empty-agents site; no-cache first-discovery unchanged; stale marker present on cached refs and absent on live ones.
  - Integration-level test exercising a real provider round-trip of persist-then-fallback (marked per repo test-tier conventions).
- Changelog entry in `libs/mngr_imbue_cloud/changelog/` for the branch.
