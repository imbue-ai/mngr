# Browser authorization

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

The **browser authorization component** -- fully, the *desktop-app backend-server* browser authorization component -- is the browser-facing part of the minds *desktop client*: the bare-origin web UI served by `minds run` (`apps/minds/imbue/minds/desktop_client/`) that serves every page the browser reaches, carries the session, and authenticates it.
It is the gateway through which the user reaches all their workspaces.

The features in this folder cover authenticating a session with a one-time code, and the session that authentication establishes.
The Rules in `invariants.feature` bind all of them.

## How session authentication works

The desktop client keeps its local state in a data directory; one installation = one data directory.
At server start it mints a one-time code and prints an authentication URL (`http://localhost:<port>/login?one_time_code=<code>`) to its terminal; this is the only credential a user ever types or clicks.
Authenticating a session establishes it: the authenticated state of a browser, carried by a signed session cookie.
The session is global: it is the one credential gating every page the browser authorization component serves.

## What this protects

The browser authorization component holds the user's private data -- all response content specific to this user or this installation -- and answers on a local origin, so any caller able to reach that origin could otherwise read it.
The session is the gate: without a valid one, a request observes no user data, only the authentication machinery itself.
The adversary is any caller of the local origin that lacks a valid session, wherever it runs; a cross-origin page in the user's own browser is turned away the same way, since its request carries no session cookie.
The one-time code that establishes a session appears only in the desktop client's own output, so obtaining it requires access to that output, and the invariants here bound the code's exposure: it is single-use, and spent only by executing the authentication page's script.

## Why "/post-login" is specified here

"/post-login" is the single route by which the out-of-scope account sign-in system returns control to this component, and only two access-control facts about it are specified here: the session gate (an arrival without a valid session is sent to authenticate), and that a caller-supplied return destination is confined to the origin -- each an illustration of the invariant that owns it (`no-data-without-session`, `no-open-redirects`).
The critical interaction, and the reason the redirect is an access-control concern and not mere routing, is between the return-destination redirect and the session gate: the gate establishes that the browser belongs to the authenticated user, and the redirect then chooses where that authenticated browser is sent next.
Because the destination is supplied by whoever formed the "/post-login" link, an unconfined redirect would let a crafted link carry a genuinely-authenticated user off the origin under the origin's own authority -- a phishing pivot that borrows the local surface's credibility at the moment the user is most primed to trust the next screen.
Confining every honored destination to a same-origin path is what keeps the redirect from carrying the user past the boundary the gate just established.
Which destination "/post-login" then chooses for an authenticated user is first-run routing, specified in `home-page/`.

## Out of scope

- Imbue-cloud account sign-in (the `/auth/*` pages) -- a separate account system layered on top of the local session -- is out of scope; only `/post-login`, where it returns control to this component, is touched here (see above), and the destination it chooses is routing specified in `home-page/`.
- The workspace-origin bridge served by the forward server (`libs/mngr_forward/`), which extends a session from the browser authorization component to each workspace's own origin -- specified in `libs/mngr_forward/behaviors/`.
- The authenticated user's home-page experience at "/" -- specified in `home-page/`.
- The `SKIP_AUTH=1` environment variable, a development escape hatch that bypasses every session check; it is intentionally left unspecified.
