# Clean up leased OVH hosts in the remote connector service

## Overview

- Today, releasing a lease only flips `pool_hosts.status` to `'released'` and sets `released_at` (`app.py:release_host`). Nothing cancels the OVH VPS or strips its tags, and the lease query only matches `status = 'available'`, so released hosts leak forever and never return to the pool.
- The OVH VPS keeps its `minds_env` and `mngr-host-id` tags after release, which confuses env operations and the recycle path. We must strip everything except `mngr-provider=ovh` so a host is cleanly recyclable by the next bake.
- We do the cleanup with **direct OVH REST calls** (no `mngr` in the image): the release route runs the full cleanup chain inline (best-effort), and a new hourly per-tier Modal cron mops up anything left behind by a crash/timeout.
- The cleanup uses a "two-phase commit" style, idempotent chain keyed off a new `removing` status: **mark `removing` → strip tags → cancel in OVH (`deleteAtExpiration`, by service name) → delete row.**
- A one-off, broad runbook (a rewrite of `cleanup_released_hosts.py`) normalizes + cancels the existing live OVH hosts (8 today) across all tiers so they become reusable for future pool-host bakes.

## Expected behavior

- **Releasing a host** now, in one inline best-effort pass:
  - Flips the row to `removing` (durable intent marker).
  - Deletes the OVH IAM tags `minds_env` and `mngr-host-id`, keeping `mngr-provider=ovh`.
  - Cancels the OVH VPS by service name (`renew.deleteAtExpiration=true`) using the row's stored `vps_instance_id`.
  - Deletes the `pool_hosts` row.
- **Release response semantics:** returns `200` as soon as the status has reached `removing` (from the user's perspective the release succeeded; the cron finishes any remaining OVH work). Any failure *before* `removing` is committed (lookup, ownership, the DB mark) returns `5xx` so the client retries.
- **Ownership + paid-account checks** are unchanged: a release for a row owned by another user still returns `403`; non-paid accounts still `403`.
- **Idempotent retries:** a repeat release on a row that is already gone (deleted) returns `200` with `already_released`. A release on a row already in `removing` re-drives the remaining steps and returns `200`.
- **The end state of a cleaned host** is an OVH VPS that is cancelled (`deleteAtExpiration=true`) and carries only `mngr-provider=ovh` — exactly the shape the OVH recycle path reuses, so it is available for future bakes without new billing.
- **The hourly cron** (per deployed tier) finds every `pool_hosts` row in `removing` and re-runs strip-tags → cancel → delete-row for each, treating host-not-found / already-cancelled as success. It processes all such rows each run and logs per-host failures (which it will retry next hour). It does not touch `available`, `leased`, or legacy `released` rows.
- **No behavior change to leasing:** the lease query still matches `status = 'available'`; recyclable cancelled hosts re-enter the pool only via a new bake, not by being re-marked available.
- **Lessee SSH key:** not explicitly removed — the recycle/rebuild wipes the VPS disk (and its `authorized_keys`). Documented assumption.
- **One-off runbook:** an operator runs the rewritten script with a dry-run that lists every `mngr-provider=ovh` VPS it would normalize+cancel (cross-referenced against the tier DBs it can reach), then `--yes` to act. Run broadly across dev/staging/production to clear the current backlog.
- **Observability:** loguru logging only — info per cleanup action, warning on per-host OVH failures the cron will retry.

## Changes

### Connector OVH cleanup core (`apps/remote_service_connector/imbue/remote_service_connector/app.py`)
- Add the official **`ovh`** package to the Modal image `pip_install` list and to the connector's pip deps.
- Add OVH config plumbing: read OVH AK/AS/CK (+ default `ovh-us` endpoint, region `us`) from environment, build a signed OVH client.
- Add an **`OvhOps` Protocol** (mirroring the existing `CloudflareOps` pattern) with an HTTP implementation and the operations needed: delete a single IAM tag (`DELETE /v2/iam/resource/{urn}/tag/{key}`), set `deleteAtExpiration` (`GET`/`PUT /vps/{service}/serviceInfos`), and (for the runbook) list VPS IAM resources with their tags.
- Add **pure functions** that implement the cleanup chain for a single host given its row data: strip non-`mngr-provider` tags, cancel by service name, with idempotent handling of 404 / already-cancelled. These are the single source of truth shared by the release route, the cron, and the runbook.
- Build the VPS URN from the service name and region (`urn:v1:us:resource:vps:<service>`).

### Release route (`app.py:release_host`)
- Replace the single `status='released'` update with the inline chain: ownership/paid checks → set `status='removing'` (commit) → strip tags → cancel in OVH → delete row.
- Return `200` once `removing` is committed; `5xx` if anything before that fails.
- Treat a missing row on repeat calls as `already_released` (`200`).

### New periodic cron (`app.py`)
- Add a Modal scheduled function (`@app.function(schedule=modal.Cron("0 * * * *"))`, UTC) on the existing connector app/image, wired to the same `neon-<tier>` and new `ovh-<tier>` secrets.
- Select all `pool_hosts` rows with `status='removing'` using `FOR UPDATE SKIP LOCKED`, and run the shared cleanup chain for each, then delete the row. Log per-host outcomes.

### Status model / DB
- Introduce a new `removing` status value (the `status` column is free-form `TEXT`, so no schema migration is required). Release sets `removing` instead of `released`.
- Reuse the existing `released_at` timestamp column for the `removing` transition (no new column).
- No grace-period gating on the cron — rely on idempotency + `SKIP LOCKED`.

### OVH credentials wiring (`apps/minds/imbue/minds/envs/...` deploy path)
- Add an **`ovh`** service to the per-env secret list pushed during `minds env deploy`, creating an `ovh-<tier>-<deploy_id>` Modal secret from Vault `secrets/minds/<tier>/ovh` (AK/AS/CK).
- Add that secret to the connector's `@app.function` secrets list (for both the web app and the cron function).

### Runbook (rewrite `apps/remote_service_connector/scripts/cleanup_released_hosts.py`)
- Remove the `mngr destroy` subprocess approach; import the pure cleanup functions from `app.py`.
- Operate broadly: tag-scan every `mngr-provider=ovh` VPS in the account (including ones missing `mngr-host-id`) and cross-reference the reachable tier DB(s) to also catch `released`/`removing`/orphaned rows.
- Discover DBs from Vault `secrets/minds/<tier>/neon` for the tiers passed on the CLI (dev = the operator's active dev env DB).
- Provide a dry-run (default) that prints the full plan and a `--yes` flag to execute. Normalize (strip non-`mngr-provider` tags) + cancel + delete any matching DB rows, leaving hosts recyclable.

### Tests
- Add an `OvhOps` fake implementation alongside the existing Cloudflare test doubles; unit-test the release chain (status transitions, response codes, idempotency, ownership/paid gates) and the cron selection/cleanup against the fake (no real OVH/DB).

### Changelog
- Add per-PR changelog entries for `apps/remote_service_connector` and `apps/minds` (and `dev/` if any root-level files change).

## Notes / open items

- "Broad across all tiers" (the runbook) assumes nothing live currently exists in staging/production pools. The runbook still surfaces a dry-run and cross-references reachable tier DBs so the operator can eyeball before `--yes`; it does not hard-block on `available`/`leased` rows. Worth a sanity check before running against staging/production if their pools are ever populated.
- The 9th OVH VPS (`vps-eec8860b`, plan `*.LZ`, west DC, no mngr tags) is intentionally out of scope.
