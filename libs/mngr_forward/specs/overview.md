# mngr forward

Component: the `mngr forward` proxy (`libs/mngr_forward/`) -- a local
HTTP/WebSocket proxy that serves each agent's web UI on its own
`agent-<id>.localhost` origin and byte-forwards every request to that
agent's backend, behind a single sign-in. It runs standalone for a browser
user, or as a child process of an embedding host application (the minds
desktop client), which pre-authorizes its own browser shell and consumes
the proxy's stdout event stream.

The corpus currently covers two areas. `authentication/` describes how a
browser or embedding host acquires a session and how that one session opens
every workspace origin. `forwarding/` describes what happens to requests on
workspace origins: routing, byte-forwarding, and failure behavior. The
Rules in the root `invariants.feature` bind the whole corpus.

## Glossary

- **forward proxy**: the process started by `mngr forward`. One listen port
  serves the bare origin and every workspace origin.
- **bare origin**: `localhost:<port>` -- the origin that carries sign-in,
  the home page, and the goto bridge.
- **workspace origin**: `agent-<id>.localhost:<port>` -- one origin per
  agent; requests here are forwarded to that agent's backend.
- **one-time code**: a secret minted fresh at each process start. The proxy
  prints a **login URL** (`http://localhost:<port>/login?one_time_code=<code>`)
  to its terminal; opening it is the only way to establish a session in a
  browser that has none.
- **session**: the signed-in state of a browser on one origin, carried by a
  signed `mngr_forward_session` cookie. Browsers scope cookies per origin,
  so the bare-origin session is bridged to each workspace origin
  automatically (the goto bridge); the user signs in once.
- **preauth cookie**: an opaque value an embedding host application may
  configure at proxy startup and pre-set in its own browser shell; a request
  presenting exactly that value as its session cookie counts as signed in.
- **goto bridge**: the bare origin's `/goto/<agent-id>/` route, which
  converts a valid bare-origin session into a workspace-origin session
  without user interaction.
- **agent**: an entity the proxy forwards for. A "known agent" is one that
  discovery has told the proxy about.
- **backend**: the per-agent HTTP server the proxy forwards a workspace
  origin's requests to. An agent's backend is "routable" once the proxy
  knows its address and can reach it. How addresses are discovered is out
  of scope here; these specs treat backend state as given.

## Out of scope

- The CLI contract: flag validation, port selection and fallback,
  `--open-browser`, configuration defaults.
- The stdout envelope stream (`login_url`, `listening`, `resolver_snapshot`,
  backend-failure events, observe/event passthrough) -- a planned `stream/`
  area.
- Discovery and backend resolution: observe modes, agent/event filters, the
  `--no-observe` snapshot, service-map cache seeding, `SIGHUP` handling --
  a planned `discovery/` area.
- Reverse tunnels (`--reverse`) -- a planned `tunnels/` area.
- The TLS/HTTP-2 serving mode (`--use-http2`). When enabled, client-facing
  URLs use `https` and session cookies are marked Secure; behavior is
  otherwise identical. Scenarios are written for the default plain-HTTP
  mode.
- The transport used to reach a remote agent's host (SSH tunneling), except
  where it changes what a client observes.
