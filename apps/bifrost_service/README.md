# bifrost_service

A Modal-deployed [Bifrost](https://github.com/maximhq/bifrost) LLM gateway backed by Neon.tech PostgreSQL, with a SuperTokens-authenticated management API.

Agents running inside containers send LLM requests with a virtual key (`sk-bf-*`); bifrost enforces per-agent budgets, then forwards the request to Anthropic using the real API key. Humans manage virtual keys through a FastAPI endpoint that authenticates via SuperTokens and proxies to bifrost's admin API.

## What it does

Allows authenticated users to:

- Create a new virtual key scoped to themselves (budget defaults to $100/day).
- List their virtual keys.
- View current usage and remaining budget for a key.
- Update a key's budget.
- Delete a key.

Agents use the virtual key's `sk-bf-*` value with the inference Function as a drop-in OpenAI-compatible endpoint, and get budget enforcement for free. (Rate limits are not configured on virtual keys today; see the spec's Open Questions for context.)

## Architecture

Two Modal Functions in a single Modal app (`bifrost-<env>`):

- **Inference Function (`bifrost_inference`)**: exposed via `@modal.web_server(port=8080)`. Runs the bifrost Go binary directly; Modal routes external traffic straight to bifrost. Agents hit this with `Authorization: Bearer sk-bf-*`.
- **Management Function (`bifrost_management`)**: exposed via `@modal.asgi_app()`. A FastAPI app that validates SuperTokens JWTs, proxies admin calls to a local bifrost subprocess on `localhost:8080`, and enforces owner-scoped naming so users can only manage their own keys.

Both containers share the same Neon PostgreSQL database. The config store (governance data) and logs store (request logs) are in separate Neon databases to reduce load.

### Ownership scoping

Virtual key names are namespaced by the user's SuperTokens ID (first 16 hex chars of the UUID): `{user_prefix}--{short_name}`. The caller only chooses `short_name`; the prefix is always enforced server-side on create, and every read/update/delete refuses to operate on keys whose name doesn't start with the caller's prefix.

## Deployment

Same two-step pattern as `apps/remote_service_connector/`: secrets, then code.

### 1. Environment-scoped Modal secrets

Copy the templates into a new per-env directory:

```bash
cp -r .minds/template/ .minds/production/
```

Fill in the values in `.minds/production/bifrost.sh`. The management Function also needs `supertokens-<env>` from the remote_service_connector deploy (same credentials are reused; there is no separate secret).

Push with:

```bash
uv run scripts/push_modal_secrets.py production
```

This creates/updates Modal secrets named `<service>-<env>`, e.g. `bifrost-production`.

**`bifrost.sh` fields**:

- `ANTHROPIC_API_KEY` (required): the real Anthropic API key. Used by bifrost to forward agent requests. Never seen by agents.
- `BIFROST_ENCRYPTION_KEY` (required): AES key used by bifrost to encrypt virtual-key values at rest. Must stay stable across every deploy sharing the same DB -- rotating it invalidates every stored virtual key.
- `BIFROST_ADMIN_TOKEN` (required): bearer token protecting bifrost's `/api/*` admin routes. The management Function uses it when proxying internally; anyone hitting the inference Function's public URL also needs it to call `/api/*`.
- `NEON_CONFIG_HOST`, `NEON_CONFIG_PORT`, `NEON_CONFIG_USER`, `NEON_CONFIG_PASSWORD`, `NEON_CONFIG_DB` (required): Neon connection for the config store (virtual keys, budgets, etc.).
- `NEON_LOGS_HOST`, `NEON_LOGS_PORT`, `NEON_LOGS_USER`, `NEON_LOGS_PASSWORD`, `NEON_LOGS_DB` (required): Neon connection for the logs store (request history).

### 2. Deploy the Modal app

```bash
scripts/deploy_bifrost_service.sh production
```

The script sets `MNGR_DEPLOY_ENV=production`, which `app.py` reads at module level to pin the secret names (`bifrost-production`, `supertokens-production`) and bake `MNGR_DEPLOY_ENV` into a `Secret.from_dict` for runtime reads.

## Authentication

### Management API (`/keys/*`)

Requires a SuperTokens JWT in `Authorization: Bearer <access_token>`. The user's identity is the first 16 hex chars of their SuperTokens user ID -- the same derivation the remote_service_connector uses, so a given user has the same prefix across both services. Email verification is required.

### Inference API (`/v1/*`)

Requires a `sk-bf-*` virtual key in `Authorization: Bearer <virtual_key>` (or any of the other header forms bifrost accepts: `x-api-key`, `x-bf-vk`, etc.). Bifrost enforces the budget attached to that key.

### Admin API (`/api/*`)

Protected by the `BIFROST_ADMIN_TOKEN` bearer token. The inference Function exposes these routes publicly too -- callers who know the admin token can hit bifrost directly, but this should be treated as an internal credential only.

## Management API

All require `Authorization: Bearer <SuperTokens access token>`.

- `POST /keys` -- Body: `{"name": "<short>", "budget_dollars": <num?>, "budget_reset_duration": "<str?>"}`. Creates a virtual key. Returns the full record including the one-time `value` (`sk-bf-*`). Name is stored as `{user_prefix}--{name}`; the prefix is server-enforced.
- `GET /keys` -- Lists virtual keys owned by the caller.
- `GET /keys/{key_id}/budget` -- Returns current usage and remaining budget.
- `PUT /keys/{key_id}/budget` -- Body: `{"budget_dollars": <num>, "budget_reset_duration": "<str?>"}`. Updates the budget.
- `DELETE /keys/{key_id}` -- Deletes the key.

Budget defaults: `$100` / `1d`. Reset durations follow bifrost's syntax (`1d`, `1h`, `1M`, `1w`, etc.).

## Bifrost binary

Installed via `npm install -g @maximhq/bifrost@<version>` during image build. The npm wrapper pulls the right pre-built binary for the container's platform. Version is pinned in `app.py` via `_BIFROST_NPM_VERSION`; bump manually when needed.

Bifrost uses GORM auto-migration on the Neon databases, so the very first deploy against a fresh Neon project runs the DDL on startup. Subsequent cold starts just open a connection pool.
