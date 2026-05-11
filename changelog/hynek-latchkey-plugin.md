## mngr-latchkey: new package

Added a new `imbue-mngr-latchkey` workspace package that owns the
shared `latchkey gateway` lifecycle, per-agent latchkey wiring, and
the reverse SSH tunnel that bridges the host-side gateway into remote
agents. The minds desktop client used to host this logic in
`apps/minds/imbue/minds/desktop_client/`; it now imports the package
and keeps only its own UI-layer code (permission dialog, service
catalog, HTML templates).

The package is currently a plain Python library -- no `mngr` CLI
subcommands are registered yet.

### Python API

- `imbue.mngr_latchkey.core.Latchkey` -- single wrapper around the
  upstream `latchkey` CLI. Owns gateway spawn / adopt / stop, password
  derivation, JWT minting, services-info and auth-browser probes.
  `Latchkey.initialize()` now runs `latchkey --version` and refuses to
  continue if the installed binary is older than the new
  `LATCHKEY_MIN_VERSION = "2.9.0"` constant; misconfiguration surfaces
  immediately rather than at the first gateway spawn. Failures raise
  the new `LatchkeyVersionError` (subclass of `LatchkeyError`).
- `imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey` -- assembles
  the env vars an agent needs (`LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]`)
  and an opaque permissions handle. **Raises** on infrastructure
  failures (latchkey CLI broken, on-disk write failed); callers decide
  whether to abort agent creation or fall back to an empty setup.
- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` --
  replaces the opaque handle with a symlink to the canonical
  agent-keyed `latchkey_permissions.json` once `mngr create` has
  returned the canonical agent id. **Raises** `LatchkeyStoreError` on
  failure; same policy stance as above.
- `imbue.mngr_latchkey.discovery.LatchkeyDiscoveryHandler` -- agent
  discovery callback that ensures the shared gateway is up and opens
  a reverse SSH tunnel from `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` into
  the agent. Each tunnel is tagged with its agent id.
- `imbue.mngr_latchkey.discovery.LatchkeyDestructionHandler` -- agent
  destruction callback that drops the destroyed agent's reverse tunnel
  so the SSH-tunnel health-check loop doesn't keep spinning paramiko
  transports against a host that no longer exists.
- `imbue.mngr_latchkey.ssh_tunnel.SSHTunnelManager` -- reverse-tunnel
  manager with per-tunnel exponential backoff, agent-id tagging, and
  `remove_reverse_tunnels_for_agent`.
- `imbue.mngr_latchkey.store` -- on-disk persistence: gateway record,
  permissions config read/write, opaque-handle allocation, per-agent
  symlink linking.

### Layout

Plugin metadata lives under `<latchkey_directory>/mngr_latchkey/`, keeping
it cleanly segregated from anything the upstream `latchkey` CLI writes
under the shared `LATCHKEY_DIRECTORY`. Minds uses `~/.minds/latchkey`
as that root directory.

### Dependencies

- `imbue-mngr-forward` for the bidirectional socket/channel relay
  helper (`imbue.mngr_forward.relay`), keeping the half-closed-channel
  fix in a single place rather than duplicating it.

### Minds-side cleanups

- `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py`: removed the
  now-unused `SSHTunnelManager` and supporting types (`ReverseTunnelInfo`,
  `_TunnelFailureState`, `_ForwardedTunnelHandler`, relay imports,
  reverse-tunnel health-check / backoff constants, and the internal
  `_ssh_connection_*` helpers). Kept `RemoteSSHInfo`, `SSHTunnelError`,
  `open_ssh_client`, and `_create_ssh_client` -- still used by
  `backend_resolver.py`, `forward_cli.py`, and the `MindsRemoteSSHInfo`
  adapter in `cli/run.py`. The matching test files
  (`ssh_tunnel_test.py`, `test_ssh_tunnel_leak.py`) moved to the new
  package along with the manager.
- `cli/run.py` and `desktop_client/agent_creator.py` rewired to import
  the latchkey types and helpers from the plugin and wrap the
  raising plugin entry points (`prepare_agent_latchkey`,
  `finalize_agent_permissions`) in try/except blocks that log a
  warning and continue agent creation -- preserving the prior
  end-to-end behaviour where a misconfigured latchkey installation
  does not abort agent creation, but making the choice explicit at the
  call site rather than buried inside the library.
- Three minds `test_ratchets.py` snapshots tightened
  (`while_true 1->0`, `time_sleep 2->1`, `broad_exception_catch 1->0`)
  to reflect violations that went away with the deleted code.

### No user-visible behaviour change in minds itself.
