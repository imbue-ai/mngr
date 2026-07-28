# Forwarding redesign — e2e validation results (2026-07-28)

All validations ran against a real Docker workspace (`fwd-e2e-501b0a01`,
`agent-28ed004a817f4eae8d510391ef5c7bd2`) created through the minds Electron
create flow from the cut-over template, on the production env.

## Local (service-per-origin through mngr_forward, TLS/HTTP-2 mode)

- Shell rendered at the bare workspace origin
  `https://agent-<hex>.localhost:8421/` (Electron content view; real render).
- Terminal panel is a cross-origin iframe at
  `https://terminal.agent-<hex>.localhost:8421/?arg=...`; ttyd's xterm
  rendered inside it — HTTP + WebSocket + per-SNI minted cert all through the
  service origin.
- Claude sign-in modal driven once with an API key; the Domain-scoped
  workspace cookie carried auth to every service origin afterwards.
- openvscode installed BY THE WORKSPACE AGENT via chat (standard app
  workflow: supervisord program + `forward_port.py --name openvscode`), no
  manual container changes. Verified `RUNNING` under supervisord, registered
  in `data/.state/apps.toml`, serving 200 on its port.
- `https://openvscode.agent-<hex>.localhost:8421/` → 200, real workbench HTML
  (authenticated curl).
- `https://deep.openvscode.agent-<hex>.localhost:8421/` → 200 — arbitrary
  sub-origin depth routes to the same service (multi-origin webview support).

## Shared (Cloudflare, existing per-service connector API + fan-out)

- `PUT /api/v1/workspaces/<agent>/sharing/system-interface` fanned out to all
  registered services; connector returned hostnames
  `<name>--28ed004a817f4eae--61d78f321d9e4683.imbueminds.com` for
  system-interface, terminal, browser, openvscode with identical email
  policies.
- Fresh external Chromium (no local state): share URL → Cloudflare Access
  login → email OTP → shared shell rendered.
- Panels on the shared shell derived sibling hostnames by first-`--`-token
  swap (`https://terminal--28ed...--61d7....imbueminds.com/?arg=...`) and the
  terminal service's UI rendered INSIDE the cross-hostname iframe — i.e.
  Access reactive auth in an iframe with an existing session works (the
  plan's Phase-0 open question, confirmed on the live stack in Chromium with
  default cookie settings).
- Direct visit to the openvscode share hostname silently authenticated via
  the existing Access session and rendered the full VS Code workbench.

## Caveats / follow-ups

- WebKit/Safari local: `*.localhost` subdomain iframes get no cookies (each
  x.localhost is its own site in WebKit) — see decisions-phase0.md fallbacks.
  Electron/Chromium/Firefox are unaffected.
- Two-tier Access Groups (workspace master list on the connector) remain
  future connector-side work; the interim fan-out shares each service with
  the same email list, and new services registered while shared need a
  re-save of the workspace share.
- Existing shared workspaces from before the cutover are not migrated
  (re-share after upgrade).
