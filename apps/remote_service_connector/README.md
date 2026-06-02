# remote_service_connector

A lightweight service deployed as a Modal Function that connects minds clients to the remote services they need: Cloudflare tunnels today, SuperTokens authentication today, and more remote capabilities (e.g. creating remote hosts on behalf of users) over time. All endpoints are authenticated.

## What it does

Allows authenticated users to:
- Create Cloudflare tunnels (one per host running `cloudflared`)
- Add/remove forwarding rules (ingress + DNS) on those tunnels
- List their tunnels and configured services
- Delete tunnels (cascading cleanup of DNS and ingress)
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

### Paid lists (who counts as "paid")

Paid-feature access is gated on two Neon tables instead of an env-var allowlist:

- `paid_emails` -- exact, full-email matches (e.g. `bob@gmail.com`).
- `paid_domains` -- exact domain matches on the part after `@` (e.g. `imbue.com` matches `alice@imbue.com` but NOT `alice@eng.imbue.com`).

A caller is "paid" when they have a verified SuperTokens email AND that email (or its exact domain) has an active (`is_paid = true`) row in either table. Both tables are managed via the `/paid/*` CRUD endpoints (admin-key authenticated) or the `mngr imbue_cloud admin paid` CLI. Rows are never hard-deleted -- "remove" sets `is_paid = false` so we retain history of when an account stopped paying. The schema is created by `migrations/005_paid_lists.sql`.

On deploy, `minds env deploy` seeds each tier's configured default entries (the `[paid]` block in that tier's `deploy.toml`) into these tables right after migrations. Every tier currently defaults `domains = ["imbue.com"]`. Seeding is **seed-if-absent** (`INSERT ... ON CONFLICT DO NOTHING`), so it sets the initial default but never re-activates an entry an operator soft-removed.

### Cloudflare token requirements for R2

The R2 bucket routes require `CLOUDFLARE_API_TOKEN` to be an **account-owned** token (`cfat_`) -- not a user-owned token (`cfut_`) -- because the connector mints account-owned per-bucket R2 tokens on the user's behalf. The token needs these permissions:

- `Cloudflare Tunnel: Edit`
- `DNS: Edit` (on the tier zone)
- `Access: Apps and Policies: Edit`
- `Access: Service Tokens: Edit`
- `Workers KV Storage: Edit`
- `Workers R2 Storage: Edit` (R2 buckets)
- `Account API Tokens: Edit` (mint/revoke per-bucket R2 keys)

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

### Paid-account gate

`/hosts/*`, `/keys/*`, and `/buckets/*` enforce paid status on top of admin auth: the caller's verified SuperTokens email must have an active row in the `paid_emails` / `paid_domains` tables (see "Paid lists" above), or the request returns 403. If the database lookup fails, the gate fails closed (also 403). Cloudflare forwarding (`/tunnels/*`) is not affected -- any email-verified account can use tunnels.

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

When a default auth policy is set on a tunnel, new services automatically get a Cloudflare Access Application with that policy applied. Per-service overrides replace the inherited policy entirely.

### Buckets (admin only, paid)

R2 buckets give an account remote object storage. Each bucket is isolated (one per host the user makes); isolation is per-bucket, not per-prefix. Buckets are named `<user_id_prefix>--<slug>` where `user_id_prefix` is the caller's 16-hex SuperTokens prefix; the server re-checks that prefix in code (not just via the R2 `name_contains` filter) so a crafted name cannot grant cross-user access. All routes require admin auth + a paid account.

- `POST /buckets` -- Create a bucket and mint its default key. Body: `{"name": "...", "access": "read"|"readwrite"}`. Returns `{bucket, key}` where `key` includes the one-time `secret_access_key`. Errors `409` if the derived bucket already exists or the per-account cap (50) is reached, `400` on an invalid derived name.
- `GET /buckets` -- List the caller's buckets.
- `GET /buckets/{name}` -- Bucket metadata (full R2 name + S3 endpoint). Keys come from the key routes.
- `DELETE /buckets/{name}` -- Destroy a bucket. Returns `409` if the bucket is not empty (empty it first); on success, cascades -- revokes all of the bucket's keys and deletes their rows.
- `POST /buckets/{name}/keys` -- Mint an additional scoped key. Body: `{"alias": "...", "access": "read"|"readwrite"}`. Returns the key material (with the one-time secret).
- `GET /buckets/{name}/keys` -- List the caller's keys for one bucket (no secrets).
- `GET /bucket-keys` -- List all of the caller's keys across every bucket (no secrets).
- `DELETE /bucket-keys/{access_key_id}` -- Revoke a key by its Access Key ID and drop its row.

Each key is an account-owned Cloudflare API token scoped to the one bucket; the S3 Access Key ID is the token id and the Secret Access Key is the SHA-256 of the token value (returned once, never stored). Only key *metadata* (access key id, owner, bucket, scope, alias, created_at) is persisted, in the `r2_keys` table; buckets themselves are listed straight from the R2 API.

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
