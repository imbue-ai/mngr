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

Deploy with:

```bash
modal deploy apps/cloudflare_forwarding/imbue/cloudflare_forwarding/app.py
```

## Authentication

Endpoints accept two auth methods, distinguished by the Authorization header:

- **Admin (Basic Auth)**: `Authorization: Basic <base64(username:password)>` -- full access to all endpoints.
- **Agent (Bearer token)**: `Authorization: Bearer <tunnel_token>` -- scoped to a single tunnel; can add/remove/list services but cannot create/delete tunnels or manage auth policies.

Credentials are validated against a JSON object in the `USER_CREDENTIALS` env var. Agent tokens are the Cloudflare tunnel tokens returned when creating a tunnel.

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
