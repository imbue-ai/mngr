Persisted discovery event parsing is now forward-compatible, fixing the downgrade/rollback wedge where an old client hung forever on newer-schema lines in the shared append-only discovery log (mngr-internal#422).

- Discovery event models (and their nested payload models: `DiscoveredAgent`, `DiscoveredHost`, `SSHInfo`, `DiscoveryError`, `DiscoveredProvider`) now ignore unknown fields instead of rejecting the whole line, so additive schema changes cost old readers nothing. `ProviderInstanceConfig` stays strict for user-authored settings.toml; discovery events carry a tolerant `PersistedProviderInstanceConfig` instead.

- `parse_discovery_event_line` skips-and-warns on schema mismatches (including wholly unknown event types from future versions) instead of raising; the new `DiscoverySchemaMismatchWarner` deduplicates the warnings per distinct failure and emits a counted summary for replays, so a poisoned log surfaces as a few clear warnings rather than one per line.

- The regenerate-and-retry recovery paths and `DiscoverySchemaChangedError` are removed: regeneration could never converge while a newer-version writer was still appending, which is exactly how the incident wedged. Per-line skipping plus the existing full-scan fallback replace them.

- `mngr observe`'s discovery stream consumer now warns and continues on an unparseable line instead of crashing the stream.

- Golden forward-compat tests inject an unknown field at every nesting level of every discovery event type and assert parsing survives, plus runtime lock tests that the tolerant configs are never re-tightened.
