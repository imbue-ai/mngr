# Cloudflare Tunnel Auth: Per-App Access Control and Scoped Agent Credentials

## Overview

- Add per-app access control to the cloudflare_forwarding service using Cloudflare Access (Applications + Policies), with tunnel-level defaults stored in Cloudflare Workers KV
- Add a second auth mechanism using the existing Cloudflare tunnel token as a scoped agent credential -- agents can add/remove/list services on their tunnel but cannot create/destroy tunnels or modify auth settings
- Refactor the endpoint structure back to a single FastAPI app served via `@modal.asgi_app()` so all route logic is testable via `TestClient` with high coverage
- Add a release test that exercises every route against the real Cloudflare API (including Access, KV, and tunnel-token auth), spins up a temporary `cloudflared` connector to verify the Access redirect, then cascading-cleans up all resources
- The service remains stateless -- all persistent data lives in Cloudflare (tunnels, DNS, Access Applications, Workers KV)

## Expected Behavior

### Dual Auth: Admin vs Agent

- Endpoints accept two auth methods, distinguished by the Authorization header format:
  - `Authorization: Basic <base64>` -- admin auth (existing behavior); can do everything
  - `Authorization: Bearer <tunnel_token>` -- agent auth; scoped to one tunnel
- Agent auth decodes the tunnel token (base64 JSON with `{"a": account_id, "t": tunnel_id, "s": secret}`), extracts the `tunnel_id`, and resolves it to a tunnel name
- Agent-authed requests can only call: add service, remove service, list services -- all scoped to the tunnel identified by the token
- Agent-authed requests CANNOT: create tunnels, delete tunnels, set/get auth policies, or access other tunnels
- If a Bearer token is provided but is not a valid tunnel token (malformed base64, missing fields), return 401

### Per-App Access Control via Cloudflare Access

- When a service is added to a tunnel, the service automatically creates a Cloudflare Access Application for the service's hostname with the tunnel's default auth policy applied
- When a service is removed, its Access Application is also deleted (cascade)
- When a tunnel is deleted, all its services' Access Applications are deleted (cascade, as part of existing service cleanup)
- Access is a soft dependency: if the Access API is unreachable or the token lacks permissions, service CRUD still works -- Access Application creation is skipped with a logged warning

### Auth Policy Management

- `GET /tunnels/{tunnel_name}/auth` -- returns the tunnel's default auth policy (from Workers KV)
- `PUT /tunnels/{tunnel_name}/auth` -- sets/replaces the tunnel's default auth policy (admin-only)
- `GET /tunnels/{tunnel_name}/services/{service_name}/auth` -- returns the Access Application policy for a specific service
- `PUT /tunnels/{tunnel_name}/services/{service_name}/auth` -- sets/replaces the Access Application policy for a specific service (admin-only)
- All auth endpoints require admin auth (Basic Auth) -- agents cannot modify auth
- Setting a policy replaces it entirely (the caller provides the complete policy)
- Policy format mirrors Cloudflare's rule-based structure, designed for extensibility:
  ```json
  {
    "rules": [
      {"action": "allow", "include": [{"email": {"is": "alice@example.com"}}]},
      {"action": "allow", "include": [{"email": {"is": "bob@example.com"}}]}
    ]
  }
  ```
- Initially only Google (email-based) rules are supported; the format supports adding other provider types (GitHub username, OIDC groups, etc.) later

### Tunnel Default Auth in Workers KV

- The service auto-creates a Workers KV namespace (named `cloudflare-forwarding-defaults`) on first use
- Tunnel defaults are stored as KV entries with key = tunnel name, value = JSON policy
- When a tunnel is created with a default policy (optional field in `CreateTunnelRequest`), it is written to KV
- When a tunnel is deleted, its KV entry is also deleted (cascade)
- If no default policy is set for a tunnel, new services are created without Access Applications

### Endpoint Refactoring

- Revert from individual `@modal.fastapi_endpoint()` decorators back to a single `web_app = FastAPI()` with standard route decorators (`@web_app.post(...)`, etc.)
- The Modal deployment uses `@modal.asgi_app()` to serve the FastAPI app
- This allows the full route logic to be tested via `starlette.testclient.TestClient` without Modal

### Release Test

- Marked with `@pytest.mark.release`
- Uses the same env vars as the service: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_DOMAIN`, `USER_CREDENTIALS`
- Test flow:
  1. Create a tunnel (admin auth) -- verify tunnel info and token returned
  2. Set a default auth policy on the tunnel (admin auth)
  3. Get the default auth policy -- verify it matches
  4. Add a service (admin auth) -- verify Access Application was created with the default policy
  5. Get the service's auth policy -- verify it matches the tunnel default
  6. Set a per-service auth policy override (admin auth)
  7. Add a service using tunnel-token auth (agent auth) -- verify it works and gets the default policy
  8. List services using tunnel-token auth -- verify both services are listed
  9. Remove a service using tunnel-token auth -- verify it works and Access Application is deleted
  10. Verify tunnel-token auth cannot create/delete tunnels or modify auth (expect 403)
  11. Start a temporary `cloudflared tunnel run` process, make an HTTP request to the service hostname, verify it gets a Cloudflare Access login redirect (302 to the Access login page)
  12. Cascading cleanup: delete the tunnel (admin auth) -- verify all DNS records, Access Applications, ingress rules, and KV entries are removed
- All resources use a unique test prefix (e.g. `test-release-{random}`) to avoid collisions

## Changes

- Refactor `app.py` endpoints from `@modal.fastapi_endpoint()` back to `@web_app.post/get/delete(...)` routes on a FastAPI app, served via `@modal.asgi_app()`
- Add `authenticate_request()` function that returns an auth result indicating admin (with username) or agent (with tunnel_id and tunnel_name) -- replaces the current `authenticate()` which only handles Basic Auth
- Add agent-auth guard logic: endpoints that only admins can access raise 403 for agent auth; endpoints agents can access verify the tunnel matches
- Add Cloudflare Access API functions: `cf_create_access_app()`, `cf_delete_access_app()`, `cf_get_access_app_by_hostname()`, `cf_create_access_policy()`, `cf_update_access_policy()`, `cf_get_access_policies()`
- Add Workers KV API functions: `cf_kv_get()`, `cf_kv_put()`, `cf_kv_delete()`, `cf_kv_ensure_namespace()`
- Extend `ForwardingCtx.add_service()` to create an Access Application with the tunnel's default policy (from KV)
- Extend `ForwardingCtx.remove_service()` to delete the service's Access Application
- Extend `ForwardingCtx.delete_tunnel()` to delete the KV entry
- Add 4 new auth policy endpoints (get/set tunnel default, get/set per-service override)
- Add `CloudflareOps` protocol methods for Access and KV operations
- Add `HttpCloudflareOps` implementations for the new protocol methods
- Add `FakeCloudflareOps` test implementations for Access and KV
- Update `CreateTunnelRequest` to accept an optional `default_auth_policy` field
- Add release test file `test_cloudflare_forwarding.py` with the full end-to-end test
- Update Modal Secret to include Workers KV namespace ID (or let the service auto-create it)
- Update README with new endpoints, auth methods, and env var requirements
