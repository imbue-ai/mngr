R2 storage-quota enforcement rework: correct measurement, confirmed downgrades, and a self-service path back under quota.

- The sweep's GraphQL usage query now groups by bucketName only (one row per bucket, 3-hour lookback) and raises on a full page instead of warning into unread cron logs -- the old shape already filled its 5000-row budget at 144 production buckets and would have started silently dropping buckets as the account grew.

- Downgrades are confirmed against the real-time per-bucket REST usage endpoint before any key is flipped; the analytics window peak alone can only screen candidates or restore keys, so a user who just cleaned up is never re-downgraded on stale data.

- New storage-cleanup grants (`POST /account/storage-cleanup-grant`): an over-quota account's downgraded keys are temporarily restored to readwrite so client-side restic forget/prune (which needs full write) can run. Grants settle at `POST /account/storage-recheck` (or at their 60-minute expiry via the sweep); only grants that free no space at all count against a rolling budget of 5 per 24 hours. Backed by the new `r2_cleanup_grants` table (migration 015).

- `POST /account/storage-recheck` also works standalone: it re-measures live usage and restores or downgrades immediately, so nobody waits an hour for the cron.

- `POST /buckets` now also refuses (structured 403, `max_total_bucket_bytes`) when the account's live storage usage is over quota, and freshly minted keys (bucket creation and roll-key's mint-fresh path) come out read-only when the owner is currently enforced-over-quota.

- `POST /admin/sweep/r2` (operator-key authenticated, optional `?email=` scope) runs one sweep pass on demand.

- Per-owner advisory locks serialize every enforcement flip (sweep, grant, recheck), and the sweep skips owners with an active grant so a mid-prune measurement never re-locks them.
