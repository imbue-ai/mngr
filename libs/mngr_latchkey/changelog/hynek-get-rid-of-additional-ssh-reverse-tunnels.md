# Minds-api-proxy: authorization injection + schema editing + baseline notification grant

The `minds-api-proxy` gateway extension now authenticates the
forwarded request *to* the upstream Minds API on the agent's behalf:

- It reads `LATCHKEY_EXTENSION_MINDS_API_KEY` on every request and,
  when set, overwrites the inbound `Authorization` header with
  `Bearer <LATCHKEY_EXTENSION_MINDS_API_KEY>` before forwarding.
  Agents therefore never see the key and cannot spoof one. With the
  env var unset, the inbound `Authorization` header is forwarded
  unchanged (used by tests).

The `permissions` extension grew matching CRUD for inline detent
schemas alongside its existing rule editor:

- `POST /permissions/schemas?path=<file>&schema_name=<name>` adds or
  replaces an inline schema. The body is a JSON object (the schema
  definition). Schema names must match the conservative pattern
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` so they round-trip safely through
  URL path segments and detent's name lookup.
- `DELETE /permissions/schemas?path=<file>&schema_name=<name>` removes
  the named schema.

These let minds install per-agent path-pattern schemas (`"only
`/minds-api-proxy/api/v1/agents/<agent_id>/...`"`) at agent-creation
time without having to direct-write the per-host permissions file
itself.

The agent baseline (`_AGENT_BASELINE_PERMISSIONS` in
`mngr_latchkey/agent_setup.py`) now ships an extra permission schema
out of the box: every minds-created agent can
`POST /minds-api-proxy/api/v1/agents/<...>/notifications`. New
helpers expose the per-agent scope / permission names + inline
schemas that the desktop client adds for each agent on top of the
baseline:

- `agent_minds_api_proxy_scope_name(agent_id)`
- `agent_minds_api_proxy_permission_name(agent_id)`
- `build_agent_minds_api_proxy_schemas(agent_id)`

`mngr_imbue_cloud/host.py`'s `build_combined_inject_command` /
`normalize_inject_args` no longer take a `minds_api_key` argument:
there is exactly one `MINDS_API_KEY` per minds installation now, the
latchkey gateway injects it transparently, and agents never see the
value -- so there is nothing to push down onto a leased pool host.

## Consolidation: shared `SSHTunnelManager`

The `SSHTunnelManager` (and `RemoteSSHInfo`, `ReverseTunnelInfo`,
`SSHTunnelError`) used to exist in two places: this package's own
`mngr_latchkey/ssh_tunnel.py` (driving the latchkey gateway's
reverse-into-each-agent tunnels) and the `mngr_forward` plugin's
`mngr_forward/ssh_tunnel.py` (driving forward + `--reverse` tunnels).
The two implementations were ~70% verbatim duplicates that diverged on
three things: latchkey added a per-tunnel exponential backoff for the
repair loop (capped at 5 minutes), an `agent_id` tag on each
`ReverseTunnelInfo`, and a `remove_reverse_tunnels_for_agent` cleanup
hook used by the destruction path.

All three latchkey improvements moved into the `mngr_forward` manager
(they're strictly better behavior for both callers), and
`mngr_latchkey/ssh_tunnel.py` is gone:

- `mngr_latchkey/discovery.py`, `cli.py`, `discovery_stream.py`,
  `discovery_stream_test.py`, and `core_test.py` now import
  `RemoteSSHInfo`, `SSHTunnelError`, `SSHTunnelManager` from
  `imbue.mngr_forward.ssh_tunnel` instead.
- The 635-line `mngr_latchkey/ssh_tunnel_test.py` has been
  consolidated into `mngr_forward/ssh_tunnel_test.py` (which now
  carries the previously-thin manager unit tests plus the new
  exponential-backoff + `remove_reverse_tunnels_for_agent` coverage).
- The reverse-tunnel repair loop in `mngr_forward` no longer uses a
  flat 30s retry; it uses per-tunnel exponential backoff with a 5min
  cap. Same recovery latency for healthy targets; much less wasted
  paramiko handshake against permanently-gone ones.
- `remove_reverse_tunnels_for_agent` is careful not to close an SSH
  client out from under any live *forward* tunnel using the same
  host, so the two flavors of tunnel can coexist on one connection.
