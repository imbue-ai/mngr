# Forwarding

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This area covers the agent-origin request path: how the Host header routes a request to an agent and its services, how HTTP requests and WebSocket connections are byte-forwarded to a backend, and what a client observes when a backend cannot answer.

An agent's backend is taken as given here: its address is known and reachable, known but unreachable, or not yet known.
How the proxy learns and updates that address is the discovery path, a later area of this corpus; the transport used to reach a backend on another host is likewise out of scope, except where it changes what a client observes -- a failure to establish that transport is indistinguishable, by design, from an unreachable backend.

Every backend-failure path here also has a machine-readable side on the proxy's stdout stream, which an embedding consumer drives its recovery from instead of the HTTP responses.
That side channel is not specified in this area yet; see `backend-errors.md`.
