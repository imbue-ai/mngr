# Forwarding

Component: the workspace-origin request path of the forward proxy -- how
the Host header routes a request to an agent, how HTTP requests and
WebSocket connections are byte-forwarded to that agent's backend, and what
clients observe when the backend cannot answer.

An agent's backend is taken as given here: either its address is known and
reachable ("routable"), known but unreachable, or not yet known. How the
proxy learns and updates those addresses belongs to the planned
`discovery/` area. Likewise, the transport used to reach a remote agent's
host is out of scope except where it changes what a client observes -- a
failure to establish that transport is indistinguishable, by design, from
an unreachable backend.

Forwarding failures also have a machine-readable side channel on the
proxy's stdout, which embedding consumers use instead of the HTTP
responses; that contract is deliberately not specified in this area yet.
See `backend-errors.md`.
