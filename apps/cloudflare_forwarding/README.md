# cloudflare_forwarding

A lightweight service deployed as a Modal Function that wraps the Cloudflare tunnel API behind authenticated HTTP endpoints.

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

The committed `.minds/template/*.sh` files declare the expected keys for each service. Per-env secrets live at `.minds/<env-name>/<service>.sh` and are gitignored; to bootstrap a new env, copy the template into it:

```bash
cp -r .minds/template/ .minds/production/
```

Each file is shell-style:

```sh
# .minds/production/cloudflare.sh
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ACCOUNT_ID=...
# ...
```

Push them to Modal with:

```bash
uv run scripts/push_modal_secrets.py production
```

This creates/updates Modal secrets named `<service>-<env>`, e.g. `cloudflare-production` and `supertokens-production`. The push aborts with a diagnostic if any per-env file is missing a key declared in the template -- so if you add a new secret to the template, existing envs must declare it (empty values are fine) before they can be pushed again.

**cloudflare.sh** holds the Cloudflare API credentials:

- `CLOUDFLARE_API_TOKEN` (required): API token with Tunnel Write and DNS Write permissions.
- `CLOUDFLARE_ACCOUNT_ID` (required): Cloudflare account ID.
- `CLOUDFLARE_ZONE_ID` (required): Cloudflare zone ID for DNS records.
- `CLOUDFLARE_DOMAIN` (required): Base domain for service subdomains (e.g. `example.com`).
- `CLOUDFLARE_ALLOWED_IDPS` (optional): Comma-separated list of Cloudflare identity provider UUIDs allowed on Access Applications (e.g. Google OAuth, one-time PIN). When unset, Cloudflare uses the account default.

**supertokens.sh** holds the SuperTokens + OAuth credentials:

- `SUPERTOKENS_CONNECTION_URI` (required): URL of the SuperTokens core.
- `SUPERTOKENS_API_KEY` (required for most deployments): SuperTokens core API key.
- `AUTH_WEBSITE_DOMAIN` (optional): Public base URL embedded in password-reset and email-verification links. Must match the URL Modal assigns to the deployed function. If unset, the app derives `https://{workspace}--cloudflare-forwarding-<env>-fastapi-app.modal.run` (using the hardcoded default workspace in `app.py`), which is only correct for that specific Modal workspace -- set this explicitly for every deploy.
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (optional): override Google OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` (optional): override GitHub OAuth client credentials. Leave blank to inherit from the SuperTokens core's dashboard.

### 2. Deploy the Modal app

```bash
scripts/deploy_cloudflare_forwarding.sh production
```

The script sets `MNGR_DEPLOY_ENV=production` in the shell that runs `modal deploy`, which is read at module level by `app.py` to pin the secret names (`cloudflare-production`, `supertokens-production`) and also baked into a `Secret.from_dict` so the container can read `MNGR_DEPLOY_ENV` at runtime. Running `modal deploy` directly without the wrapper defaults to `production`.

## Authentication

All non-`/auth/*` endpoints require a Bearer token:

- **Agent (tunnel token)**: `Authorization: Bearer <tunnel_token>` — scoped to a single tunnel. Can add/remove/list services on that tunnel only; cannot create/delete tunnels or manage auth policies.
- **User (SuperTokens JWT)**: `Authorization: Bearer <access_token>` — the signed-in user's SuperTokens session. Treated as an "admin" auth whose username is the first 16 hex chars of the user's SuperTokens user ID (used to namespace tunnels per user).

The `/auth/*` endpoints are themselves the authentication flow, so they do not require a token.

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
