# Backend errors: the stdout side channel

The scenarios in `backend-errors.feature` state only what the requesting
client observes. Each of these failure paths (and the loopback refusal in
this folder's `invariants.feature`, and the WebSocket failure closes in
`websockets.feature`) additionally emits a machine-readable failure event
on the proxy's stdout envelope stream -- a `system_interface_backend_failure`
payload whose reason distinguishes a backend that was never resolved, a
connection that could not be established, a response lost mid-stream, and
a backend that answered with a non-success status. Embedding consumers
(the minds desktop client) drive their recovery behavior from those events
rather than from the HTTP responses, which their users never see directly.

That stdout contract is deliberately not specified in this corpus yet. It
belongs to a planned `stream/` area covering the whole envelope stream:
the `login_url` and `listening` startup events, `resolver_snapshot`,
`reverse_tunnel_established`, the backend-failure events, and the
passthrough of discovery and per-agent event lines. Until that area
lands, the payload schemas in `imbue/mngr_forward/data_types.py` are the
reference for the stream's shape.

Two calibration notes for readers of the scenarios:

- The proxy's patience for a backend that accepted a connection but has
  not answered ("the proxy's timeout") is 30 seconds today; the normative
  content is that a wedged backend yields a gateway timeout rather than
  waiting forever.
- The "Loading workspace" page re-checks the workspace by background
  polling rather than by reloading itself, so an embedding host's overlay
  UI does not lose focus once per tick while a workspace boots.
