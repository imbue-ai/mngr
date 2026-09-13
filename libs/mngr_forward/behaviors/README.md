# mngr forward behavior corpus

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This corpus specifies the externally observable behavior of the `mngr forward` proxy (`libs/mngr_forward/`).
The proxy is a local HTTP/WebSocket gateway that serves each agent on its own origin and byte-forwards every request to that agent's backend, behind a single sign-in.
It runs standalone for a browser user, or as a child process of an embedding host application that pre-authorizes its own browser shell and drives its recovery from the proxy's stdout event stream.

Terms carry their mngr meanings and are not redefined here: an *agent* and a *host* are mngr's identity primitives, an agent id (`agent-<hex>`) and a host id (`host-<hex>`).
The proxy's addressable unit is the *agent*: one mngr agent, served at its own `agent-<hex>.localhost` origin, where `<hex>` is the 32-character agent id.
An agent's backend runs on a host, so the proxy resolves each discovered agent to the host it runs on and forwards there; the host is only the path to the backend, never a surface a client addresses — that is always the agent origin.


## Areas

`authentication/` describes how a browser or an embedding host acquires a session on the bare origin, and how that one session is carried onto every agent origin.
`forwarding/` describes what happens to requests on agent origins: how the Host header routes them, how they are byte-forwarded to a backend, and what a client observes when no backend answer exists.
The Rules in this folder's `invariants.feature` bind the whole corpus.

## The origins the proxy serves

One listen port serves two kinds of origin, and the Host header decides which.

The *bare origin* is `localhost:<port>`, with `127.0.0.1` accepted in place of `localhost`.
It carries the sign-in machinery, the home page, and the bridges that extend a session onto an agent.

An *agent origin* is any host of the form `[<service>.]agent-<hex>.localhost:<port>`, where `<hex>` is a 32-character hexadecimal agent id.
The bare `agent-<hex>.localhost` origin serves the agent's *default service* -- the one reached when the origin carries no service label; each registered service owns `<service>.agent-<hex>.localhost`; a label chain deeper than the service name is that service's own sub-origin space.
Every origin of one agent shares the `agent-<hex>.localhost` *agent domain*, which is the scope of that agent's session cookie.

A *host origin* of the form `[<service>.]host-<hex>.localhost` is a legacy coordinate the proxy no longer serves: it redirects a top-level navigation to the agent origin and refuses everything else, so no backend is ever reached on it.

## How one sign-in reaches every origin

A session is the signed-in state of a browser on one origin, carried by a signed `mngr_forward_session` cookie.
Because browsers scope cookies per origin, a bare-origin session does not by itself authenticate an agent origin.
The *goto bridge* -- the bare origin's `/goto/<coordinate>/` route -- closes that gap without user interaction: it mints a short-lived token bound to one agent, redirects the browser to that agent, and the agent's token-redemption endpoint sets the domain-scoped agent cookie that covers the default service and every service origin at once.

An embedding host has two further ways to arrive already signed in, both skipping the one-time-code flow: a *preauth cookie* value it pre-sets on the bare origin, and a *browser-bridge token* it redeems at the bare origin.

## Out of scope

- The CLI contract: flag validation, port selection and fallback, opening a browser, configuration defaults.
- The stdout envelope stream (startup events, resolver snapshots, backend-failure events, discovery and per-agent passthrough), which an embedding consumer reads instead of the HTTP responses: a planned stream area.
- Discovery and backend resolution -- how the proxy learns and updates the address behind an agent: a planned discovery area; here a backend's reachability is taken as given.
- Reverse tunnels: a planned tunnels area.
- The TLS/HTTP-2 serving mode, under which client-facing URLs use `https`/`wss` and cookies are marked `Secure`; behavior is otherwise identical, so scenarios are written for the default plain-HTTP mode.
- The transport used to reach a backend on another host, except where it changes what a client observes.
