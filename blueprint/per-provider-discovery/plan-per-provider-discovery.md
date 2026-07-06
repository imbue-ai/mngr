# Per-provider discovery (no more full snapshots)

## Refined prompt

we want to make provider discovery more robust for mngr -- mngr needs to STOP having the "full_snapshot" thing, and *only* ever work per-provider

Otherwise a hung / slow provider (during discovery) can cause everything to hang.
This is particularly bad and brittle, and we really want to avoid this failure mode.

This matters in our Minds app in particular, and we'll want to update the logic there to account for the new style of discovery (per-provider)

Ideally we move away from having the full discovery snapshots at all (since they're a bad pattern where a single provider can get stuck and cause all providers to be stuck)

* Replace the global `FullDiscoverySnapshotEvent` with a **per-provider snapshot event**, emitted independently as each provider finishes (keep a per-provider reset point; drop the global snapshot entirely)
* Bound hung providers *without killing threads* (threads can't be killed): warn after a first threshold, then emit a per-provider `DiscoveryError` after a second, longer timeout while the abandoned thread keeps running
* Poll each provider on its **own decoupled loop**, with poll frequency dynamically configurable per provider
* Apply the per-provider + dual-timeout treatment to **both** the streaming/observe pipeline and the one-shot paths (`mngr list`, `discover_hosts_and_agents`); an effectively-infinite final timeout reproduces today's "wait for all providers" behavior
* Add a **shared consumer-side reconciler** in mngr that folds per-provider snapshots into accumulated state; rewrite all five consumers (`mngr observe`, `mngr_forward`, `mngr_latchkey`, minds, forever-claude-template's `system_interface`) to use it
* Fix a related consistency bug: a single host/agent state-change event arriving *during* a provider's discovery span can be clobbered by the older snapshot; aggregators must record when each provider's discovery *started and finished* and refuse to apply snapshot state for any item that changed within that span
* When discovery observes such an intervening event during a provider's span, it should immediately re-run that provider's discovery on completion, to converge to a consistent state
* Add per-host and per-agent discovery timeouts, constrained to be set *below* the provider error timeout (raise otherwise); accepts that a slow host can otherwise hold up that provider's discovery
* Cover the intervening-event-during-discovery-span race with explicit tests
* Reconciler rule: each snapshot carries `discovery_started_at`/`discovery_finished_at`; the reconciler tracks the last-seen event time per host/agent and keeps the *event's* value for any item that changed at/after the snapshot's start
* A host/agent that exceeds its sub-provider timeout is marked **explicitly UNKNOWN** in the snapshot (distinct from a destroyed host or a fully-errored provider)
* Span-aware reconciliation lives in the shared reconciler and applies to all discovery state-change/destroy events
* Defaults: poll interval **30s**, provider warn **20s**, provider error timeout **120s**, host & agent discovery timeouts **30s** (host/agent validated below the provider *error* timeout, not below warn)
* Remove `DISCOVERY_FULL` / `FullDiscoverySnapshotEvent` entirely; add `DISCOVERY_PROVIDER` / `ProviderDiscoverySnapshotEvent` (one provider's agents/hosts/error/span)
* The shared reconciler is a stateful `DiscoveryStateAggregator` class in mngr core, delegating span/partition decisions to `@pure` helpers
* Producer model: one `mngr observe --discovery-only` process running one decoupled poll loop (thread) per provider; a provider-set change still bounces the single process
* Cover the reconciler/span/partition logic with unit tests and hung-provider isolation + the race end-to-end with acceptance tests (so CI runs them)
* Scope includes the coordinated forever-claude-template `system_interface/agent_manager.py` rewrite, done on a matching branch in a `.external_worktrees/forever-claude-template` worktree (no manual re-vendor -- that is automated)
* User-facing: a timed-out provider surfaces via the existing `ProviderErrorInfo` path in `mngr list` (same as an unreachable provider); healthy providers still print

---

## Overview

- **Problem:** discovery waits for *all* providers before emitting one merged `FullDiscoverySnapshotEvent`, so one hung/slow provider freezes discovery for everything. `_wait_for_provider_discovery` only logs the pending provider; nothing bounds a hang.
- **Core shift:** drop the global full snapshot entirely. Each provider is discovered, snapshotted, and emitted *independently* as a `ProviderDiscoverySnapshotEvent`, on its own decoupled poll loop. A wedged provider can no longer block any other.
- **Bounding hangs without killing threads:** Python threads can't be force-killed, so each provider poll gets a dual-threshold timeout -- warn at 20s, then emit a per-provider `DiscoveryError` at 120s and move on while the orphaned thread runs to completion (its late result is accepted when it lands). Per-host/per-agent reads get their own 30s timeout (validated below the provider error timeout) so one slow host can't hold its provider's snapshot.
- **Fix a latent clobber bug:** a host/agent state change emitted *during* a provider's discovery span can be overwritten by the older (in-flight) snapshot. Snapshots now carry a `discovery_started_at`/`discovery_finished_at` span; a shared `DiscoveryStateAggregator` refuses to apply snapshot state to any item that changed at/after the span start, and the producer immediately re-polls that provider to converge.
- **One reconciler for everyone:** all five consumers (`mngr observe`, `mngr_forward`, `mngr_latchkey`, minds desktop client, forever-claude-template `system_interface`) today reimplement a global `prior - fresh` diff that assumes "one snapshot = the whole world." They are rewritten to feed events into the shared, span-aware `DiscoveryStateAggregator`, which merges per-provider (never wholesale-replaces across providers).

## Expected behavior

- A hung or slow provider no longer blocks discovery of any other provider -- `mngr list`, `mngr observe`, minds, and the system_interface all keep surfacing healthy providers' agents while one provider is stuck.
- Each provider polls on its own ~30s loop; the cadence (and warn/error/host/agent timeouts) is configurable per provider via `[providers.<name>]`.
- A provider that exceeds its 120s error timeout shows up as a provider error -- in `mngr list` it renders via the existing `ProviderErrorInfo` row / `--on-error` behavior, and its previously-known agents are *retained as stale/unknown* rather than dropped (same retain rule as an unreachable provider today).
- A single host/agent that exceeds its 30s sub-provider timeout appears **explicitly UNKNOWN** in that provider's snapshot -- distinguishable from a destroyed host (gone) and from a fully-errored provider.
- A destroy/state-change event that lands while a provider is mid-discovery is never clobbered by that in-flight snapshot; the provider re-polls immediately on completion so state converges within one extra cycle.
- minds' recovery-redirect freshness gating becomes per-provider (a workspace uses *its* provider's last snapshot time) instead of a single global `last_full_snapshot_at`; minds' existing `discovery_health` watchdog (already keyed on "any event") continues to treat a single slow provider as healthy.
- Clean schema break: old `DISCOVERY_FULL` lines are no longer produced or read. Stale on-disk events trigger the existing auto-regenerate path (`DiscoverySchemaChangedError`), which now writes per-provider snapshots.
- `mngr observe --discovery-only` stays the single producer process and is still bounced (SIGHUP) on a provider-set change; its argv is unchanged so the `mngr_cli_contract` pin holds.

## Changes

### New event + config model (`libs/mngr`)

- Remove `DiscoveryEventType.DISCOVERY_FULL` and `FullDiscoverySnapshotEvent`; add `DiscoveryEventType.DISCOVERY_PROVIDER` and `ProviderDiscoverySnapshotEvent` carrying one provider's `provider_name`, `agents`, `hosts`, optional `DiscoveryError`, the set of host/agent ids marked `UNKNOWN` (sub-provider timeout), and the `discovery_started_at` / `discovery_finished_at` span.
- Add per-provider timeout/cadence fields to `ProviderInstanceConfig`: poll interval (30s), warn timeout (20s), error timeout (120s), host timeout (30s), agent timeout (30s); validate at config-load that host/agent timeouts are below the provider error timeout (raise otherwise). Defaults baked into the field defaults so implicit-default providers inherit them.
- Update `DiscoveredProvider` / `DiscoveryError` usage to be carried per-provider on the new event rather than aggregated.

### Producer: per-provider decoupled loops (`libs/mngr` discover/list/observe/discovery_events)

- Replace the single all-providers poll loop in `run_discovery_stream` with one decoupled per-provider loop (thread) per provider inside the one `mngr observe --discovery-only` process; each loop owns its own cadence and skips overlapping polls (no pile-up).
- Add per-provider discovery with the dual-threshold timeout pattern: warn, then emit a per-provider `DiscoveryError` at the error timeout while the orphaned thread continues; accept and emit the late result when it eventually returns.
- Add per-host/per-agent timeouts during a provider's discovery; a timed-out host/agent is marked UNKNOWN in that provider's snapshot.
- Stamp each provider snapshot with its discovery span; during the span, watch the discovery log for intervening state-change/destroy events touching that provider's hosts, and re-run that provider immediately on completion if any landed.
- Replace `write_full_discovery_snapshot` / `_maybe_write_full_discovery_snapshot` with per-provider snapshot writers; `mngr list` still emits, but one snapshot per provider as a side effect.
- Rework on-disk replay/offset logic (`find_latest_full_snapshot_offset`, `_replay_discovery_events_into_maps`, `_emit_latest_cached_snapshot`) to accumulate the latest snapshot *per provider* instead of resetting all state on each global snapshot.
- Bound the one-shot paths too (`list_agents` / `discover_hosts_and_agents` and their `_construct_and_discover_*` helpers) with the same per-provider timeouts; an effectively-infinite error timeout reproduces today's wait-for-all behavior.

### Shared reconciler (`libs/mngr`)

- Add a stateful `DiscoveryStateAggregator` (implementation class, `MutableModel`) that consumers feed per-provider snapshots and incremental events into and query for accumulated agents/hosts/providers/errors.
- Move retain/drop and span decisions into `@pure` helpers it calls: generalize `partition_removed_agents_by_provider_error` to operate per-provider, and add a span-aware rule that keeps an item's event value over a snapshot value when the item's last-seen event time is at/after the snapshot's `discovery_started_at`.
- Track last-seen event time per host/agent; apply the span rule to all state-change and destroy events.

### Consumer rewrites

- `mngr` `AgentObserver` (`api/observe.py`): replace `_handle_full_snapshot`'s wholesale provider-state replace + host-set diff with the shared aggregator; merge per-provider known/errored sets instead of replacing.
- `mngr_forward` `ForwardStreamManager` and `mngr_latchkey` `DiscoveryStreamConsumer`: replace their `prior - fresh` global diffs with the aggregator; scope per-agent event-stream/tunnel teardown to the single provider a snapshot covers.
- minds `forward_cli.py` + `backend_resolver.py`: feed events into the aggregator; change `update_providers` from wholesale replace to per-provider merge; replace the global `last_full_snapshot_at` with per-provider freshness, and update `app.py` recovery-redirect gating / providers-panel counters / `_is_discovery_fresh` to use per-provider freshness. `discovery_health.py` stays keyed on "any event."
- forever-claude-template `system_interface/agent_manager.py`: rewrite `_handle_full_snapshot` / `_handle_discovery_event` to consume per-provider snapshots via the (re-vendored) aggregator and honor per-provider authority/error retention; make the event-type match exhaustive. Done on a matching branch in a `.external_worktrees/forever-claude-template` worktree; re-vendoring is automated.

### Tests + changelog

- Unit tests for `DiscoveryStateAggregator`, the per-provider partition helper, and the span-aware "don't clobber" rule (including the intervening-event-during-span race).
- Acceptance tests (CI-run) for hung-provider isolation (one stuck provider, others still discovered) and the race end-to-end, plus config-validation tests for the timeout ordering.
- Update the `mngr_cli_contract` argv pin only if the observe argv changes (it should not).
- Changelog entries for every touched project: `libs/mngr`, `apps/minds`, and any touched plugins (`mngr_forward`, `mngr_latchkey`), under each project's `changelog/<branch>.md`.
