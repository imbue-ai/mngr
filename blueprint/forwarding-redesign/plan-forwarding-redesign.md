# Plan: Service-per-origin forwarding redesign

## Overview

- Replace the path-prefix muxing architecture (`/service/<name>/` + four transforms) with **service-per-origin**: every service owns a browser origin, locally and on shares.
- Motivation: arbitrary unmodified apps must just work (openvscode is the canonical example — own service worker, root-absolute URLs, multi-origin webviews). The transforms exist only to compensate for the shared origin; deleting the shared origin deletes them.
- Local scheme: shell at bare `agent-<hex>.localhost:8421`; services at `<name>.agent-<hex>.localhost:8421`; anything deeper (`*.<name>.agent-<hex>.localhost`) routes to the same service, so multi-origin apps work with no configuration. TLS/HTTP-2 mode is retained (with extended cert SANs); plain HTTP stays the default.
- Shared scheme: unchanged flat `<name>--<host>--<user>.<domain>` (one-label wildcard-cert constraint). Same two coordinates, two spellings; origin derivation is a pure function of `location.host` in both.
- Sharing becomes two-tier: per-service email lists (narrow grants) plus the `system_interface` list as the master key, implemented as one Cloudflare **Access Group** per workspace referenced by one Access app per service hostname.
- `system_interface` becomes a pure shell web app; the routing that remains is byte-level and Host-header-based: `mngr_forward` locally, cloudflared ingress remotely.
- No backward compatibility; big-bang cutover on one branch.
- Repos touched: **mngr** (modest changes to `libs/mngr_forward`, `apps/minds`), **default-workspace-template** (large deletion), **remote_service_connector** (two-tier Access model; separate repo, specified here as an API contract).

## Expected behavior

### Local (minds desktop)
- The workspace shell loads at `http://agent-<hex>.localhost:8421/` exactly as today.
- Each service panel is an iframe pointed at `http://<name>.agent-<hex>.localhost:8421/` — a real origin the service owns. `href="/api"`, `new WebSocket("/ws")`, and `Set-Cookie: Path=/` are all correct as emitted; nothing rewrites anything.
- A user manually installing an app (e.g. openvscode-server via supervisord + `forward_port.py`) gets a working panel with its own service worker and webviews, unmodified.
- Login happens once per workspace: the existing `/goto/` bridge sets one cookie with `Domain=agent-<hex>.localhost`, valid for the shell and every service subtree at any depth.
- A service that has not yet registered shows `mngr_forward`'s auto-retrying loading page instead of an error.
- Cross-origin panels: the shell's per-tab Refresh uses the existing `src`-reassignment fallback; panel↔shell messaging stays `postMessage` (already the mechanism).
- Layout JSON stays portable: `serviceName` is the source of truth on load; the iframe URL is derived at render time from `location.host`; no origin is ever persisted.
- The TLS/HTTP-2 local mode is kept and extended to the new hostname shapes: certs gain per-workspace wildcard SANs (`*.agent-<hex>.localhost`) plus per-service SANs (`*.<name>.agent-<hex>.localhost`) so nested service origins and their subtrees are covered. Plain HTTP remains the default.

### Shared (Cloudflare)
- Sharing a single service works as today: `https://<name>--<host>--<user>.<domain>` straight to the raw port, no proxy in the path.
- Sharing the workspace (`system_interface`) gives the recipient the full shell with every panel working: each panel resolves its sibling hostname by string-swapping the first `--` token of `location.host`.
- Grants are two-tier:
  - Adding an email to a service's list grants that service only.
  - Adding an email to the `system_interface` list (the workspace Access Group) grants every service, including ones registered later.
- A service registered while the workspace is shared automatically gets a DNS record, an ingress rule, and an Access app referencing the group — reachable by full-access members immediately, narrow list empty.
- Every registered service is exposed on share; the per-service `global` flag is deleted.
- Multi-origin apps on shares use their public defaults (VS Code webviews → `vscode-cdn.net`, as vscode.dev does); connector hostname handling is designed so ACM wildcard certs can be added later (Option C).

### Deleted behavior
- `/service/<name>/` URLs no longer exist. Service-worker bootstrap, HTML rewriting, `<base>` injection, WS shim, cookie-path rewriting: all gone.
- `system_interface` no longer proxies anything.

## Implementation plan

### mngr repo — `libs/mngr_forward`
- `primitives.py`
  - Replace `FORWARD_SUBDOMAIN_PATTERN` with a pattern capturing optional service labels: `^(?:(?<labels>[a-z0-9-]+(?:\.[a-z0-9-]+)*)\.)?(agent-[a-f0-9]+)\.(?:localhost|127\.0\.0\.1)(?::\d+)?$`; the **last** label before `agent-<hex>` is the service name, deeper labels are the service's own sub-origin space.
  - Add `ServiceLabel` validation (lowercase alphanumeric + hyphen).
- `resolver.py`
  - `resolve(agent_id)` → `resolve(agent_id, service_name: str | None)`; `None` (bare origin) maps to the shell service; otherwise look up `_services_by_agent[agent][service]`. Data already present.
- `server.py`
  - Parse `(service, agent)` from Host; route bare origin → shell service, service label → its port, unknown-but-plausible service → existing loading page (`loading_page.py`, wired at the current `_service_unavailable_response`).
  - Set the subdomain session cookie with `Domain=agent-<hex>.localhost` in `_handle_subdomain_auth_bridge` so one bridge hop covers all service subtrees.
  - Keep the `mngr_forward_session` strip before forwarding (`server.py:348`) — unchanged, more important now.
  - Auth redirect logic (`_unauthenticated_subdomain_response`, `/goto/`) unchanged except cookie domain.
- `tls.py` — keep; extend cert generation with per-workspace and per-service wildcard SANs (`*.agent-<hex>.localhost`, `*.<name>.agent-<hex>.localhost`). Cert regeneration strategy needed for services registered after cert creation (regenerate + hot-reload, or a broad static SAN set — decide in implementation).
- `cookie.py` / `auth.py` — accept a domain parameter; token flow unchanged.
- Tests: update `primitives_test.py`, `resolver_test.py`, `server_test.py`, `cookie_test.py`, `tls_test.py` (new SAN shapes).

### mngr repo — `apps/minds`
- `desktop_client/sharing_handler.py`
  - `enable_sharing` grows the two-tier shape: workspace-level grant (writes the Access Group) vs service-level grant (writes that service's app policy).
  - New-service reconciliation contract documented here; execution lives in the connector.
- `desktop_client/imbue_cloud_cli.py` + `libs/mngr_imbue_cloud/.../connector/client.py`
  - New client calls: `create_or_update_workspace_group`, `grant_workspace`, `revoke_workspace`, `grant_service`, `revoke_service` (names per connector contract below).
- Workspace settings UI: master email list (labeled "full workspace access") + per-service lists.
- `forward_cli.py` — unchanged (service map already streams; consumed as-is).

### default-workspace-template
- Delete `system/libs/system_interface/imbue/system_interface/proxy.py`, `service_dispatcher.py`, and their tests; remove `register_service_routes` wiring from the app factory.
- `frontend/src/views/DockviewWorkspace.ts`
  - `getServiceUrl(serviceName)` → `deriveServiceOrigin(serviceName)`: local hosts (`agent-<hex>.localhost`) → `http://<name>.<host>/`; shared hosts (contains `--`) → swap the first `--` token. Persisted `url` in layout JSON becomes a stale hint; `serviceName` authoritative on load.
  - Terminal/browser refs keep their query args on the new origins (`http://terminal.<ws>/?arg=agent&arg=<name>`).
- `frontend/src/views/IframePanel.ts` — no change (cross-origin fallback exists).
- `system/scripts/layout.py` / `layout_ops.py` — update `_TERMINAL_SERVICE_URL_PATH`-style constants to the origin-based scheme.
- `system/scripts/forward_port.py` — validate hostname-label-safe names (underscores allowed: `system_interface` predates the scheme and underscore labels resolve fine on Cloudflare DNS and in Chromium); drop the `global` field.
- `system/supervisord.conf` — keep the `system_interface` service name (a rename was considered for DNS-hygiene but dropped: underscore hostnames already worked for Cloudflare shares, and the rename broke resumed snapshots' `apps.toml`); drop dead `ROOT_PATH=/service/browser` env; keep everything else.
- Docs: update `apps/minds/docs/overview.md`, workspace glossary, `build-web-service` skill (remove "the proxy handles prefixing" guidance; state "your app owns its origin").

### remote_service_connector (separate repo — API contract)
- One Access Group per shared workspace holding the master list; app-per-service policies become `group ∪ service emails`.
- Every service Access app is created with `allow_iframe: true` (documented Access setting enabling iframe embedding) and a non-Strict SameSite on the auth cookie, so shared-shell panels can authenticate via the silent per-hostname bounce after one top-level login. All share hostnames are same-site (one label under the zone), which is the supported embedding case.
- `POST /sharing/enable` (service-level) unchanged shape; new workspace-level endpoints: create/ensure group, grant/revoke on group.
- Service-registration reconcile: create DNS + ingress + Access app (policy = group ref) for services appearing while shared.
- Revocation semantics knob: session duration + optional revoke-active-sessions call (open question).

## Implementation phases

### Phase 0 — verifications (blocking design confirmations)
- Nested `*.localhost` same-site + two-label resolution: iframe a sub-subdomain locally; confirm cookies flow; Chrome + Safari + Firefox.
- Access reactive auth inside an iframe with an existing session, with `allow_iframe: true` set on the app (decisive for shared-shell panels; now a confirm-documented-behavior test rather than an unsupported flow — also check private-browsing / strict cookie modes).
- Access Groups: reference semantics, group/member limits.
- Connector API surface today vs the contract above; confirm hostname component order from connector source.
- Each result is recorded in the decisions doc; failures route to documented fallbacks (per-panel auth bootstrap; flat-local naming contingency).

### Phase 1 — local routing, additive (system still fully working)
- `mngr_forward`: service-label parsing + resolver branch + Domain cookie + loading page. Old path-muxing in the template untouched; both schemes serve simultaneously.
- Manual check: `http://terminal.agent-<hex>.localhost:8421/` serves ttyd raw.

### Phase 2 — cutover local (big-bang lands here)
- Frontend origin derivation; panels move to service origins.
- Delete `proxy.py` + `service_dispatcher.py`; shell stops proxying.
- `forward_port.py` validation; supervisord rename; layout constants.
- Extend `tls.py` SANs so HTTP/2 mode works on nested origins.
- Result: local system fully on the new scheme.

### Phase 3 — sharing (two-tier)
- Connector: groups + per-app policies + registration reconcile.
- minds: client calls, sharing_handler, settings UI (master + per-service lists).
- Shared-shell panels resolve sibling hostnames.

### Phase 4 — cleanup and docs
- Remove dead config (`global` flags in existing templates, `ROOT_PATH`), update docs/specs (`specs/workspace-server-forwarding`, `specs/mngr-forward-plugin` superseded notes), update `build-web-service` skill, tutorial e2e references.

### Note on retained TLS mode
- TLS/HTTP-2 is retained by explicit decision (2026-07-27): per-origin routing removes most of the connection-cap pressure, but the mode stays for SSE-heavy services and future headroom. The accepted cost is the SAN-extension work in `tls.py`, since the current one-label `*.localhost` cert cannot match nested service origins.

## Testing strategy

- **Unit — mngr_forward**: hostname parsing (service labels, subtree depth, invalid names, bare origin); resolver service branch (known/unknown service, unknown agent); cookie domain scoping; auth bridge with domain cookie.
- **Unit — frontend**: `deriveServiceOrigin` for local and shared host shapes (vitest, alongside existing DockviewWorkspace tests); layout load prefers `serviceName` over stale `url`.
- **Unit — template**: `forward_port.py` name validation.
- **Integration — mngr_forward**: existing server_test harness extended: HTTP + WS through a service subdomain to a stub backend; SSE passthrough; loading page for unregistered service; cookie isolation between two agents.
- **Acceptance — template (docker agent)**: create workspace; shell loads at bare origin; terminal panel connects over `terminal.<ws>` WS; a scaffolded web service works at its origin with zero rewriting; layout save/load round-trips.
- **Sharing (staging connector)**: share a service → narrow grant works, other services 403; add email to master list → all services reachable; register a new service while shared → reachable by master list without action; revoke → blocked per chosen semantics.
- **Manual spike**: install openvscode-server in a workspace by hand (supervisord + forward_port); verify workbench, terminal, extensions, markdown preview (webviews) locally; verify shared with webviews via CDN default.
- **Edge cases**: service names colliding with `agent-` prefix (reject at registration); deep subtree WS; two workspaces open simultaneously (cookie isolation); Safari.

## Implementation resolutions (2026-07-28, post-review)

A pre-implementation review flagged several gaps; how each landed:

- **TLS SANs (review item 1 — valid):** the SAN-extension framing here was
  unimplementable (X.509 wildcards match one label; no static SAN set can
  cover unknown agent ids). Implemented instead as **per-SNI dynamic cert
  minting** in `tls.py`: any uncovered `.localhost` name gets a certificate
  for that exact hostname on first handshake (cached, signed by the same
  ephemeral key). Covers any depth and services registered at any time; no
  regeneration/hot-reload needed. Validated over HTTPS locally incl. deep
  sub-origins.
- **Auth redirect (review item 2 — valid, "unchanged except cookie domain"
  was wrong):** the unauthenticated bounce now carries the full service
  label chain (`?service=<labels>`) and original path+query (`?next=`);
  `/goto/` validates each label as a DNS-safe `ServiceLabel` (404 on
  crafted values) and targets the bridge at the exact requesting origin.
  Direct deep links to service origins are first-class. Tested.
- **Resolver strategy dispatch (review item 3 — valid):** decided: in
  manual-port mode the bare origin maps to the fixed port and named service
  labels still resolve from the registered service map. Tested.
- **Regex syntax:** implemented with Python's `(?P<labels>...)`; pattern
  stays IGNORECASE, parsed labels are lowercased, `ServiceLabel` enforces
  lowercase at registration.
- **`global` flag:** confirmed nonexistent on the template branch; nothing
  to delete (line 33/73 of this plan were stale).
- **`forward_cli.py` "unchanged":** confirmed once the shell rename was
  dropped — the service name stays `system_interface` on both sides.
- **Domain-cookie eviction (review addition — accepted trade-off):** any
  service under `agent-<hex>.localhost` can set/evict a parent-domain cookie
  named `mngr_forward_session`. Impact is bounded: services never see the
  real cookie (strip-before-forward), planted values fail signature
  verification, and a failed cookie self-heals through `/goto/` (the tested
  stale-cookie path). Residual risk is nuisance eviction (a forced
  re-bridge hop), not theft or useful fixation. Documented as a local-mode
  trade-off.
- Phase 4's `specs/workspace-server-forwarding` / `specs/mngr-forward-plugin`
  paths do not exist in either repo; the real doc updates happened in the
  template's skills/docs (build-app et al).

## Open questions

- Per-tab Share button: edits that service's narrow list (leading option) or removed in favor of workspace settings only?
- Revocation: list-edit only with short session duration, or also revoke active sessions?
- Subdomain coordinate: keep `agent-<hex>` (current lean) or rename to a workspace id while compatibility is already broken? (Team discussion flagged.)
- Verification outcomes may reroute: framed reactive auth failing → per-panel auth bootstrap design needed; `*.localhost` same-site failing in Safari → local naming contingency (flat local labels + Domain cookie workaround) must be designed.
- Access Group / app count limits at scale (many workspaces per user) — connector-side pagination/cleanup policy.
- What happens to existing shared workspaces at cutover (no migration promised; document "re-share after upgrade"?).
