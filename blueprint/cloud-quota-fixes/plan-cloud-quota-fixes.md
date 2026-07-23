# Plan: R2 storage-quota fixes (sweep correctness, creation gate, cleanup grants)

## Overview

- Fix the R2 storage sweep's silent truncation: the shipped GraphQL query (bucketName + datetime dimensions, 48h lookback) already fills its 5000-row page at 144 production buckets and warns into logs nobody reads. Reshape it to one row per bucket (bucketName-only grouping, `max` over a 3h lookback) and raise on a full page, which then genuinely means "more than 5000 buckets" (`bucketName_in` sharding is the documented future escape hatch, not built now).
- Make downgrades exact and non-flapping: before flipping any key to read-only, the sweep re-measures that owner's buckets with the real-time per-bucket REST usage endpoint. The recheck/restore path reads the same source, so a legitimately-restored user can never be re-broken by the sweep's stale window peak. Restores need no confirmation (window peak under the limit proves live usage is under).
- Give over-quota users a self-service path back under quota: per-account cleanup grants temporarily restore readwrite so the client can run restic forget/prune (which requires full write -- prune repacks; no Cloudflare permission level allows delete-but-not-put). Only grants that fail to decrease usage at all burn a small budget; genuine cleanup is unlimited.
- Close two grant-time holes: check the storage quota when a bucket is created (`POST /buckets`), and mint new tokens pre-downgraded when the owner is currently enforced-over-quota (bucket creation and roll-key's fresh-mint path alike). minds stops retrying terminal quota refusals during backup provisioning.
- Guard the tier configs with a unit test asserting all four `deploy.toml` `[plans]` blocks stay identical (they are byte-identical today, by design).

## Expected behavior

### Sweep (hourly cron, unchanged schedule)

- The usage query groups by `bucketName` only and takes `max(payloadSize + metadataSize)` over a 3-hour lookback: exactly one row per bucket. Measured production cadence is one snapshot per 10-70 minutes (median 30, newest-snapshot age up to ~76 min), so 3h always contains at least one snapshot per bucket.
- A response that fills the row budget raises and fails the cron run (visible failure) instead of logging a warning; unreachable below 5000 buckets.
- A bucket absent from the window still counts as zero usage, which remains restore-only-safe.
- The GraphQL peak is a screening filter only: owners whose peak exceeds their limit are re-measured with the real-time REST usage endpoint (at most `max_buckets` calls each, only for screening-positive owners), and keys are downgraded only when live usage is over the limit. Restores keep using the peak (peak under limit implies live under limit).
- A failed REST read never permits a downgrade (skip that owner, log an error, count it) -- fail open, consistent with "missing data never downgrades".
- The sweep skips all enforcement for an owner with an active unsettled cleanup grant, and settles any expired unsettled grants using its REST-confirmed measurement.
- Every enforcement flip (sweep per-owner, recheck, grant) serializes on the existing per-user advisory-lock pattern, so overlapping runs cannot interleave token-policy writes.

### Bucket creation and key minting

- `POST /buckets` refuses with the structured 403 `quota_exceeded` error (entitlement `max_total_bucket_bytes`) when the owner's live REST-measured usage is over their limit; a failed usage read fails open (creation proceeds, warning logged). The existing `max_buckets` count check is unchanged.
- When the owner is currently enforced-over-quota (any key has `enforced_access = 'read'`), newly minted tokens (bucket creation and roll-key's mint-fresh path) are created read-only with `enforced_access = 'read'`, closing the mint-a-writable-key-while-downgraded hole.
- minds backup provisioning treats the structured quota 403 as terminal: the detached retry loop stops immediately and surfaces the error through the existing creation-error notification and the workspace backups warning (no create-form changes).

### Cleanup grants (new connector endpoints, SuperTokens auth)

- `POST /account/storage-cleanup-grant`: flips all of the caller's downgraded keys back to readwrite (clearing `enforced_access`), records a grant row with the live REST usage as baseline and a 60-minute expiry. Idempotent no-op success (current key states, no grant row) when nothing is downgraded. Refused with a distinct structured 403 (`code: cleanup_grant_budget_exhausted`, with limit/current/retry-after detail) when 5 grants in the rolling 24h window settled without any usage decrease.
- `POST /account/storage-recheck`: works standalone -- re-measures live usage via REST and restores or downgrades the caller's keys accordingly; when an active grant exists, also settles it (success = settled usage strictly below baseline, any decrease counts).
- Grant parameters (60 min expiry, 5 failed per 24h) are module constants, not per-plan entitlements.
- `POST /admin/sweep/r2` (optional `?email=` single-account scope): runs one sweep pass on demand. Gated on the fixed operator key (`MINDS_PAID_ADMIN_KEY`) exactly like the other `/admin/*` routes -- explicitly NOT the SuperTokens `require_admin` path.
- New CLI: `mngr imbue_cloud account cleanup-grant`, `mngr imbue_cloud account recheck-storage`, `mngr imbue_cloud admin sweep r2`.

### minds "free up backup space" flow

- The Accounts page storage row shows a "free up backup space" action when the account is over its storage quota; clicking runs immediately with visible progress (the button is the consent -- no confirmation dialog).
- The flow iterates: request a cleanup grant, then for each trimmable repo (workspaces with a local canonical `restic.env`) forget the oldest half of its snapshots (never the latest), prune, recheck; repeat until under quota or nothing more to trim.
- Buckets the client cannot operate on (no local restic env/password) are skipped and reported by name if the account is still over quota afterward.
- The minds restore path retries once with `--no-lock` when restic fails on a repository-lock write (the failure mode of a read-only key), keeping account state out of `restic_cli`.

### Config guard

- A unit test parses every `imbue/minds/config/envs/*/deploy.toml` `[plans]` table through `PlanQuotasConfig`, asserts the four known tiers are all present, and asserts every tier's parsed plans equal the first tier's, naming the diverging tier/plan/field on failure.

## Changes

### remote_service_connector

- Rework `_R2_STORAGE_GRAPHQL_QUERY` / `parse_r2_storage_graphql_response` / `cf_query_r2_storage_by_bucket`: bucketName-only dimensions, 3h lookback constant, raise a typed error on a full page (delete the truncation warning).
- `run_r2_quota_sweep`: split candidate screening (GraphQL peak) from downgrade confirmation (REST re-measure per screening-positive owner); skip owners with active grants; settle expired grants; new counters for confirms/skips.
- `create_bucket_endpoint`: live-usage storage check before `ops.create_bucket`; `_mint_and_record_key` and roll-key's mint-fresh path take the owner's enforcement state into account.
- New endpoints: `POST /account/storage-cleanup-grant`, `POST /account/storage-recheck`, `POST /admin/sweep/r2` (operator-key auth, optional email filter).
- Migration `015_r2_cleanup_grants.sql`: grant rows (user_id, granted_at, expires_at, baseline_bytes, settled_at, settled_bytes) plus whatever index the rolling-24h budget count needs.
- New `GrantStore` protocol + Postgres implementation alongside `KeyStore`; per-user advisory-lock helper reused around all enforcement flips.
- New structured error `cleanup_grant_budget_exhausted`; README sections for the sweep rework, grants, and the admin sweep trigger.
- Unit tests via the existing mock-ops pattern in `app_test.py` / `testing.py` (query parsing, raise-on-full-page, confirm-before-downgrade, grant lifecycle incl. budget exhaustion and expiry settlement, creation gate, enforced-at-mint).

### mngr_imbue_cloud

- Connector-client methods for grant/recheck/admin-sweep; new typed error for the exhausted grant budget.
- CLI commands `account cleanup-grant`, `account recheck-storage`, `admin sweep r2`; README and `libs/mngr/docs/commands/secondary/imbue_cloud.md` updates.

### minds

- `_provision_backups`: stop retrying on the structured quota 403 (terminal), surface through existing notification/warning paths.
- New trim-flow module driving grant -> forget-oldest-half -> prune -> recheck rounds; `restic_cli` gains forget/prune helpers and the `--no-lock` retry on restore; Accounts page storage row gains the over-quota action with progress reporting.
- `deployment_tests/test_quota_enforcement.py`: live grant-flow test against the CI tier (admin set tiny storage quota, `admin sweep r2 --email` to downgrade, grant, recheck, settle, restore), using the new admin sweep endpoint.
- Config guard test in `imbue/minds/config/data_types_test.py`.

### dev

- `.reviewer/settings.json`: CI poll timeout raised to 900s (already applied in this session; needs its changelog entry).

### Changelog

- One entry per touched project: `remote_service_connector`, `mngr_imbue_cloud`, `minds`, `dev`.
