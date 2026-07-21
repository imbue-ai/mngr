# remote_service_connector

A lightweight service deployed as a Modal Function that connects minds clients to the remote services they need: Cloudflare tunnels, SuperTokens authentication, pool-host leasing, LiteLLM keys, R2 buckets, and per-account plans/quotas. All endpoints are authenticated, and every resource grant is checked against the account's entitlements (see "Plans and entitlements" below).

## What it does

Allows authenticated users to:
- Create Cloudflare tunnels (one per host running `cloudflared`)
- Add/remove forwarding rules (ingress + DNS) on those tunnels
- List their tunnels and configured services
- Delete tunnels (cascading cleanup of DNS and ingress)
- Lease pre-provisioned pool hosts, mint LiteLLM keys, and create R2 buckets
- See their plan, quotas, and live usage (and switch plans)
- Sign in / sign up via SuperTokens (proxying the SuperTokens core so clients never need its API key)

After creating a tunnel, users receive a token to run `cloudflared tunnel run --token <TOKEN>` on their host.

## Deployment

Deployment is split into two pieces so you can rotate secrets without redeploying code and vice versa.

### 1. Environment-scoped Modal secrets

The committed `.minds/template/*.sh` files declare the expected keys for each service -- they are the schema for the HCP Vault entries at `secrets/minds/<tier>/<service>`. To populate a fresh tier's Vault entry, copy the template into a tmp file, fill in the values, push it to Vault, and shred the local file:

```bash
cp .minds/template/cloudflare.sh /tmp/cloudflare-production.sh
$EDITOR /tmp/cloudflare-production.sh
uv run scripts/push_vault_from_file.py production cloudflare /tmp/cloudflare-production.sh
shred -u /tmp/cloudflare-production.sh
```

Each template file is shell-style:

```sh
# .minds/template/cloudflare.sh
export CLOUDFLARE_API_TOKEN=
export CLOUDFLARE_ACCOUNT_ID=
# ...
```

Push everything to Modal and deploy in one shot:

```bash
eval "$(uv run minds env activate production)"
uv run minds env deploy --yes-i-mean-production
```

`minds env deploy` reads `apps/minds/imbue/minds/config/envs/production/deploy.toml`
for the list of services to push from Vault, creates/updates Modal
secrets named `<service>-<env>` (e.g. `cloudflare-production` and
`supertokens-production`), then runs `modal deploy` for both the
connector and the LiteLLM proxy. The push aborts with a diagnostic if
any Vault entry is missing a key declared by the template (empty
values are fine -- the deploy skips them when pushing to Modal).

**cloudflare.sh** holds the Cloudflare API credentials:

- `CLOUDFLARE_API_TOKEN` (required): API token with Tunnel Write and DNS Write permissions.
- `CLOUDFLARE_ACCOUNT_ID` (required): Cloudflare account ID.
- `CLOUDFLARE_ZONE_ID` (required): Cloudflare zone ID for DNS records.
- `CLOUDFLARE_DOMAIN` (required): Base domain for service subdomains (e.g. `example.com`).
- `CLOUDFLARE_ALLOWED_IDPS` (optional): Comma-separated list of Cloudflare identity provider UUIDs allowed on Access Applications (e.g. Google OAuth, one-time PIN). When unset, Cloudflare uses the account default.

**supertokens.sh** holds the SuperTokens + OAuth credentials:

- `SUPERTOKENS_CONNECTION_URI` (required): URL of the SuperTokens core.
- `SUPERTOKENS_API_KEY` (required for most deployments): SuperTokens core API key.
- `AUTH_WEBSITE_DOMAIN` (optional): Public base URL embedded in password-reset and email-verification links. Must match the URL Modal assigns to the deployed function. If unset, the app derives `https://{workspace}--remote-service-connector-<env>-fastapi-app.modal.run` (using the hardcoded default workspace in `app.py`), which is only correct for that specific Modal workspace -- set this explicitly for every deploy.
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (optional): override Google OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` (optional): override GitHub OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.
- `MINDS_PAID_ADMIN_KEY` (optional): fixed API key authenticating the paid-list admin CRUD endpoints (`/paid/*`). Distinct from every other auth path -- the connector accepts it ONLY on `/paid/*` and rejects SuperTokens / tunnel tokens there, and rejects this key on every other route. Leave empty to disable the paid-list admin API. The `mngr imbue_cloud admin paid ...` CLI reads the same value from `$MINDS_PAID_ADMIN_KEY`.
- `MINDS_PAID_LIST_CACHE_TTL_SECONDS` (optional): how long (seconds) the connector caches a per-email paid-status lookup before re-querying the tables. Unset uses the built-in default (60s); `0` disables caching. Each container caches independently, so a paid-list change propagates within this window.

### Plans and entitlements (quotas)

Resource access is governed by per-account quotas ("entitlements"), not by a paid/unpaid gate:

- The `plans` table holds the plan definitions ("explorer" and "ally" today). It is **git-owned**: `minds env deploy` writes (overwriting) the `[plans]` blocks from the tier's `deploy.toml` after migrations, so deploy.toml is the source of truth for plan defaults.
- The `account_entitlements` table holds one row per account, created lazily on the account's first quota-relevant request. The row's values are copied wholesale from the plan at assignment and are the adjustable source of truth thereafter -- changing a plan's defaults never retroactively changes existing rows.
- Lazy-creation backfill rule: accounts whose SuperTokens `time_joined` predates the feature-ship cutoff get "ally" when their email is paid-listed; every newer account starts as "explorer".
- Quota rejections are HTTP 403 with structured detail: `{"code": "quota_exceeded", "entitlement": "<name>", "limit": N, "current": N, "message": "..."}`.
- Quotas are checked when a resource is *granted* (lease, tunnel, service, bucket, sync record, key). Lowering a quota below current usage never revokes existing resources; the two continuous exceptions are the monthly LLM budget (enforced per-request by LiteLLM user budgets) and R2 storage (enforced by the hourly sweep, see "R2 storage-quota sweep" below).

The quota entitlements: `max_remote_workspaces`, `max_tunnels`, `max_services_per_tunnel`, `max_buckets`, `max_total_bucket_bytes`, `monthly_llm_spend_usd`, `max_active_synced_workspaces`.

### Paid lists (ally-plan eligibility)

The paid lists remain, but only as the eligibility input for selecting the "ally" plan:

- `paid_emails` -- exact, full-email matches (e.g. `bob@gmail.com`).
- `paid_domains` -- exact domain matches on the part after `@` (e.g. `imbue.com` matches `alice@imbue.com` but NOT `alice@eng.imbue.com`).

An email is "paid-listed" when it (or its exact domain) has an active (`is_paid = true`) row in either table. Both tables are managed via the `/paid/*` CRUD endpoints (admin-key authenticated) or the `mngr imbue_cloud admin paid` CLI. Rows are never hard-deleted -- "remove" sets `is_paid = false`. Removing an email from the list does NOT automatically demote an existing ally; that is an operator action via the account admin API. The schema is created by `migrations/005_paid_lists.sql`.

On deploy, `minds env deploy` seeds each tier's configured default entries (the `[paid]` block in that tier's `deploy.toml`) into these tables right after migrations. Every tier currently defaults `domains = ["imbue.com"]`. Seeding is **seed-if-absent** (`INSERT ... ON CONFLICT DO NOTHING`), so it sets the initial default but never re-activates an entry an operator soft-removed.

### Cloudflare token requirements for R2

The R2 bucket routes require `CLOUDFLARE_API_TOKEN` to be an **account-owned** token (`cfat_`) -- not a user-owned token (`cfut_`) -- because the connector mints account-owned per-bucket R2 tokens on the user's behalf. The token needs these permissions:

- `Cloudflare Tunnel: Edit`
- `DNS: Edit` (on the tier zone)
- `Access: Apps and Policies: Edit`
- `Access: Service Tokens: Edit`
- `Workers KV Storage: Edit`
- `Workers R2 Storage: Edit` (R2 buckets)
- `Account API Tokens: Edit` (mint/revoke/roll per-bucket R2 keys)
- `Account Analytics: Read` (the storage-quota sweep's GraphQL usage query)

**R2 must also be enabled on the Cloudflare account** (a one-time dashboard action; until then the API returns `code 10042 "Please enable R2 through the Cloudflare Dashboard"`). Existing tiers shipped with a user-owned tunnel/DNS token and must be migrated (create the account-owned token with the permissions above, replace `CLOUDFLARE_API_TOKEN` in Vault, then redeploy) before the bucket routes work.

### 2. Deploy the Modal app

The previous step (`minds env deploy --yes-i-mean-production`) already
runs `modal deploy` for the connector as part of the unified deploy
flow. If you want to re-deploy just the connector (e.g. after editing
`app.py` without changing any Vault secrets), invoke `modal deploy`
directly:

```bash
MNGR_DEPLOY_ENV=production uv run modal deploy --name remote-service-connector-production \
    --env main apps/remote_service_connector/imbue/remote_service_connector/app.py
```

`MNGR_DEPLOY_ENV` is read at module load by `app.py` to pin the
secret names (`cloudflare-production`, `supertokens-production`).
Running `modal deploy` directly without the wrapper defaults to
`production`.

## Authentication

All non-`/auth/*` endpoints require a Bearer token:

- **Agent (tunnel token)**: `Authorization: Bearer <tunnel_token>` — scoped to a single tunnel. Can add/remove/list services on that tunnel only; cannot create/delete tunnels or manage auth policies.
- **User (SuperTokens JWT)**: `Authorization: Bearer <access_token>` — the signed-in user's SuperTokens session. Treated as an "admin" auth whose username is the first 16 hex chars of the user's SuperTokens user ID (used to namespace tunnels per user).

The `/auth/*` endpoints are themselves the authentication flow, so they do not require a token.

### Quota enforcement

Every resource-granting endpoint checks the caller's entitlements (see "Plans and entitlements" above) on top of admin auth:

- `POST /hosts/lease` -- `max_remote_workspaces` (strict: a per-user advisory lock serializes concurrent leases; stopped workspaces still hold their lease and count).
- `POST /tunnels` -- `max_tunnels` (idempotent re-creates of an existing tunnel are always allowed).
- `POST /tunnels/{name}/services` -- `max_services_per_tunnel` (re-adding an existing service is always allowed; enforced under both admin and agent auth).
- `POST /buckets` -- `max_buckets`, plus `max_total_bucket_bytes` against live REST-measured usage (an account already over its storage quota cannot create new buckets; an unreadable usage number fails open). New keys minted while the owner is enforced-over-quota (bucket creation and roll-key's fresh mint) come out read-only with the downgrade recorded, so a fresh mint can never bypass the sweep.
- `POST /keys/create` -- refused outright when `monthly_llm_spend_usd` is 0 (e.g. the explorer plan); otherwise the account's LiteLLM user-level budget is upserted before minting, so LiteLLM caps aggregate spend across all the account's keys.
- `PUT /sync/records/{host_id}` -- `max_active_synced_workspaces` when the push would create a new ACTIVE record.

### Paid-list admin API (`/paid/*`)

The paid lists are managed by a separate set of endpoints authenticated by the fixed `MINDS_PAID_ADMIN_KEY` (passed as `Authorization: Bearer <key>`). This key is rejected on all other routes, and SuperTokens / tunnel tokens are rejected here. All operations are idempotent; `list` returns every row with its `is_paid` status by default (`?paid_only=true` filters to active rows):

- `GET /paid/domains` / `GET /paid/emails` -- list rows.
- `POST /paid/domains/add` / `POST /paid/emails/add` -- body `{"value": "..."}`; add or reactivate.
- `POST /paid/domains/remove` / `POST /paid/emails/remove` -- body `{"value": "..."}`; soft-delete (`is_paid = false`).

## Identity providers for Access Applications

When `CLOUDFLARE_ALLOWED_IDPS` is set, Access Applications created for forwarded services will restrict authentication to the specified identity providers (e.g. Google OAuth, one-time PIN). This controls how end users authenticate when visiting a tunneled service URL. Set it to a comma-separated list of Cloudflare identity provider UUIDs. You can find these UUIDs in the Cloudflare Zero Trust dashboard under Settings > Authentication.

## API

### Tunnels (admin only)

- `POST /tunnels` -- Create a tunnel. Body: `{"agent_id": "...", "default_auth_policy": ...}`. Returns tunnel info with token.
- `GET /tunnels` -- List your tunnels with their configured services.
- `DELETE /tunnels/{tunnel_name}` -- Delete a tunnel and all its DNS records, Access Applications, ingress rules, and KV entries.

### Services (admin or agent)

- `POST /tunnels/{tunnel_name}/services` -- Add a service. Body: `{"service_name": "...", "service_url": "http://localhost:8080"}`.
- `GET /tunnels/{tunnel_name}/services` -- List services on a tunnel.
- `DELETE /tunnels/{tunnel_name}/services/{service_name}` -- Remove a service, its DNS record, and its Access Application.

### Auth policies (admin only)

- `GET /tunnels/{tunnel_name}/auth` -- Get the default auth policy for a tunnel (stored in Workers KV).
- `PUT /tunnels/{tunnel_name}/auth` -- Set the default auth policy for a tunnel. New services inherit this policy.
- `GET /tunnels/{tunnel_name}/services/{service_name}/auth` -- Get the auth policy for a specific service.
- `PUT /tunnels/{tunnel_name}/services/{service_name}/auth` -- Set/override the auth policy for a specific service.

Every forwarded service gets a Cloudflare Access Application, unconditionally:

- A tunnel created without an explicit default auth policy gets an allow-only-the-owner's-verified-email default.
- Adding a service creates its Access Application (with the tunnel default, or the owner-email fallback) *before* any DNS/ingress exists; if the Access step fails, the add is aborted and rolled back rather than leaving the service publicly reachable.
- Auth-policy writes reject policies with no identity constraint (every rule must name emails, email domains, an IdP login method, or a group). Access service tokens remain supported via the dedicated service-token endpoints.
- Per-service overrides replace the inherited policy entirely.

### Buckets (admin only)

R2 buckets give an account remote object storage. Each bucket is isolated (one per host the user makes); isolation is per-bucket, not per-prefix. Buckets are named `<user_id_prefix>--<slug>` where `user_id_prefix` is the caller's 16-hex SuperTokens prefix; the server re-checks that prefix in code (not just via the R2 `name_contains` filter) so a crafted name cannot grant cross-user access. Each bucket has exactly **one** key; the hourly sweep revokes any extras (newest wins).

- `POST /buckets` -- Create a bucket and mint its single key. Body: `{"name": "...", "access": "read"|"readwrite"}`. Returns `{bucket, key}` where `key` includes the one-time `secret_access_key`. Errors `409` if the derived bucket already exists, `403` (quota) at the `max_buckets` cap, `400` on an invalid derived name.
- `GET /buckets` -- List the caller's buckets.
- `GET /buckets/{name}` -- Bucket metadata (full R2 name + S3 endpoint). Keys come from the key routes.
- `DELETE /buckets/{name}` -- Destroy a bucket. Returns `409` if the bucket is not empty (empty it first); on success, cascades -- revokes all of the bucket's keys and deletes their rows.
- `POST /buckets/{name}/roll-key` -- Return fresh credentials for the bucket's key by rolling its secret in place: same Access Key ID, new Secret Access Key, token policies untouched (so a storage-quota downgrade survives a roll). Mints a fresh key when the bucket has none.
- `GET /buckets/{name}/keys` -- List the caller's keys for one bucket (no secrets).
- `GET /bucket-keys` -- List all of the caller's keys across every bucket (no secrets).
- `DELETE /bucket-keys/{access_key_id}` -- Revoke a key by its Access Key ID and drop its row (recover with roll-key, which then mints anew).

Each key is an account-owned Cloudflare API token scoped to the one bucket; the S3 Access Key ID is the token id and the Secret Access Key is the SHA-256 of the token value (returned once, never stored). Only key *metadata* (access key id, owner, bucket, scope, alias, created_at, enforcement state) is persisted, in the `r2_keys` table; buckets themselves are listed straight from the R2 API.

### R2 storage-quota sweep

An hourly cron (`r2_quota_sweep`) enforces each account's `max_total_bucket_bytes`:

- Usage comes from one Cloudflare GraphQL analytics query per sweep (`r2StorageAdaptiveGroups`, grouped by `bucketName` only, so it returns exactly one row per bucket: the peak snapshot inside a 3-hour lookback). One query covers every bucket, so the sweep's API cost does not scale with bucket count; a response that fills the row budget (possible truncation) fails the cron run loudly instead of enforcing from partial data. The real-time per-bucket REST usage endpoint serves the display path (`GET /account`).
- The GraphQL peak is only a screening filter: before any key is downgraded, the owner is re-measured with the real-time REST usage endpoint (the same source the recheck endpoint reads), so a user who just cleaned up is never re-downgraded on a stale window peak. Restores need no confirmation -- a peak under the limit proves live usage is under it.
- An over-quota account's readwrite keys have their token policies flipped to read-only **in place** -- the S3 credentials are unchanged, so reads keep working while writes fail -- and are restored automatically once the account is back under quota.
- The sweep skips accounts with an active cleanup grant (see below), settles expired grants, and enforces the single-key-per-bucket invariant on every pass.
- `POST /admin/sweep/r2` (admin-key authenticated, like `/admin/accounts/*`) runs one sweep pass on demand; an optional `?email=` query parameter scopes it to a single account. Used operationally and by the deployment tests.

### Storage-cleanup grants

Cloudflare's R2 token model has no delete-without-write permission, and restic's space reclaim (`forget` + `prune`) needs full write access (prune repacks data). So an over-quota account with read-only keys could never reduce its own usage. Cleanup grants close that loop:

- `POST /account/storage-cleanup-grant` (SuperTokens auth) flips all of the caller's downgraded keys back to readwrite and records a grant with the live usage as its baseline. Idempotent: an active grant is returned as-is, and an account with nothing downgraded gets a `not_needed` no-op.
- `POST /account/storage-recheck` re-measures live usage and applies enforcement immediately (restoring or downgrading), settling any outstanding grant. It also works standalone -- a user who freed space some other way does not wait for the hourly sweep.
- A grant settles as *successful* when usage decreased at all versus its baseline. Only unsuccessful grants count against a rolling budget (5 settled-without-decrease grants per 24 hours; a 403 with `code: cleanup_grant_budget_exhausted` past that), so genuine cleanup is unlimited while write-under-cover-of-cleanup abuse is bounded to roughly one sweep interval of writes per burned grant.
- Grants expire after 60 minutes; the sweep settles expired grants as the fallback when the client never rechecked, and skips enforcement for accounts whose grant is still active (a prune transiently *increases* usage while it repacks).

### Account (admin only)

- `GET /account` -- The caller's plan, entitlement values, live usage, and the available plan names. Lazily creates the entitlements row on first touch.
- `POST /account/plan` -- Switch plans. Body: `{"plan": "..."}`. Resets the account's entitlements wholesale to the plan's defaults; re-selecting the current plan is a no-op (idempotent retries never wipe operator-granted bumps). Switching to "ally" requires a paid-listed email (403 with the reason otherwise).

### Account admin API (`/admin/accounts/*`)

Email-addressed operator management of per-account entitlements, authenticated by the same fixed `MINDS_PAID_ADMIN_KEY` as the paid-list CRUD (and exposed as `mngr imbue_cloud admin account ...`):

- `GET /admin/accounts/{email}` -- One account's plan, entitlements, and live usage (lazily creates the row).
- `POST /admin/accounts/{email}/plan` -- Body `{"plan": "..."}`; always resets to the plan's defaults (the operator's way to wipe manual bumps; skips the ally eligibility check).
- `POST /admin/accounts/{email}/quota` -- Body `{"entitlement": "...", "value": N}`; bump a single entitlement.

### Auth

These endpoints front the SuperTokens core so that clients (e.g. the `minds` desktop client) never need the SuperTokens API key. They require `SUPERTOKENS_CONNECTION_URI` (and usually `SUPERTOKENS_API_KEY`) to be configured on the server; otherwise they return 503. All of them are unauthenticated *except* `/auth/session/revoke`, which must be called with the caller's own access token (see below).

- `POST /auth/signup` -- Body: `{email, password}`. Returns status, user info, session tokens, and whether email verification is pending.
- `POST /auth/signin` -- Body: `{email, password}`. Returns status, user info, session tokens, and whether email verification is pending.
- `POST /auth/session/refresh` -- Body: `{refresh_token}`. Returns a new access/refresh token pair.
- `POST /auth/session/revoke` -- Header: `Authorization: Bearer <access_token>`. Revokes every SuperTokens session for the caller's user. The user_id is derived from the access token, so an anonymous caller cannot revoke another user's sessions. Called on sign-out so that access/refresh tokens stored on the client's machine become useless even if copied off-box.
- `POST /auth/email/send-verification` -- Body: `{user_id, email}`. Resends the verification email.
- `POST /auth/email/is-verified` -- Body: `{user_id, email}`. Returns `{verified: bool}`.
- `GET /auth/verify-email?token=...` -- Renders an HTML result page. Used by the link inside verification emails.
- `POST /auth/password/forgot` -- Body: `{email}`. Always returns OK (to avoid account enumeration).
- `POST /auth/password/reset` -- Body: `{token, new_password}`. Consumes a reset token and sets a new password.
- `GET /auth/reset-password?token=...` -- Renders an HTML form. Used by the link inside password-reset emails.
- `POST /auth/oauth/authorize` -- Body: `{provider_id, callback_url}`. Returns the URL to redirect the user to.
- `POST /auth/oauth/callback` -- Body: `{provider_id, callback_url, query_params}`. Exchanges OAuth params for a session.
- `GET /auth/users/{user_id}` -- Returns basic info about a user (email, login provider).
