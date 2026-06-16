# Discovery provider-error resilience + latchkey config-refresh

Picks up the two loose threads from `specs/discovery-provider-error-resilience.md`
(the workspace-flicker debug). Thread 1 stops consumers from dropping live
hosts/agents on a transient provider discovery error. Thread 2 keeps latchkey's
discovery in lockstep with minds' own observe on mid-session config changes.

## Overview

- A `FullDiscoverySnapshotEvent` is currently treated by every consumer as fully
  authoritative: anything missing fires `destroyed`, even when its provider only
  *errored* this poll. A single errored poll therefore drops live hosts/agents
  whose state is merely unknown.
- Fix is consumer-side: the producer is stateless per poll and omits an errored
  provider's items, so each consumer must keep its own prior state and decline to
  drop items attributed to a provider in `error_by_provider_name`.
- Retention is strictly indefinite while a provider keeps erroring. An item is
  removed only on an explicit destroy event or a successful (non-errored) poll
  that shows it absent. No staleness timer, no cap, no schema change, no new
  lifecycle enum.
- Surfacing reuses the `error_by_provider_name` signal already plumbed to minds'
  providers panel. Minds marks retained-but-unverified rows as stale/unknown but
  keeps them fully interactive; the non-UI consumers (`mngr_forward`,
  `mngr_latchkey`) retain silently with only a debug/trace log.
- Separately, latchkey never refreshes its provider set mid-session. We add a
  lightweight SIGHUP observe-bounce to `mngr latchkey forward` (mirroring
  `mngr forward`) and have minds bounce it on the same triggers it already uses
  to bounce its own observe, so the latchkey gateway's permission/tunnel setup
  no longer lags a stale provider view.

## Expected behavior

### Thread 1 — provider-error retention

- When a provider's discovery raises on a poll, its previously-known
  hosts/agents stay in every consumer's view instead of disappearing.
- A retained item is dropped only when it is explicitly destroyed, or when its
  provider later succeeds and the item is absent from that successful snapshot.
- A provider that succeeds but legitimately returns no hosts still drops them
  immediately (unchanged from today).
- A provider that is silently missing from `providers` entirely (not in
  `error_by_provider_name`) is unchanged and out of scope — its items still drop.
- In the minds desktop UI, a retained-but-unverified agent/host shows a
  stale/unknown indicator while its provider is currently errored, driven by the
  `error_by_provider_name` the resolver already receives.
- Retained-stale items stay fully interactive: clicking / forwarding keeps
  working off the retained mapping; staleness is informational only.
- In `mngr_forward` and `mngr_latchkey`, retention is invisible to the user —
  service mappings and reverse tunnels for an errored provider's agents are kept
  alive (not torn down), with a debug/trace log noting the retention.
- A repeated errored poll never re-drops or re-creates retained items; they
  persist unchanged until destroyed or re-verified by a clean poll.

### Thread 2 — latchkey mid-session refresh

- After a provider is enabled/disabled, or an imbue_cloud account is added on
  signin / removed on signout, latchkey's discovery observer reloads the current
  provider set without waiting for a full minds restart.
- The bounce restarts only latchkey's `mngr observe` child; the shared latchkey
  gateway and all existing reverse tunnels stay up and uninterrupted.
- New *agents* continue to need no bounce (latchkey's observe is unfiltered and
  already sees them on the next poll); the bounce specifically refreshes the
  *provider set*.
- If the detached latchkey forward is not running when a bounce is requested
  (down, or a stale pidfile), the bounce brings it up instead of no-op'ing.
- Minds startup is unchanged (still a full supervisor `restart()`); the new
  bounce path is used only for mid-session changes.
- The startup-ordering race (latchkey spawning before the config write lands)
  is explicitly left as-is for this PR, relying on #1885's stream isolation to
  keep it from flickering minds' UI.
- Minds' own `mngr forward` lifecycle (its `bounce_observe()` no-op-when-dead
  behavior and the Electron preauth-cookie / listening-port handshake) is
  unchanged.

## Changes

### `libs/mngr` (discovery contract)

- Flip the `FullDiscoverySnapshotEvent` docstring in
  `imbue/mngr/api/discovery_events.py`: replace the current "agents and hosts for
  providers in `error_by_provider_name` MUST NOT be retained" wording with the
  new rule — consumers retain errored-provider items from prior state and mark
  them unknown, dropping only on an explicit destroy or a successful poll showing
  absence. Mirror the same correction in the `run_discovery_stream` /
  `_write_unfiltered_full_snapshot*` narration that currently restates the old
  "drop on error" contract.

### `apps/minds` (minds desktop consumer + latchkey bounce wiring)

- In `imbue/minds/desktop_client/forward_cli.py::_handle_full_snapshot`, capture
  the prior agent map (with `provider_name`) before replacing it; for each
  removed agent whose prior provider is in `event.error_by_provider_name`, do not
  fire `destroyed` and keep it in the retained agent state. Apply the same
  errored-provider check to host removal.
- Drive the existing providers-panel error signal through to a per-row
  stale/unknown indicator on retained agents/hosts (reuse the
  `error_by_provider_name` already passed to `resolver.update_providers`); keep
  those rows fully interactive.
- Add latchkey bounce calls at all four sites where minds already bounces its own
  observe: the provider enable/disable toggle in
  `desktop_client/app.py::_handle_provider_toggle`, both imbue_cloud account-write
  paths on signin and the account-unset path on signout in
  `desktop_client/supertokens_routes.py`. Each should bounce latchkey only when
  the corresponding settings write actually changed (matching the existing
  `changed` / return-value gating), and after the config write is finalized.
- Wire minds' access to the latchkey supervisor so those sites can call the new
  `LatchkeyForwardSupervisor.bounce()` (expose / retain the supervisor handle
  created in `cli/run.py`).

### `libs/mngr_latchkey` (forward bounce + consumer retention)

- Add a lightweight SIGHUP observe-bounce to the `mngr latchkey forward` command
  (`imbue/mngr_latchkey/cli.py`), mirroring `mngr forward._install_sighup_handler`:
  a watcher thread bounces only the `DiscoveryStreamConsumer`'s `mngr observe`
  child, leaving the shared gateway and reverse tunnels alive. Repurpose SIGHUP to
  mean "bounce observe" and leave SIGINT/SIGTERM as the shutdown signals.
- Give `DiscoveryStreamConsumer` (`discovery_stream.py`) the ability to restart
  its observe subprocess in place (bounce), reusing its existing spawn path and
  `--events-dir`.
- Add a `bounce()` method to `LatchkeyForwardSupervisor` (`forward_supervisor.py`):
  resolve the live supervised PID (from its info / pidfile) and send the bounce
  signal; if none is running, fall back to `ensure_running()` (start-if-down).
- Apply the retention rule in `discovery_stream.py::_handle_full_snapshot`: snapshot
  the prior provider-attributed agent map before replacing it, and skip the
  destruction callback for any removed agent whose prior provider is in the
  snapshot's `error_by_provider_name` — so the agent's reverse tunnel/permission
  is retained rather than torn down. Log the retention at debug/trace.

### `libs/mngr_forward` (forward consumer retention)

- Apply the retention rule in `stream_manager.py`'s full-snapshot handler:
  before replacing the known-agent set, keep any agent whose prior provider is in
  `error_by_provider_name`, so `resolver.update_known_agents` / `remove_known_agent`
  do not drop it and its service mapping survives the errored poll.
- Retain the corresponding service/tunnel mapping in `resolver.py` rather than
  dropping it for an errored provider's agents. Log retention at debug/trace.

### Tests + changelog

- Per-consumer retention unit tests (minds `forward_cli`, `mngr_forward`
  stream_manager/resolver, `mngr_latchkey` discovery_stream): a snapshot omitting
  an agent whose provider is in `error_by_provider_name` does NOT drop it; a
  subsequent clean (non-errored) snapshot omitting the same agent DOES drop it.
- One changelog entry per touched project: `libs/mngr`, `libs/mngr_forward`,
  `libs/mngr_latchkey`, `apps/minds` (branch-named files under each
  `changelog/` directory).

### Explicitly out of scope

- forever-claude-template's `system_interface` consumer (picks this up later via
  normal mngr vendoring/release).
- The "provider silently absent from `providers`" case.
- The latchkey startup-ordering race.
- Reworking minds' own `mngr forward` respawn / Electron handshake.
- Any staleness timeout, eviction cap, or new discovery-event schema/enum.
