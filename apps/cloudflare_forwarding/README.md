# cloudflare_forwarding

A lightweight service deployed as a Modal Function that wraps the Cloudflare tunnel API behind authenticated HTTP endpoints.

## What it does

Allows authenticated users to:
- Create Cloudflare tunnels (one per host running `cloudflared`)
- Add/remove forwarding rules (ingress + DNS) on those tunnels
- List their tunnels and configured services
- Delete tunnels (cascading cleanup of DNS and ingress)

After creating a tunnel, users receive a token to run `cloudflared tunnel run --token <TOKEN>` on their host.

## Deployment

Requires the following Modal Secrets (env vars):

- `CLOUDFLARE_API_TOKEN`: Cloudflare API token with Tunnel Write and DNS Write permissions
- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID
- `CLOUDFLARE_ZONE_ID`: Cloudflare zone ID for DNS records
- `CLOUDFLARE_DOMAIN`: Base domain for service subdomains (e.g. `example.com`)
- `USER_CREDENTIALS`: JSON object mapping usernames to secrets (e.g. `{"alice": "secret1"}`)
- `CLOUDFLARE_ALLOWED_IDPS` (optional): Comma-separated list of Cloudflare identity provider UUIDs to allow on Access Applications (e.g. Google OAuth, one-time PIN). When set, created Access Applications will only offer these identity providers to users. When unset, Cloudflare uses the account default.
- `SUPERTOKENS_CONNECTION_URI` (optional): URL of the SuperTokens core. Required for the `/auth/*` endpoints.
- `SUPERTOKENS_API_KEY` (optional): API key for the SuperTokens core. Paired with `SUPERTOKENS_CONNECTION_URI`.
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (optional): Google OAuth credentials used by the `/auth/oauth/*` endpoints.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` (optional): GitHub OAuth credentials used by the `/auth/oauth/*` endpoints.
- `AUTH_WEBSITE_DOMAIN` (optional): Public base URL embedded in outbound password-reset and email-verification links. Defaults to `https://cloudflare-forwarding.modal.run`.

Deploy with:

```bash
modal deploy apps/cloudflare_forwarding/imbue/cloudflare_forwarding/app.py
```

## Authentication

Endpoints accept two auth methods, distinguished by the Authorization header:

- **Admin (Basic Auth)**: `Authorization: Basic <base64(username:password)>` -- full access to all endpoints.
- **Agent (Bearer token)**: `Authorization: Bearer <tunnel_token>` -- scoped to a single tunnel; can add/remove/list services but cannot create/delete tunnels or manage auth policies.

Credentials are validated against a JSON object in the `USER_CREDENTIALS` env var. Agent tokens are the Cloudflare tunnel tokens returned when creating a tunnel.

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

### Auth (unauthenticated)

These endpoints front the SuperTokens core so that clients (e.g. the `minds` desktop client) never need the SuperTokens API key. They require `SUPERTOKENS_CONNECTION_URI` (and usually `SUPERTOKENS_API_KEY`) to be configured on the server; otherwise they return 503.

- `POST /auth/signup` -- Body: `{email, password}`. Returns status, user info, session tokens, and whether email verification is pending.
- `POST /auth/signin` -- Body: `{email, password}`. Returns status, user info, session tokens, and whether email verification is pending.
- `POST /auth/session/refresh` -- Body: `{refresh_token}`. Returns a new access/refresh token pair.
- `POST /auth/email/send-verification` -- Body: `{user_id, email}`. Resends the verification email.
- `POST /auth/email/is-verified` -- Body: `{user_id, email}`. Returns `{verified: bool}`.
- `GET /auth/verify-email?token=...` -- Renders an HTML result page. Used by the link inside verification emails.
- `POST /auth/password/forgot` -- Body: `{email}`. Always returns OK (to avoid account enumeration).
- `POST /auth/password/reset` -- Body: `{token, new_password}`. Consumes a reset token and sets a new password.
- `GET /auth/reset-password?token=...` -- Renders an HTML form. Used by the link inside password-reset emails.
- `POST /auth/oauth/authorize` -- Body: `{provider_id, callback_url}`. Returns the URL to redirect the user to.
- `POST /auth/oauth/callback` -- Body: `{provider_id, callback_url, query_params}`. Exchanges OAuth params for a session.
- `GET /auth/users/{user_id}` -- Returns basic info about a user (email, login provider).
