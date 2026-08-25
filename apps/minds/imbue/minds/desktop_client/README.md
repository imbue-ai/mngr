# Architecture

The local desktop client is a Flask app that handles authentication and traffic forwarding. It is the gateway through which users access all their workspaces.

> **UI is a Mithril single-page app.** The user-facing UI is a client-rendered
> Mithril SPA under [`../../../frontend/`](../../../frontend) (its own
> Vite/TypeScript/Tailwind package, built into `static/ui/` at wheel-build
> time). Flask serves the SPA's `index.html` for every hub route and exposes a
> session-authed JSON surface at `/ui/api/*` plus one WebSocket per window at
> `/ui/ws`. The WebSocket carries all live state (workspaces, accounts,
> providers, requests, the notification feed, per-workspace health, discovery
> health, and one-shot events) from a single edge-driven publisher
> (`ui_publisher.py`) through a
> per-client-queue broadcaster (`ui_channel.py`); the handshake is driven
> directly with `simple_websocket` (no flask-sock, so the session check runs
> before the socket is hijacked) on top of the cheroot gateway adapter in
> `ws_gateway.py`. First paint is seeded from a bootstrap document inlined
> into the served page.
>
> All UI work belongs in the `frontend/` package. The only server-rendered
> documents left are the dependency-free static pages that must work before
> (or without) the SPA bundle: the one-time-code login flow (`ui_login.py`)
> and the friendly error pages (`static_pages.py`). `static/` holds only the
> embed contract module, the vendored Sentry browser bundle + its init, the
> service icons, and the built SPA bundle (`static/ui/`, gitignored).

Each workspace already runs its own `system_interface`, which serves the dockview UI at the workspace's bare origin; every other registered service owns its own origin (`<service>.agent-<hex>.localhost:PORT/`), so nothing proxies or rewrites service traffic. The desktop client's job is to route browser traffic for `[<service>.]agent-<hex>.localhost:PORT/*` to the right in-workspace backend -- it does not rewrite paths or inject anything itself.

This desktop client is a separate component from any individual workspace's web server -- the desktop client does not define what workspaces do or how they respond to messages. It only handles routing and authentication so that the URLs being served by the workspace are accessible locally.

## Authentication

Authentication is global (one session grants access to all agents). The desktop client uses `itsdangerous` for cookie signing. Auth works as follows:

Note this is the *local* session with the desktop client itself. *Imbue account* sign-up/sign-in happens on the connector's hosted accounts pages in the system browser: the SPA drives `POST /auth/api/web-login/start` (which runs `mngr imbue_cloud auth login` -- browser + loopback + PKCE code exchange) and polls `GET /auth/api/web-login/status/<flow_id>` to render the waiting/copy-link modal. There are no in-app account sign-in pages anymore; the retired `/auth/login` and `/auth/signup` URLs 302 into the SPA with `?web-login=1`, which starts the browser flow on load.

- **Signing key**: generated once on first server start, stored at `{data_directory}/signing_key`. Used to sign all auth cookies.
- **One-time codes**: a login code is generated and printed to the terminal when the server starts. Codes are stored in `{data_directory}/one_time_codes.json` and can only be used once.
- **Session cookie**: after successful authentication, the server sets a signed `minds_session` cookie. The cookie is host-only (no `Domain` attribute): browsers treat `localhost` as a public suffix and refuse to send `Domain=localhost` cookies to subdomains. Workspace subdomains instead get their own session via the forward server's `/goto/<agent-id>/` auth bridge, so a single bare-origin sign-in still covers every workspace.

## Local desktop client routes

`/login` route (takes one_time_code param):
    if you already have a valid session cookie, it redirects you to the main page ("/")
    if you don't have a session cookie, it uses JS to redirect to "/authenticate?one_time_code={one_time_code}"
        this is done to prevent preloading servers from accidentally consuming your one-time use codes

`/authenticate` route (takes one_time_code param):
    validates the one-time code against stored codes
    if valid: marks it as used and sets a signed session cookie, then redirects to "/"
    if invalid: explains to the user that they need to use the login URL printed in the terminal

`/` route is special:
    if you don't have a valid session cookie, shows a login prompt
    if you are authenticated:
        while the error-reporting consent question is unanswered, shows the consent screen
        if any workspaces are known (discovered locally or synced from other devices), lists them all -- even when there is exactly one
        if none are known and the initial discovery is still running, shows a self-refreshing "Discovering workspaces" page
        once discovery completes with no workspaces, shows the agent creation form

`/create` route (requires auth):
    GET: shows a form to enter a git URL for creating a new workspace
    GET with ?retry={create_attempt_id}: pre-fills the form from an interrupted/failed
        create attempt's pending record (the interrupted row's Retry action); opening
        the form is non-destructive -- the dead create attempt's leftover host and
        record are cleaned up only when the new create is submitted
    POST: accepts form data with git_url, starts a create attempt, redirects to /creating/{agent_id}

`/api/create-agent` route (POST, JSON API, requires auth):
    accepts JSON body with git_url, starts a create attempt, returns agent_id and status

`/api/create-agent/{agent_id}/status` route (GET, JSON API, requires auth):
    returns current create attempt status (INITIALIZING, CLONING_REPO, CHECKING_OUT_BRANCH, CREATING_WORKSPACE, WAITING_FOR_READY, DONE, FAILED) and redirect_url when done

`/creating/{agent_id}` route (requires auth):
    shows a progress page that polls the create operation status and streams the
    create attempt log (the log buffer is replayable, so re-entering the page mid-create
    replays the history before tailing live)
    auto-redirects to the agent when the create attempt completes
    when no live create attempt is tracked but a pending-create-attempt record survives, the
    page becomes the record-backed detail view: retry + discard for an interrupted
    create attempt, persisted error + log tail + dismiss for a failed one

`[<service>.]agent-<hex>.localhost:PORT/*` (workspace-origin catch-all, requires auth):
    a host-header middleware and a catch-all WebSocket route recognize
    `[<service>.]agent-<hex>.localhost(:port)` hosts and byte-forward the
    HTTP or WebSocket request to that workspace's matching backend: the
    bare origin reaches the system_interface, `<service>.` origins reach
    that registered service (resolved via the backend resolver, optionally
    through an SSH tunnel). Unknown hosts return 404; unauthenticated HTML
    navigations redirect to the bare-origin landing page so the user can
    sign in.

## Proxying design

The desktop client only byte-forwards requests. Each workspace owns a family of origins keyed by its workspace id (its agent id -- so URLs survive machine changes; legacy host-keyed origins redirect): the bare `agent-<hex>.localhost` origin serves the shell (system_interface), each registered service owns `<service>.agent-<hex>.localhost`, and deeper labels route to the same service (its own sub-origin space for multi-origin apps). Services therefore run unmodified -- root-absolute URLs, WebSockets, cookies, and service workers all work as written; nothing rewrites anything. One session cookie scoped `Domain=agent-<hex>.localhost` (set by the forwarder's `/goto/` bridge, with `SameSite=None; Secure; Partitioned` so it is sent from inside the chrome's cross-site iframe) covers the whole family.

## The SPA shell and the workspace iframe

The user-facing UI is one web context per window (both in the desktop app and in a plain browser): the Mithril SPA shell, which renders the titlebar, the hub pages (Home, Create, Settings, ... routed client-side by `frontend/src/router.ts`), the sandboxed cross-origin iframe that displays workspace content (`frontend/src/views/shell/WorkspaceFrame.ts`), and in-DOM Mithril modals. Workspace entry goes through `GET /forward-bridge?next=/goto/<workspace-id>/`: minds verifies its own session and 302s to the forward plugin's `/_bridge` with a spawn-time secret, which sets the plugin's bare-origin cookie and redirects onward -- the browser twin of the Electron shell's programmatic cookie injection.

Shell<->workspace messaging flows exclusively through the embed contract (`static/embed_contract.js`, documented in `apps/minds/docs/embed-contract.md`); the forward plugin's appended `frame-ancestors` policy is what makes "being framed at all" proof the embedder was allowed.
