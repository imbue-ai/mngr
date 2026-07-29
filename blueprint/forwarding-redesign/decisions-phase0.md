# Phase 0: Browser verification — nested `*.localhost` origins + domain-scoped cookies

Date: 2026-07-28. Verified with Playwright 1.62.0 (Chromium, Firefox, WebKit) against a
stdlib `http.server` on `127.0.0.1:9421` (port 9421 was free; no fallback port needed).
Test scripts lived in `/tmp/phase0-verify/` (throwaway, not committed).

Setup mirrors the target architecture: shell at `http://agent-abc123.localhost:9421/`,
`/login` sets `Set-Cookie: test_session=tok123; Domain=agent-abc123.localhost; Path=/; HttpOnly`
and redirects to `/`. The shell iframes `http://svc.agent-abc123.localhost:9421/frame`
(one label deep) and `http://deep.svc.agent-abc123.localhost:9421/frame` (two labels deep).
Each frame reports the Cookie header it received on its document request and on a
`fetch('/api', {credentials: 'include'})` to its own origin.

## Results

| Check | Chromium | Firefox | WebKit |
|---|---|---|---|
| Multi-label `*.localhost` resolves without DNS (page loads) | pass | pass | pass |
| Browser accepts `Domain=agent-abc123.localhost` cookie (jar shows `.agent-abc123.localhost`; localhost not treated as a blocking public suffix) | pass | pass | pass |
| Cookie sent to top-level shell document (`agent-abc123.localhost`) | pass | pass | pass |
| Cookie sent to 1-deep subdomain, top-level navigation (`svc.agent-abc123.localhost`) | pass | pass | pass |
| Cookie sent to 2-deep subdomain, top-level navigation (`deep.svc.agent-abc123.localhost`) | pass | pass | pass |
| Cookie sent to 1-deep subdomain **iframe** document | pass | pass | **FAIL** |
| Cookie sent on `credentials: 'include'` fetch inside 1-deep iframe | pass | pass | **FAIL** |
| Cookie sent to 2-deep subdomain **iframe** document | pass | pass | **FAIL** |
| Cookie sent on fetch inside 2-deep iframe | pass | pass | **FAIL** |
| Cross-workspace isolation: cookie NOT sent to `agent-ffff99.localhost:9421` | pass | pass | pass |

## The WebKit iframe failure, characterized

Follow-up experiments isolated the failure:

- Top-level navigations to the subdomain hosts (any depth) DO receive the domain cookie
  in WebKit — the cookie itself is fine.
- A same-origin iframe (`agent-abc123.localhost` iframed inside itself) receives the
  cookie. Only sub-domain iframes fail.
- Explicit `SameSite=None` (without `Secure`, over http) does not help.
- A subdomain iframe cannot even use a first-party cookie set by **its own origin**
  (`Set-Cookie` from the iframe's host is accepted into the jar but never sent back
  inside the embedded context). So a "each service sets its own session cookie"
  fallback does NOT rescue WebKit iframes on `.localhost`.
- Control with a real registrable domain: repeating the identical test on
  `agent-abc123.lvh.me` / `svc.agent-abc123.lvh.me` (public wildcard DNS to 127.0.0.1)
  **passes everything in WebKit**, including domain-cookie flow into 1-deep subdomain
  iframes and fetches within them.

Conclusion: WebKit computes the "site" (registrable domain) of `*.localhost` hosts as
the full host — every `x.localhost` host is its own site. Subdomain iframes under a
`.localhost` parent are therefore cross-site, and WebKit blocks all cross-site cookies
(no Storage Access grant). This is a `.localhost`-specific quirk, not a general
objection to the subdomain/domain-cookie design: on any real registrable domain the
architecture works in WebKit.

## Implications for the design

- Chromium and Firefox fully support the proposed scheme (shell + N-deep service
  subdomains + one `Domain=agent-<hex>.localhost` cookie, including inside iframes,
  including fetches from iframes). Cross-workspace isolation holds everywhere.
- Safari/WebKit needs a documented fallback for the *iframe* case only. Options, in
  rough order of preference:
  1. Use a real wildcard-DNS loopback domain instead of `.localhost` for WebKit/Safari
     users (verified working: `*.lvh.me`; equivalents: `*.traefik.me`, or an
     imbue-owned wildcard domain pointing at 127.0.0.1 — the latter avoids depending
     on a third-party DNS zone and allows a valid TLS cert).
  2. Token bootstrap into the iframe (query param / postMessage) + the iframe holding
     the session in memory or `sessionStorage`, avoiding cookies in embedded contexts.
  3. Storage Access API (`document.requestStorageAccess()`) — requires a user gesture
     inside the iframe, so poor UX; not recommended as the primary path.
  - Non-iframe flows (opening a service in a new tab / top-level) work fine in WebKit
    with no fallback.

## Caveats

- WebKit-via-Playwright is an approximation of Safari: same engine, but not Safari's
  exact ITP configuration or release cadence. Real Safari is expected to be at least
  as strict; the `.localhost` site-computation behavior is engine-level and should
  match. Spot-check real Safari before shipping the WebKit fallback decision.
- Tests ran over plain http. Cookies had no `Secure`; Chromium's
  "`SameSite=None` requires `Secure`" rule was not exercised (the passing cases rely
  on same-site defaults, which is also what production will rely on).
- `context.cookies()` in all three browsers reported the cookie with domain
  `.agent-abc123.localhost` (host-prefix dot), confirming it was stored as a domain
  cookie, not silently downgraded to host-only.
- WebSockets were not exercised (out of scope per plan); only document requests and
  credentialed fetches.
