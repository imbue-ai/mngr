# Architecture

The local forwarding server is a FastAPI app that handles authentication and traffic forwarding. It is the gateway through which users access all their changelings.

The simplest way would be to use sub-domains, but we don't control the DNS or URLs where user's agents are being served, so we have to do it with URL paths instead. 
In order to make that actually work, we use a combination of service workers, script injection, and rewriting.

This forwarding server is a separate component from any individual changeling's web server -- the forwarding server does not define what changelings do or how they respond to messages. It only handles routing and authentication so that the URLs being served by the changeling are accessible remotely.

## Authentication

The forwarding server uses `itsdangerous` for cookie signing. Auth works as follows:

- **Signing key**: generated once on first server start, stored at `{data_directory}/signing_key`. Used to sign all auth cookies.
- **One-time codes**: generated during `changeling deploy` and stored in `{data_directory}/one_time_codes.json`. Each code is associated with an agent ID and can only be used once. When a code is consumed, it is marked as "USED" in the JSON file.
- **Cookies**: after successful authentication, the server sets a signed cookie for the specific changeling. The cookie value contains the agent ID, signed with the signing key.

## Local forwarding server routes

`/login` route (takes agent_id and one_time_code params):
    if you have a valid cookie for this changeling, it redirects you to the main page ("/")
    if you don't have a cookie, it uses JS to redirect you and your secret to "/authenticate?agent_id={agent_id}&one_time_code={one_time_code}"
        this is done to prevent preloading servers from accidentally consuming your one-time use codes

`/authenticate` route (takes agent_id and one_time_code params):
    validates the one-time code against stored codes
    if this is a valid code (not used and not revoked), marks it as used and replies with a signed cookie
    if this is not a valid code, explains to the user that they need to generate a new login URL for this device (each URL can only be used once)

`/` route is special:
    looks at the cookies you have -- for each valid changeling cookie, that changeling is listed
    if you have 0 valid cookies, it shows a placeholder telling you to log in
    if you have 1 or more valid cookies, those changelings are shown as links to their individual pages

`/agents/{agent_id}/` route lists all servers for a changeling:
    requires a valid auth cookie for that changeling
    shows a page listing all known server names for the agent (discovered via `mng logs`)
    each server name links to `/agents/{agent_id}/{server_name}/`

`/agents/{agent_id}/{server_name}/{path}` route serves individual server UIs:
    requires a valid auth cookie for that changeling (auth is per-agent, not per-server)
    proxies any request from the user to the specific server's backend URL
    uses Service Workers for transparent path rewriting so the server's app works correctly under the `/agents/{agent_id}/{server_name}/` prefix

All pages except "/", "/login" and "/authenticate" require the auth cookie to be set for the relevant changeling.

## Proxying design

Since we can't control DNS or use subdomains, we multiplex changelings under URL path prefixes (`/agents/{agent_id}/{server_name}/`). Each server for an agent gets its own prefix and Service Worker scope. This requires a combination of Service Workers, script injection, and rewriting:

- On first navigation, a bootstrap page installs a Service Worker scoped to `/agents/{agent_id}/{server_name}/`
- The SW intercepts all same-origin requests and rewrites paths to include the prefix
- HTML responses have a WebSocket shim injected to rewrite WS URLs
- Cookie paths in Set-Cookie headers are rewritten to scope under the server prefix
- WebSocket connections are proxied bidirectionally
