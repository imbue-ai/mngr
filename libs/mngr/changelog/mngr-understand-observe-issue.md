## Discovery snapshots now carry per-provider state

- `FullDiscoverySnapshotEvent` (the JSONL event emitted by `mngr observe --discovery-only`) has two new fields: `providers` (providers that loaded successfully) and `error_by_provider_name` (providers whose discovery raised). Old snapshots parse cleanly (the new fields default to empty); new snapshots will trip `DiscoverySchemaChangedError` in older builds of `mngr_forward` / `mngr_latchkey` / `mngr_notifications` until those are rebuilt.
- `mngr observe --discovery-only` now emits a `FullDiscoverySnapshotEvent` on every poll, even when zero providers succeeded. Per-provider failures land in `error_by_provider_name`; consumers treat the snapshot as authoritative and drop any previously-known agents/hosts whose provider is now errored.
- `mngr list` no longer skips its side-effect snapshot when some providers failed; the snapshot now includes the per-provider error info. Snapshots are still skipped when a non-provider-attributable error happens at the top level of `list_agents`.
- Bug fix: the outer `mngr observe` (the multi-host observer) used to spawn its inner `mngr observe --discovery-only` child with an unsupported `--on-error continue` flag, killing the child on every startup. The flag is now gone.

## New `UNKNOWN` agent / host lifecycle state

- `AgentLifecycleState` and `HostState` both grow an `UNKNOWN` value, defined as "the provider that owns this agent/host could not be accessed during the most recent discovery attempt."
- `AgentObserver` now emits an UNKNOWN entry in its `FullAgentStateEvent` for any previously-observed agent whose provider just failed discovery (sticky: agent stays UNKNOWN until it reappears in a snapshot or its provider is removed from config). Agents whose provider falls out of configured set entirely are dropped from tracking instead.
- `mngr list` does NOT show UNKNOWN -- it remains stateless and only shows what its own listing returned.

## Retry semantics

- The discovery polling path no longer retries failures at the top level. Providers are responsible for retrying their own transient failures before raising; the snapshot reflects whatever they reported.
