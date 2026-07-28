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

## openvscode multi-origin observations (reported by operator)

- VS Code webviews (walkthroughs, previews, extension UIs) load from their
  default sandbox origin `<hash>.vscode-cdn.net`. The minds Electron shell's
  window-open policy treats that foreign origin as external and opens it in
  the system browser instead of rendering inline. Fix options: allowlist
  `*.vscode-cdn.net` iframes in the Electron shell, and/or run openvscode
  with `--webview-external-endpoint={{uuid}}.openvscode.<workspace-host>` so
  webviews use the service's own sub-origin space (deep sub-origin routing
  already validated locally; on shares this is the wildcard-cert "Option C"
  connector follow-up).
- On the share, a walkthrough image
  (`.../static/out/vs/workbench/contrib/welcomeGettingStarted/common/media/dark.png`)
  rendered broken. Diagnosis: backend serves it 200, local forward serves it
  200 (image/png), and the share hostname 302s unauthenticated requests to
  Access login -- so the browser sent that request without the Access cookie
  (cross-site webview/subresource context vs the Access cookie's SameSite).
  This is the exact class the planned connector work addresses
  (`allow_iframe: true` + non-Strict SameSite on the Access app cookie).
  Nuance: `SameSite=None` fixes cross-site cookie attachment only where
  third-party cookies are permitted (Chrome/Electron today; Firefox
  partitions them; Safari blocks them) -- most webview resource loads are
  unaffected either way because they go through VS Code's service-worker
  relay to the parent workbench. The browser-proof fix is self-hosting the
  webview endpoint on the service's own sub-origins
  (`--webview-external-endpoint={{uuid}}.openvscode.<workspace-host>`),
  which makes everything same-site; on shares that is gated on the
  wildcard-cert (Option C) connector work.
- Security scoping decided in review: `SameSite=None` must NOT be applied
  globally. It re-opens CSRF for that cookie, and cross-site WebSockets
  (not covered by CORS) would let an arbitrary website open authenticated
  sockets to shared services -- unacceptable for e.g. the terminal. The
  shared shell's own panels do not need it (shell and services are
  same-site under one registrable domain; validated working with Lax).
  Design: a per-service opt-in declared at registration by the installing
  agent (forward_port flag threading through sharing -> connector -> Access
  app cookie settings), default off, plus Origin-header validation on WS
  endpoints of any opted-in service; treat it as a bridge until
  self-hosted webview origins land.

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
