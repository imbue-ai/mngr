# openvscode multi-origin: status, problems, solutions

Digest of the webview-origin issues observed after the service-per-origin
cutover (details: `validation-results.md`; connector contract:
`plan-forwarding-redesign.md`).

## Where we're at

- The cutover works: openvscode runs unmodified at its own origin, locally
  (`https://openvscode.agent-<hex>.localhost:8421/`) and on shares
  (`https://openvscode--<host>--<user>.imbueminds.com/`). Workbench,
  terminal panel, and extensions validated live (2026-07-28, twice).
- Webviews (markdown preview, walkthroughs, extension UIs) still load from
  VS Code's default external endpoint `{{uuid}}.vscode-cdn.net` — a
  deliberately foreign origin that sandboxes extension-supplied HTML away
  from the workbench origin (vscode.dev ships the same way).
- Share auth: each share hostname is its own Cloudflare Access app; panel
  iframes authenticate via a silent per-hostname bounce off the one
  top-level login. Validated in Electron/Chrome.

## What the problems are

Both problems are consequences of the `vscode-cdn.net` webview origin; they
are the same root cause surfacing in two places.

1. **Local: webview links/windows open outside the app.** The minds
   Electron shell's window-open policy treats `*.vscode-cdn.net` as an
   external origin, so webview content that opens a window lands in the
   system browser instead of rendering inline.
2. **Shares: a small subset of webview resource loads render broken**
   (observed: a walkthrough image; backend 200, local forward 200, share
   hostname 302-to-Access). The webview frame is cross-*site* to the share
   hostname, so the browser refuses to attach the Access cookie to those
   subresource fetches; Access redirects them to login, which a bare asset
   fetch cannot complete. Most webview loads are unaffected because they
   ride VS Code's service-worker relay through the same-site, authenticated
   workbench frame. Severity is browser-dependent: Chrome/Electron mostly
   work today; Firefox partitions third-party cookies; Safari blocks them
   outright.

## What the solutions are

**Root fix (browser-proof, fixes both): self-host the webview endpoint on
the service's own sub-origin space** — run openvscode with
`--webview-external-endpoint={{uuid}}.openvscode.<workspace-host>`.

- Preserves VS Code's security model: each webview keeps its own
  per-`{{uuid}}` origin, still isolated from the workbench by the
  same-origin policy.
- Makes every webview request same-*site*: cookies attach under plain
  `SameSite=Lax` (no third-party-cookie policy applies, so Safari/Firefox
  work), and the Electron window-open policy sees a workspace origin, so
  links render inline. Both problems disappear; no `SameSite=None` needed
  for openvscode.
- **Locally deployable now**: deep sub-origin routing
  (`sub.svc.agent-<hex>.localhost` → `svc`) and per-SNI cert minting are
  already validated on this branch.
- **On shares, gated on using ACM (Cloudflare Advanced Certificate
  Manager)**: `{{uuid}}.openvscode--<host>--<user>.imbueminds.com` is two
  labels under the zone, beyond the universal cert's `*.imbueminds.com`.
  ACM provides the deeper wildcard certs (the paid, gating piece); the
  connector additionally needs a wildcard DNS record, wildcard ingress
  rule, and Access coverage for the sub-hosts. The connector's hostname
  handling was designed so this can be added later.

**Interim mitigations (until Option C):**

- Local links: allowlist `*.vscode-cdn.net` iframes in the Electron shell's
  window-open policy — or skip straight to the local
  `--webview-external-endpoint` flag in the template's supervisord command
  (+ build-app guidance), which also removes the CDN dependency locally.
- Shares (Chrome-family only): connector sets `allow_iframe: true`
  (documented Access setting enabling iframe embedding — today's working
  behavior made configuration-guaranteed) and a per-service
  `SameSite=None` opt-in on the Access cookie. `SameSite=None` must NEVER
  be global: it re-opens CSRF, and cross-site WebSockets (not covered by
  CORS) would let an arbitrary website open authenticated sockets to shared
  services — unacceptable for the terminal. Opt-in is declared at service
  registration and threads `forward_port → sharing → connector`; opted-in
  services must validate `Origin` on WS endpoints. This is a bridge: it
  does not help Safari (blocks third-party cookies regardless), and it
  retires once the webview endpoint is self-hosted.

**Sequencing:**

1. Local `--webview-external-endpoint` (template supervisord + build-app
   guidance) and/or Electron allowlist → fixes local link behavior now.
2. Connector cookie settings (`allow_iframe` explicit; per-service
   SameSite opt-in machinery) → hardens shares for Chrome-family browsers.
3. Connector ACM work (wildcard certs + DNS/ingress/Access) → flip the
   webview endpoint on shares; retire the SameSite bridge for openvscode.

## Related: the `/api/browsers` passthrough (and why terminal has none)

The shell UI itself calls the browser daemon's fleet API (`GET/POST
/browsers` — list the fleet, create a browser from the "+" menu). After the
cutover the browser service lives on a sibling origin, and sibling
subdomains are same-*site* but not same-*origin* — `fetch()` gets no CORS
exemption from same-siteness, locally or on shares. Rather than adding CORS
to the daemon (and Access-intercepted preflights on shares), the shell
backend forwards those two calls server-side as `/api/browsers`, keeping
the UI's requests same-origin. Terminal needs no equivalent because the
shell never calls a terminal HTTP API: terminal state lives in the
connection (clicking "New terminal" opens a ttyd iframe whose URL names the
tmux session, and attaching creates it), while browser state lives in the
daemon (a fleet that outlives its viewer panes, with server-allocated
names). The browser fleet is therefore the one place the shell must *read a
sibling service's API* rather than merely frame it — and iframing, not
fetching, is the case the per-origin design is built around.
