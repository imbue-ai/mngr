# Plan: Track paid users via database tables (replacing the secret env var)

## Overview

- Replace the `PAID_ACCOUNT_SUFFIXES` secret env var (a comma-separated `endswith` allowlist) with two simple Postgres tables in the `remote_service_connector` Neon database: one for allowed domains, one for allowed individual emails.
- A user counts as "paid" when they have a verified SuperTokens email AND that email (or its exact domain) has a matching row with `is_paid = true` in either table.
- Add a small admin CRUD API on the connector (list / add / remove for domains and emails), authenticated by a single fixed API key folded into the existing `supertokens` deploy secret.
- Add a `mngr imbue_cloud admin paid` CLI (under the existing operator-only `admin` group) so the lists can be updated while the app is deployed, using that API key.
- No backwards compatibility is required — everything is redeployed and nothing is live, so the env var, its Vault entry, and the `paid-accounts.sh` template are removed entirely.

## Expected behavior

- **Paid gating (existing routes).** `/hosts/*`, `/keys/*`, and `/buckets/*` stay gated. The gate now consults the two tables instead of the env var; `/tunnels/*` remains ungated.
- **Match rules.** Domains match exactly on the part after `@` (`imbue.com` matches `alice@imbue.com` but not `alice@eng.imbue.com`). Emails match the full address. All values are normalized to lowercase on write and compare.
- **Verified email still required.** An unverified or missing email is rejected before the table lookup, exactly as today.
- **Fail closed.** If the database lookup fails, paid access is denied (HTTP 403).
- **Caching.** Paid-status lookups are cached in-memory with a configurable TTL (default ~60s, via `MINDS_PAID_LIST_CACHE_TTL_SECONDS`; set to 0 to disable for testing). Each Modal container caches independently, so a CRUD change may take up to the TTL to be seen everywhere — this bounded staleness is acceptable.
- **Soft-delete model.** Rows are never hard-deleted. Each row is `(value PK, is_paid boolean, created_at, updated_at)`. "Remove" sets `is_paid = false` and bumps `updated_at`, preserving history so we can later audit when a user/company stopped paying and reclaim lingering resources.
- **Idempotent writes.** "Add" upserts `is_paid = true` (reactivating a previously removed row in place — keeps original `created_at`, bumps `updated_at`). Re-adding an active row or removing an inactive/absent one is a success, not an error.
- **No retroactive teardown.** Removing an entry only affects future gated requests; already-leased hosts, keys, and buckets remain until separately released.
- **CRUD API.** New admin-only endpoints support list, add, and remove for each table. `list` returns all rows with their `is_paid` status and timestamps by default, with an optional paid-only filter. There is no check/lookup operation.
- **Admin auth.** The CRUD endpoints accept only `Authorization: Bearer <MINDS_PAID_ADMIN_KEY>` (constant-time compared); they reject SuperTokens JWTs and tunnel tokens, and the existing routes continue to reject the admin key. The API key alone is sufficient (no extra rate limiting or IP allowlist).
- **CLI.** Operators run `mngr imbue_cloud admin paid domain [add|remove|list]` and `mngr imbue_cloud admin paid email [add|remove|list]`. Domains and emails are managed and displayed separately. The CLI reads the key from `MINDS_PAID_ADMIN_KEY` and targets the configured connector URL.

## Changes

### Database
- Add a new sequential, idempotent migration (`apps/remote_service_connector/migrations/005_paid_lists.sql`) creating two tables — paid domains and paid emails — each with a lowercased value primary key, an `is_paid` boolean, and `created_at` / `updated_at` timestamps.

### Connector service (`apps/remote_service_connector`)
- Remove the `PAID_ACCOUNT_SUFFIXES` env var and its parsing/matching helpers.
- Replace the paid-account check so it derives paid status from the two tables (exact domain match or full-email match on `is_paid = true` rows), keeping the verified-email precondition and the fail-closed behavior.
- Add an in-memory, TTL-bounded cache for paid-status results, configurable via `MINDS_PAID_LIST_CACHE_TTL_SECONDS` (0 disables it).
- Add admin-only CRUD endpoints for paid domains and paid emails (list / add / remove), with soft-delete and idempotent semantics, and a paid-only list filter.
- Add a new auth path that validates the fixed admin API key (`MINDS_PAID_ADMIN_KEY`) for the CRUD endpoints only, isolated from the SuperTokens/tunnel auth used elsewhere.
- Update the connector README to document the new tables, endpoints, env vars, and the removal of `PAID_ACCOUNT_SUFFIXES`.

### Client + CLI (`libs/mngr_imbue_cloud`)
- Add HTTP client methods for the new CRUD endpoints (sending the admin API key).
- Add a `paid` subgroup under the existing `mngr imbue_cloud admin` command, with separate `domain` and `email` subcommands for `add` / `remove` / `list` (with a paid-only filter on list).
- Read the API key from `MINDS_PAID_ADMIN_KEY` and resolve the connector URL the same way other `imbue_cloud` commands do.

### Secrets & deploy
- Remove the `paid-accounts.sh` template, the `secrets/minds/<tier>/paid-accounts` Vault entry, and the `paid-accounts` entry in each tier's `deploy.toml` services list.
- Add `MINDS_PAID_ADMIN_KEY` and `MINDS_PAID_LIST_CACHE_TTL_SECONDS` to the existing `supertokens` secret template / Vault entry / Modal secret, so they ship with the supertokens secret rather than a new service.

### Cleanup
- Remove tests and references tied to `PAID_ACCOUNT_SUFFIXES`; add coverage for the new table-driven gate, cache behavior, soft-delete/idempotency, admin-key auth, and the CLI (per the repo's unit vs integration/acceptance conventions).
- Add the required per-project changelog entries for `apps/remote_service_connector`, `apps/minds` (if touched for deploy config), and `libs/mngr_imbue_cloud`.
