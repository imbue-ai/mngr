# Architecture

The local forwarding server is a FastAPI app that handles authentication and traffic forwarding. It is the gateway through which users access all their minds.

The simplest way would be to use sub-domains, but we don't control the DNS or URLs where user's agents are being served, so we have to do it with URL paths instead.
In order to make that actually work, we use a combination of service workers, script injection, and rewriting.

This forwarding server is a separate component from any individual mind's web server -- the forwarding server does not define what minds do or how they respond to messages. It only handles routing and authentication so that the URLs being served by the mind are accessible remotely.

## Authentication

Authentication is global (one session grants access to all agents). The forwarding server uses `itsdangerous` for cookie signing. Auth works as follows:

- **Signing key**: generated once on first server start, stored at `{data_directory}/signing_key`. Used to sign all auth cookies.
- **One-time codes**: a login code is generated and printed to the terminal when the server starts. Codes are stored in `{data_directory}/one_time_codes.json` and can only be used once.
- **Session cookie**: after successful authentication, the server sets a signed `mind_session` cookie. This single cookie grants access to all agents and all server routes.

## Local forwarding server routes

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
    GET: shows a form to enter a git URL for creating a new mind
    POST: accepts form data with git_url, starts agent creation, redirects to /creating/{agent_id}

`/api/create-agent` route (POST, JSON API, requires auth):
    accepts JSON body with git_url, starts agent creation, returns agent_id and status

`/api/create-agent/{agent_id}/status` route (GET, JSON API, requires auth):
    returns current creation status (CLONING, CREATING, DONE, FAILED) and redirect_url when done

`/creating/{agent_id}` route (requires auth):
    shows a progress page that polls /api/create-agent/{agent_id}/status
    auto-redirects to the agent when creation completes

`/agents/{agent_id}/` route (requires auth):
    redirects to the agent's default web server at /agents/{agent_id}/web/

`/agents/{agent_id}/servers/` route (requires auth):
    shows a page listing all known server names for the agent (discovered via `mngr events`)

`/agents/{agent_id}/{server_name}/{path}` route (requires auth):
    proxies any request from the user to the specific server's backend URL
    uses Service Workers for transparent path rewriting

## Proxying design

Since we can't control DNS or use subdomains, we multiplex minds under URL path prefixes (`/agents/{agent_id}/{server_name}/`). Each server for an agent gets its own prefix and Service Worker scope. This requires a combination of Service Workers, script injection, and rewriting:

- On first navigation, a bootstrap page installs a Service Worker scoped to `/agents/{agent_id}/{server_name}/`
- The SW intercepts all same-origin requests and rewrites paths to include the prefix
- HTML responses have a WebSocket shim injected to rewrite WS URLs
- Cookie paths in Set-Cookie headers are rewritten to scope under the server prefix
- WebSocket connections are proxied bidirectionally
