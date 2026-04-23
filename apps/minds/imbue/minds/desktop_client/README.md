# Architecture

The local desktop client is a FastAPI app that handles authentication and traffic forwarding. It is the gateway through which users access all their workspaces.

Each workspace already runs its own `minds_workspace_server`, which serves the dockview UI and exposes its services under `/service/<name>/...` paths (handling Service Worker bootstrap, path rewriting, cookie scoping, and WebSocket shims internally). The desktop client's job is to route browser traffic for `<agent-id>.localhost:PORT/*` to the correct workspace server -- it does not rewrite paths or inject anything itself.

This desktop client is a separate component from any individual workspace's web server -- the desktop client does not define what workspaces do or how they respond to messages. It only handles routing and authentication so that the URLs being served by the workspace are accessible locally.

## Authentication

Authentication is global (one session grants access to all agents). The desktop client uses `itsdangerous` for cookie signing. Auth works as follows:

- **Signing key**: generated once on first server start, stored at `{data_directory}/signing_key`. Used to sign all auth cookies.
- **One-time codes**: a login code is generated and printed to the terminal when the server starts. Codes are stored in `{data_directory}/one_time_codes.json` and can only be used once.
- **Session cookie**: after successful authentication, the server sets a signed `minds_session` cookie. It is issued with `Domain=localhost` when the request host is `localhost` or an `<agent-id>.localhost` subdomain, so the browser carries it across all workspace subdomains with a single sign-in.

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
        if exactly 1 agent is known, redirects directly to that agent
        if 2+ agents are known, shows links to each agent
        if no agents exist, shows the agent creation form

`/create` route (requires auth):
    GET: shows a form to enter a git URL for creating a new workspace
    POST: accepts form data with git_url, starts agent creation, redirects to /creating/{agent_id}

`/api/create-agent` route (POST, JSON API, requires auth):
    accepts JSON body with git_url, starts agent creation, returns agent_id and status

`/api/create-agent/{agent_id}/status` route (GET, JSON API, requires auth):
    returns current creation status (CLONING, CREATING, DONE, FAILED) and redirect_url when done

`/creating/{agent_id}` route (requires auth):
    shows a progress page that polls /api/create-agent/{agent_id}/status
    auto-redirects to the agent when creation completes

`<agent-id>.localhost:PORT/*` (subdomain catch-all, requires auth):
    a host-header middleware and a catch-all WebSocket route recognize
    `<agent-id>.localhost(:port)` hosts and byte-forward the HTTP or
    WebSocket request to that workspace's minds_workspace_server (resolved
    via the backend resolver, optionally through an SSH tunnel). Unknown
    subdomains return 404; unauthenticated HTML navigations redirect to
    the bare-origin landing page so the user can sign in.

## Proxying design

Because the desktop client only byte-forwards requests to the per-workspace `minds_workspace_server`, each workspace keeps its own origin (an `<agent-id>.localhost` subdomain). Within each workspace origin, the workspace server is responsible for multiplexing the workspace's individual services under `/service/<name>/...`:

- On first navigation to a service, the workspace server returns a bootstrap page that installs a Service Worker scoped to `/service/<name>/`.
- The SW intercepts all same-origin requests and rewrites paths to include the prefix.
- HTML responses have a WebSocket shim injected to rewrite WS URLs.
- Cookie paths in `Set-Cookie` headers are rewritten to scope under the service prefix.
- WebSocket connections are proxied bidirectionally.

See `apps/minds_workspace_server/imbue/minds_workspace_server/service_dispatcher.py` for the service-side implementation.
