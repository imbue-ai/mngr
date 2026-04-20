# Cloudflare Forwarding Shim

## Overview

- A new standalone app (`apps/cloudflare_forwarding`) deployed as a Modal Function that provides HTTP endpoints for managing Cloudflare Tunnels and their ingress rules on behalf of authenticated users
- Wraps the Cloudflare API (tunnel CRUD, ingress configuration, DNS record management) behind a simple authenticated API so users can request forwarding without needing direct Cloudflare access
- Users authenticate via HTTP Basic Auth against a credential list stored as a Modal Secret; each user can only see and manage tunnels whose name contains their username
- After creating a tunnel, users receive a tunnel token they can pass to `cloudflared tunnel run --token <TOKEN>` on their host to establish the connection
- Intentionally has no dependencies on the rest of the monorepo at runtime -- the deployed code is self-contained (only `modal`, `fastapi`, and `httpx` for Cloudflare API calls)

## Expected Behavior

### Authentication
- Every request requires HTTP Basic Auth credentials
- Credentials are validated against a JSON object stored in the `USER_CREDENTIALS` env var (e.g. `{"alice": "secret1", "bob": "secret2"}`)
- Invalid or missing credentials return 401

### Tunnel Management
- **Create tunnel** (`POST /tunnels`): takes `agent_id` as input; tunnel name is auto-generated as `{username}--{agent_id}`; idempotent -- reuses existing tunnel if one with that name already exists; returns the tunnel token for `cloudflared`
- **List tunnels** (`GET /tunnels`): returns all tunnels belonging to the authenticated user (filtered by username in tunnel name), with their configured services
- **Delete tunnel** (`DELETE /tunnels/{tunnel_name}`): cascading delete -- removes all CNAME DNS records for the tunnel's services, clears the ingress configuration, then deletes the tunnel itself; only the owning user can delete

### Service (Ingress Rule) Management
- **Add service** (`POST /tunnels/{tunnel_name}/services`): takes `service_name` and `service_url` (e.g. `http://localhost:8080`); creates a CNAME DNS record pointing `{service_name}--{agent_id}--{username}.{domain}` to `{tunnel_id}.cfargotunnel.com`; updates the tunnel's ingress configuration to include the new hostname->service mapping; the ingress config PUT is a full replacement, so the endpoint reads the current config, appends the new rule, and writes it back
- **Remove service** (`DELETE /tunnels/{tunnel_name}/services/{service_name}`): removes the CNAME DNS record and the corresponding ingress rule from the tunnel configuration
- **List services**: included in the `GET /tunnels` response -- each tunnel lists its configured ingress rules

### Cloudflare API Integration
- All Cloudflare API calls use a Bearer token from the `CLOUDFLARE_API_TOKEN` env var
- Required env vars: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_DOMAIN`
- Cloudflare API errors are passed through directly to the caller in the HTTP response
- Tunnel token is fetched via `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/token` after tunnel creation

### Deployment
- Modal app is named `cloudflare-forwarding`
- Deployed via `modal deploy` with secrets configured as Modal Secrets
- Endpoints exposed via `@modal.fastapi_endpoint()`
- Minimal image: `modal.Image.debian_slim()` with only `fastapi[standard]` and `httpx` installed

## Changes

- Create `apps/cloudflare_forwarding/` directory with standard monorepo app structure (`pyproject.toml`, `README.md`, `imbue/cloudflare_forwarding/` package)
- Create the Modal app module with a FastAPI app containing the tunnel and service CRUD endpoints
- Create a thin Cloudflare API client (using `httpx`) that wraps the four needed operations: create/delete/list tunnels, get tunnel token, get/put tunnel configuration, create/delete/list DNS records
- Create auth middleware or dependency that validates HTTP Basic Auth against the credentials env var
- Add the app to the monorepo workspace in the root `pyproject.toml` (workspace members, coverage config)
- Add tests using monorepo primitives/fixtures where useful, but keep the app's runtime code dependency-free
