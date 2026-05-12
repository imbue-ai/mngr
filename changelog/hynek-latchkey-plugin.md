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

## mngr-latchkey: register a `mngr latchkey` CLI surface

The `imbue-mngr-latchkey` package now ships as a proper `mngr` plugin:
it declares a `[project.entry-points.mngr]` entry point and registers
a `mngr latchkey` command group with three subcommands, plus a
`[plugins.latchkey]` settings.toml block. Users can wire latchkey to
agents end-to-end from the shell, without the minds desktop app.

### New CLI

- `mngr latchkey forward` -- long-running foreground supervisor.
  Spawns the shared `latchkey gateway` subprocess, consumes
  `mngr observe`'s discovery stream, and sets up / tears down a
  reverse SSH tunnel for every agent on a remote host. Stops the
  shared gateway on `SIGINT`/`SIGTERM` (coupled lifetime).
- `mngr latchkey create-agent-env` -- one-shot. Wraps
  `prepare_agent_latchkey(is_tunneled=True)` and emits
  `{"env": {...}, "opaque_permissions_path": "..."}` on stdout as a
  single JSON object. Always emits the constant agent-side loopback
  URL (`http://127.0.0.1:1989`); there is no DEV / on-host mode.
- `mngr latchkey link-permissions --agent-id ID --opaque-path PATH` --
  one-shot. Wraps `finalize_agent_permissions` to swing the opaque
  handle's symlink to the canonical agent-keyed permissions path.

Intentionally not in scope: `ensure-gateway`/`stop-gateway` (lifecycle
is internal to `forward`), `latchkey auth ...` wrappers, permissions
editing, agent-include / agent-exclude filtering on `forward`. Users
who need credential management run upstream `latchkey` directly.

### New settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via `MNGR_LATCHKEY_DIRECTORY` and
`MNGR_LATCHKEY_BINARY` env vars and matching `--latchkey-directory` /
`--latchkey-binary` CLI flags. Precedence is CLI > env > settings.toml
> built-in default.

### Failure semantics

Any `LatchkeyError` / `LatchkeyStoreError` raised by the underlying
library surfaces as a non-zero exit; `create-agent-env` does not fall
back to the empty-env degraded mode the library tolerates.

### Implementation notes

New modules under `libs/mngr_latchkey/imbue/mngr_latchkey/`:
`plugin.py` (entry point), `cli.py` (the three subcommands +
settings-precedence resolver), `config.py` (`LatchkeyPluginConfig`),
`discovery_stream.py` (a small `mngr observe`-driven dispatcher that
fans the relevant events out to `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler`). `testing.py` lifts the existing
`_FakeLatchkey` test double to a shared `FakeLatchkey` so the new
`cli_test.py` and the existing `agent_setup_test.py` share one
implementation.

## mngr-latchkey: `LatchkeyForwardSupervisor` and minds rewiring

Follow-up after the `mngr latchkey` CLI plugin.

### `LatchkeyForwardSupervisor`

New class in `imbue.mngr_latchkey.forward_supervisor`. Owns the
lifecycle of a single detached `mngr latchkey forward` subprocess for
a given `latchkey_directory`:

- `ensure_running()` -- idempotent. Spawns a fresh detached supervisor
  if no record exists; adopts the existing one if its PID is still
  alive and its cmdline matches `mngr latchkey forward`; otherwise
  discards the stale record and spawns a fresh one. Mirrors the same
  on-disk reconciliation pattern `Latchkey` already uses for the
  gateway.
- `stop()` -- SIGTERMs the supervisor (which cascades into
  coupled-lifetime shutdown of the shared gateway + reverse tunnels)
  and deletes the record.
- `get_forward_info()` -- read-only inspection of the on-disk
  `LatchkeyForwardInfo` record.

New on-disk record at `<plugin_data_dir>/latchkey_forward.json` and a
log file at `<plugin_data_dir>/latchkey_forward.log`, with matching
`save_forward_info` / `load_forward_info` / `delete_forward_info`
helpers in `store.py`. The spawn helper sits in `_spawn.py` next to
the existing `spawn_detached_latchkey_gateway`; the cmdline-based
liveness probe guards against PID reuse.

### `mngr latchkey forward` now also accepts the common settings flags

`--latchkey-directory` and `--latchkey-binary` were available on
`create-agent-env` and `link-permissions` but not on `forward` (an
oversight in the original PR). They are now uniformly available on
all three subcommands.

### minds: spawn `mngr latchkey forward` as a detached subprocess

`apps/minds/imbue/minds/cli/run.py` no longer constructs
`SSHTunnelManager` / `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler` in-process; it instead calls
`LatchkeyForwardSupervisor.ensure_running()` at startup, which spawns
the canonical `mngr latchkey forward` process detached. Minds does
*not* call `supervisor.stop()` on shutdown -- the supervisor keeps
running across desktop-client restarts and the next minds session
adopts it. This matches how minds already treated the underlying
`latchkey gateway` subprocess.

Side effect: the `_LatchkeyDiscoveryAdapter` class in `cli/run.py` is
gone, plus its supporting `MindsRemoteSSHInfo` / `AgentId` imports.

### Quieter logs from one-shot CLI subcommands

`Latchkey.initialize()` no longer logs "Adopted existing shared
Latchkey gateway" / "Discarding stale ..." at INFO level. Both lines
are now DEBUG, so one-shot invocations of `mngr latchkey create-agent-env`
and `mngr latchkey link-permissions` (which `initialize()` but never
touch the gateway) no longer emit a misleading line on stderr.

## mngr-latchkey: drop the on-disk gateway record

With `LatchkeyForwardSupervisor` guaranteeing at most one `mngr
latchkey forward` process per latchkey directory, the only thing that
ever spawns a `latchkey gateway` is that single supervised forward
subprocess. Cross-process gateway adoption -- the original reason for
persisting a `LatchkeyGatewayInfo` record at `<plugin_data_dir>/latchkey_gateway.json`
-- is no longer needed.

Changes:

- `Latchkey.initialize()` no longer reads or reconciles a persisted
  gateway record. It still runs `latchkey --version` so misconfiguration
  surfaces eagerly.
- `Latchkey.ensure_gateway_started()` no longer persists / restores
  state across processes; it stays in-process-idempotent (subsequent
  calls return the cached `self._info`).
- `Latchkey.stop_gateway()` no longer deletes a record; just terminates
  the in-memory tracked subprocess.
- `imbue.mngr_latchkey.store`: removed `save_gateway_info`,
  `load_gateway_info`, `delete_gateway_info`, `gateway_info_path`, and
  the `_GATEWAY_RECORD_FILENAME` constant. `LatchkeyGatewayInfo`
  itself stays as the in-memory return-type for the spawn path.
- `imbue.mngr_latchkey.core`: removed `_is_info_alive`,
  `_cmdline_looks_like_latchkey_gateway`, and the
  `_LIVENESS_CONNECT_TIMEOUT_SECONDS` constant. The `_is_port_listening`
  helper now takes a `timeout` argument from its (one remaining)
  caller, `_wait_for_port_listening`, which is still used after spawn
  to wait for the gateway to bind its port.

Trade-off: if `mngr latchkey forward` crashes (SIGKILL, OOM, segfault)
without running its SIGTERM cleanup path, the gateway becomes an
orphan. The orphan keeps its port bound but no reverse tunnel still
points at it (those died with the previous forward's paramiko
clients), so the orphan is just an idle process. The next supervisor
call spawns a fresh forward + fresh gateway on a fresh port; the
orphan can be cleaned up with `pkill latchkey`.

## mngr-latchkey: spawn the gateway via ConcurrencyGroup, simplify Latchkey API

Now that the gateway is only ever spawned by `mngr latchkey forward`
(a long-running supervised process), the detached-process /
on-disk-record machinery was overkill. Switched the gateway over to
standard `ConcurrencyGroup.run_process_in_background` and stripped the
lifecycle surface down to what the two production callers actually
need.

API changes:

- `Latchkey.ensure_gateway_started()` -> `Latchkey.start_gateway(cg)`.
  Takes the owning `ConcurrencyGroup` as an explicit argument (the CG's
  `__exit__` is what terminates the gateway). In-process idempotent.
- Replaced the `LatchkeyGatewayInfo` return value with simpler
  in-instance state and accessors:
  - `Latchkey.is_gateway_running` -- boolean.
  - `Latchkey.gateway_port` -- int (raises `LatchkeyNotInitializedError`
    when no gateway is running).
  - `Latchkey.gateway_url` -- `http://<listen_host>:<gateway_port>`.
- `LatchkeyGatewayInfo` itself is gone (was the in-memory return shape
  with `host`/`port`/`started_at`; replaced by the properties above).
- `Latchkey.get_gateway_info()` removed; callers use `is_gateway_running`
  / `gateway_port` directly.

Internals:

- `spawn_detached_latchkey_gateway` removed from `_spawn.py`. The
  remaining detached helpers are `ensure-browser` (Chromium download
  that should outlive a quick forward restart) and `mngr latchkey forward`
  itself (the supervisor adopts across embedder restarts).
- Gateway output is captured by a small `_GatewayLogWriter` `MutableModel`
  that tees per-line into the same `<plugin_data_dir>/latchkey_gateway.log`
  the detached path used. The CG's standard pipe-based output capture
  replaces the old direct stdout/stderr-to-file redirection.
- Cross-process adoption helpers (`_is_info_alive`,
  `_cmdline_looks_like_latchkey_gateway`, `_terminate_pid`,
  `_LIVENESS_CONNECT_TIMEOUT_SECONDS`) are gone -- the supervisor wrapper
  already enforces "at most one forward per directory" and adoption
  inside that single process is now just a boolean check.

Callers updated:

- `LatchkeyDiscoveryHandler.__call__` reads the host-side port via
  `latchkey.gateway_port` after calling `start_gateway`.
- `_forward_command` (cli.py) passes `mngr_ctx.concurrency_group` to
  `start_gateway` and logs `latchkey.gateway_url`.
- `prepare_agent_latchkey` accepts an optional `concurrency_group`
  argument; raises `LatchkeyError` if `is_tunneled=False` is used
  without one (the only path that actually spawns a gateway from
  inside this helper).
- `FakeLatchkey` in `testing.py` mirrors the new `start_gateway`
  signature.

## mngr-latchkey: do not let `mngr latchkey forward` die with its parent

`_forward_command` was calling `start_parent_death_watcher`, which polls
`os.getppid()` every ~3 seconds and SIGTERMs the process when the
original parent dies and the process gets reparented to PID 1. That
actively defeats the detached-supervisor pattern: when minds spawns
`mngr latchkey forward` with `start_new_session=True` and then exits,
the watcher saw the reparent and shut the gateway down within ~3
seconds.

Removed the watcher call. To still handle the *interactive* case (user
runs `mngr latchkey forward` in a terminal and closes the terminal),
SIGHUP is now wired into the same signal handler as SIGINT/SIGTERM,
so a terminal close triggers the clean coupled-lifetime shutdown path
rather than killing the python interpreter under the default handler
(which would leave the gateway orphaned).
