Migrated the minds desktop app's discovery consumption from the removed global discovery snapshot to mngr's new per-provider model, so a single slow or erroring provider no longer disrupts discovery of the others.

- The discovery consumer now folds every observed event into the shared, span-aware `DiscoveryStateAggregator` and pushes its reconciled view into the backend resolver, replacing the old global prior-vs-fresh agent diff. It consumes per-provider `ProviderDiscoverySnapshotEvent`s and ignores the legacy global snapshot.

- Provider state is tracked per provider: a snapshot for one provider no longer erases another provider's error state, and discovery freshness is now recorded per provider. The providers panel's "time since last discovery" counters use the aggregate (max) across providers.

- The workspace recovery redirect's freshness gate now uses the workspace's own provider's last snapshot time (falling back to the aggregate only when the agent's provider is unknown), so a healthy workspace is not held back by an unrelated provider being down.

- Fixed a slow memory leak in the discovery consumer: cached per-host SSH info is now forgotten when a host is removed, instead of accumulating for the lifetime of the session.
