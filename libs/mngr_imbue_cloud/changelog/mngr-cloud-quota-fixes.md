Storage-cleanup surface for over-quota accounts.

- New CLI commands: `mngr imbue_cloud account cleanup-grant` (temporarily restore storage-downgraded bucket keys so restic forget/prune can run), `mngr imbue_cloud account recheck-storage` (re-measure live usage and apply enforcement immediately, settling any grant), and `mngr imbue_cloud admin sweep r2 [--email <email>]` (operator-key authenticated on-demand R2 sweep pass).

- Connector-client methods for the three new endpoints, plus a typed `ImbueCloudCleanupGrantBudgetError` for the connector's structured `cleanup_grant_budget_exhausted` 403.

- `R2KeyInfo` now carries the connector's `enforced_access` marker (the model previously rejected the field the connector already returns, since FrozenModel forbids extra fields).
