# Discovery providers and errors as first-class snapshot state

## Overview

- Today, when any provider's discovery raises (e.g. Modal token missing), the entire `FullDiscoverySnapshotEvent` is skipped and a side-channel `DiscoveryErrorEvent` is emitted. Consumers cannot distinguish "this provider failed" from "no snapshot arrived," and minds works around this by string-matching the error type and silently rewriting the user's settings to disable the offending provider.
- This spec replaces that with first-class per-provider state inside the snapshot itself: every snapshot now carries `providers` (successfully constructed providers) and `error_by_provider_name` (providers whose discovery raised), so every consumer reads reality instead of correlating across a side channel.
- A `FullDiscoverySnapshotEvent` is now emitted on every poll, including the all-providers-failed case. Snapshots are authoritative — consumers drop previously-known agents/hosts for any provider in `error_by_provider_name` rather than retaining stale state.
- The auto-disable-on-auth-error machinery in minds is removed and replaced with a visible providers panel on the landing page, plus a small explicit Enable/Disable toggle. Users see provider state directly and choose what to do about it.
- A new `UNKNOWN` value is added to `AgentState`/`HostState` so `AgentObserver` can emit a meaningful state for previously-tracked agents whose provider just failed, instead of letting them silently vanish from the agent state stream that `mngr_notifications` consumes.

## Expected Behavior

### Discovery snapshot

- Every discovery poll emits exactly one `FullDiscoverySnapshotEvent`, regardless of which providers succeeded or failed.
- A provider whose construction succeeded but whose discovery call raised appears in `error_by_provider_name`. Its agents and hosts do not appear in the snapshot's `agents`/`hosts`.
- A provider whose discovery succeeded (even if some per-host operations were warnings) appears in `providers`.
- Providers that are configured-but-disabled (`is_enabled = false`) do not appear in either snapshot field — discovery does not attempt them.
- When zero providers are configured, an all-empty snapshot is still emitted as a heartbeat.
- `DiscoveredProvider.config` carries base `ProviderInstanceConfig` fields only (`backend`, `plugin`, `is_enabled`, `destroyed_host_persisted_seconds`, `min_online_host_age_seconds`); subclass-specific fields are dropped at serialization.
- The contract is documented: providers are responsible for retrying their own transient failures before raising. No new retry scaffolding is added.
- Snapshots are emitted from both the `list_agents` side-effect path (for unfiltered listings) and the polling path; both now include errors.
- Non-provider-attributable errors during `list_agents` (the top-level `except MngrError`) propagate to the caller and do NOT emit a discovery event. Polling-loop crashes still surface via the existing `DiscoveryErrorEvent`.
- Existing incremental events (`AgentDiscoveryEvent`, `HostDiscoveryEvent`, `AgentDestroyedEvent`, `HostDestroyedEvent`, `HostSSHInfoEvent`, `DiscoveryErrorEvent`) continue to be emitted alongside snapshots; consumers ignore the ones they don't need.
- Pre-update snapshots already on disk continue to parse cleanly (new fields have defaults).
- Older builds of `mngr_forward` / `mngr_latchkey` / `mngr_notifications` will raise `DiscoverySchemaChangedError` on new snapshots until rebuilt — this is the intended loud-failure signal.

### `AgentObserver` and the UNKNOWN state

- `UNKNOWN` becomes a value on both `AgentState` and `HostState`, defined as: "the provider that owns this agent/host could not be accessed during the most recent discovery attempt."
- When `AgentObserver` sees a snapshot whose `error_by_provider_name` contains a provider it was previously tracking agents for, those agents transition to UNKNOWN. Their `host.state` is also set to UNKNOWN (tied 1:1).
- The observer also reacts to incremental `DiscoveryErrorEvent`s for faster signaling between snapshots: an event with `provider_name` set marks that provider's agents UNKNOWN; an event with `provider_name=None` (the polling loop itself crashed) marks every tracked agent UNKNOWN.
- UNKNOWN is sticky: an agent leaves it only when it reappears in a subsequent snapshot, or when explicitly destroyed. There is no automatic "give up" transition.
- If a previously-tracked agent's provider falls out of the configured set entirely (block removed from settings), the observer drops that agent from its tracked set — config-removal is treated as an implicit destroy.
- After `mngr observe` restarts, only agents observed during this observer process's lifetime are UNKNOWN-eligible. The existing `load_base_state_from_history` continues to suppress duplicate "state changed to X" events on first poll after restart, but is no longer used to bootstrap the UNKNOWN-eligible set.
- `mngr list` (the stateless CLI) never shows UNKNOWN. It shows what its own listing returned, with errors per-provider in its existing `result.errors`. UNKNOWN exists only on the observer-derived `FullAgentStateEvent` stream.
- `HostState`'s existing `host.state: HostState | None` typing is preserved; `None` continues to mean "not observed / not applicable." `UNKNOWN` is just additionally available.

### Minds providers panel

- The landing page renders a Providers section listing every configured provider in stable alphabetical order. Categories (healthy, errored, disabled) are interleaved; the status badge differentiates them.
- The `local` provider is always hidden from this panel (always present, always healthy — would be noise). Other surfaces like `mngr list` continue to show it normally.
- Each entry shows: provider name, backend type, status badge (OK / Error / Disabled), the last error verbatim with its `type_name` when errored, and an Enable or Disable button.
- The panel does not show per-provider agent/host counts or per-provider last-discovery timestamps.
- The Disable button appears on every provider in `providers` or `error_by_provider_name`. The Enable button appears on every configured-but-disabled provider.
- Configured-but-disabled providers are enumerated by minds reading its own active settings file directly (only that file, not a merged view); they are not present in the discovery snapshot.
- The page also shows two freshness timers: "time since last discovery event" (any received event) and "time since last full discovery event" (`FullDiscoverySnapshotEvent`). The server pushes the underlying timestamps over the existing SSE channel; JS ticks the elapsed counter locally between pushes.

### Minds toggle interaction

- Clicking Disable writes `is_enabled = false` into minds' active settings.toml under `[providers.<name>]`. If no block exists, one is created with just that key, acting as an override on top of mngr's merged config.
- Clicking Enable writes `is_enabled = true` explicitly (symmetric with Disable).
- After either click, minds calls the existing `bounce_observe()` to SIGHUP `mngr forward`, which restarts `mngr observe` so the change takes effect on the next poll.
- The provider entry shows a transient "waiting for refresh" state immediately on click. It clears optimistically as soon as the next snapshot's `last_full_snapshot_at` is newer than the click timestamp, whether or not the snapshot reflects the intended state.
- There is no client-side timeout fallback. If `mngr forward` dies as a side effect of the bounce, the existing "Forwarding subprocess died" notification covers the failure mode.

### Minds landing page agent list

- Continues to be driven by the discovery snapshot only. UNKNOWN agents (which exist only on the observer's separate `FullAgentStateEvent` stream) do not appear in the landing page agent list.
- The fact that some agents are currently unknown is communicated entirely through the errored-provider entry in the providers panel.

### Auto-disable behavior is gone

- When an `imbue_cloud_<slug>` session is revoked, minds no longer silently rewrites `is_enabled = false` in the settings file. The error appears in the providers panel; the user clicks Disable themselves, or fixes the upstream auth and the provider recovers on the next snapshot.

### `mngr_notifications`

- The watcher continues to fire its existing "agent went to wait for input" notification for both the existing `RUNNING → WAITING` transition and the new `RUNNING → UNKNOWN → WAITING` sequence.
- No new notification class is introduced for UNKNOWN itself; the watcher just bridges past it.

## Changes

### Discovery event schema (libs/mngr)

- Introduce `DiscoveredProvider` and `DiscoveryError` data types alongside the existing discovery event family.
- Extend `FullDiscoverySnapshotEvent` with `providers: tuple[DiscoveredProvider, ...] = ()` and `error_by_provider_name: dict[ProviderInstanceName, DiscoveryError] = {}`.
- Do not add `error_by_agent_id` or `error_by_host_id` to the snapshot — neither is relevant for discovery.
- Delete the FIXME on `_write_unfiltered_full_snapshot` in `libs/mngr/imbue/mngr/api/discovery_events.py`; this work resolves it.

### Discovery emission semantics (libs/mngr)

- Change snapshot emission in `list_agents`'s `_maybe_write_full_discovery_snapshot` side-effect: always emit for unfiltered listings, populated with the new `providers` + `error_by_provider_name` fields.
- Change the polling path (`run_discovery_stream` → `_write_unfiltered_full_snapshot`) to always emit a snapshot on every poll cycle, including the all-failed case.
- Keep the existing `_DISCOVERY_STREAM_POLL_INTERVAL_SECONDS`; existing rotation handles the larger write volume.
- Document the contract that providers retry their own transient failures before raising; add no new retry layer.

### `AgentObserver` (libs/mngr)

- Fix the `--on-error continue` bug in `_start_discovery_stream` (drop the unsupported flag).
- Extend the snapshot handler to consume the new `providers` + `error_by_provider_name` fields and additionally subscribe to incremental `DiscoveryErrorEvent`s.
- Emit UNKNOWN agent state for previously-tracked agents whose provider just failed; tie `host.state = UNKNOWN` 1:1.
- Implement the sticky-UNKNOWN behavior described above (recover only on snapshot reappearance or explicit destroy).
- Implement config-removal-as-implicit-destroy: drop agents from the tracked set when their provider is no longer loaded.
- Restrict UNKNOWN-eligibility to agents observed during this observer process's lifetime; keep `load_base_state_from_history` only for its state-change-detection role.

### Agent and host state enums (libs/mngr)

- Add `UNKNOWN` value to `AgentState`.
- Add `UNKNOWN` value to `HostState`. The existing `host.state: HostState | None` typing and "None means not observed / not applicable" semantics are preserved unchanged.
- Document both new values with the same definition: "the provider that owns this agent/host could not be accessed during the most recent discovery attempt."

### `mngr_notifications` (libs/mngr_notifications)

- Update `watcher.py`'s state-transition matcher to additionally recognize `RUNNING → UNKNOWN → WAITING` as a "went to wait for input" transition.
- Add minimal per-agent memory inside the watcher to bridge the indirect transition (remember "was RUNNING before going UNKNOWN," forget it otherwise).

### Minds resolver and SSE plumbing (apps/minds)

- Extend `MngrCliBackendResolver` to also track providers, per-provider errors, and the two freshness timestamps; expose getters for the templates/SSE endpoint to read.
- Reuse the existing `_fire_on_change` notification path so the existing SSE channel carries the new data alongside agent state. Single unified channel; no new endpoint.
- Extend the existing SSE message payload with new top-level keys: `providers`, `error_by_provider_name`, `disabled_providers`, `last_event_at`, `last_full_snapshot_at`. Clients ignore keys they do not consume.
- Have minds enumerate the configured-but-disabled set by reading its own active settings file directly (not from the snapshot, not from a merged view).

### Minds providers panel UI (apps/minds)

- Add a Providers section to `landing.html` with the layout described in Expected Behavior.
- Hide the `local` provider entry from the panel; show all other configured providers.
- Implement the two freshness timers (server-pushed timestamps, JS-side local tick between pushes).
- Implement the transient "waiting for refresh" state per entry after a toggle click (optimistic clear on next `last_full_snapshot_at`; no client-side timeout).

### Minds toggle helper and endpoint (apps/minds)

- Rename `disable_imbue_cloud_provider_for_account` to a generic `set_provider_is_enabled(provider_name, is_enabled)` covering any provider name.
- Have it always write to minds' active settings file, creating the `[providers.<name>]` block if missing. Enable explicitly writes `is_enabled = true` (symmetric with Disable).
- Migrate all existing callers in the same change. Do not leave a compatibility shim.
- Add the click handler endpoint that calls `set_provider_is_enabled` and then `bounce_observe()`.

### Minds — delete auto-disable machinery (apps/minds)

- Delete `_ImbueCloudAuthErrorDisabler` entirely from `apps/minds/imbue/minds/cli/run.py`.
- Delete the provider-error callback plumbing on `EnvelopeStreamConsumer`: `add_on_provider_error_callback`, `_on_provider_error_callbacks`, `_fire_provider_error`.
- Keep `bounce_observe()` — it is now called by the new toggle endpoint.

### Out of scope

- `mngr_latchkey` is not changed. It continues to consume the incremental discovery events as today.
- The `logger.warning("Discovery error from ...")` in `libs/mngr_forward/imbue/mngr_forward/stream_manager.py` stays unchanged. The over-log policy across the process-tree relay is preserved deliberately.
- The rollout order between the writer (mngr observe with the new schema) and the consumers (mngr_forward, mngr_latchkey, mngr_notifications) is not specified. This lands as a single monorepo PR; users who have a partial install briefly see `DiscoverySchemaChangedError` until they rebuild.

### Operational

- This is intended as a single PR. It needs per-project changelog entries in all five affected projects: `libs/mngr`, `libs/mngr_forward`, `libs/mngr_notifications`, `libs/mngr_imbue_cloud`, and `apps/minds`.
