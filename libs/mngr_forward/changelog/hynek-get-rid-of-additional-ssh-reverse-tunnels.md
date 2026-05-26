# `SSHTunnelManager` is now the single SSH tunneling implementation

`mngr_forward/ssh_tunnel.py`'s `SSHTunnelManager` (and `RemoteSSHInfo`,
`ReverseTunnelInfo`, `SSHTunnelError`) absorbed the latchkey package's
parallel copy and is now the only SSH tunneling implementation in the
monorepo. Both the plugin's own forward (direct-tcpip) and reverse
(`--reverse REMOTE:LOCAL`) paths as well as the `mngr latchkey forward`
supervisor use it.

Behavior changes the existing forward-plugin callers will see:

- `ReverseTunnelInfo` gained an optional `agent_id: str | None = None`
  field; `setup_reverse_tunnel` gained an optional `agent_id`
  parameter. Existing callers can ignore both -- the default `None`
  matches the pre-change behavior.
- The reverse-tunnel repair loop now uses per-tunnel exponential
  backoff (1s, 2s, 4s, ..., capped at 5min) instead of the previous
  flat 30s cadence. A healthy target sees the same recovery latency;
  a permanently-gone target costs one paramiko handshake every five
  minutes instead of every 30s. Failures clear on a successful repair
  (or when a sibling tunnel on the same SSH host gets repaired and
  the connection comes back).
- New `remove_reverse_tunnels_for_agent(agent_id)` method tears down
  every reverse tunnel tagged with a given `agent_id`. It is careful
  not to close an SSH client out from under any live *forward* tunnel
  that shares the same host -- the two flavors of tunnel can coexist
  on one connection.

Public API additions are backward-compatible. The deleted
`mngr_latchkey/ssh_tunnel.py` is now re-exported transparently from
this module's existing public surface.
