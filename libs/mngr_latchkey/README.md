# mngr-latchkey

Latchkey gateway management for [mngr](https://github.com/imbue-ai/mngr).

This package owns the lifecycle of a single shared `latchkey gateway`
subprocess and the per-host state that points the gateway at each
host's own permissions file. Per-host state is keyed by the canonical
host name and accompanied by a `host-id` file so a host with the same
name that has been recreated since the last permission grant has its
stale grants cleared automatically. It is a plain Python library:
import the classes and call them directly; there is no `mngr` CLI
surface yet.

## Python API

```python
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.agent_setup import (
    prepare_agent_latchkey,
    finalize_agent_permissions,
)
from imbue.mngr_latchkey.discovery import (
    LatchkeyDiscoveryHandler,
    LatchkeyDestructionHandler,
)
from imbue.mngr_latchkey.ssh_tunnel import SSHTunnelManager

latchkey = Latchkey(
    latchkey_binary="/path/to/latchkey",  # default: "latchkey" on PATH
    latchkey_directory=root_dir,
)
latchkey.initialize()

# (a) Pre-create env vars + per-host permissions file for a new agent.
setup = prepare_agent_latchkey(latchkey, host_name, is_tunneled=True)
# setup.env: LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]

# ... mngr create returns the canonical host id ...

# (b) Reconcile the per-host ``host-id`` file. If the recorded value
# differs from ``host_id`` (or no file exists yet) the prior
# permissions are treated as stale and cleared.
finalize_agent_permissions(latchkey, host_name, host_id)
# Raises LatchkeyStoreError on failure -- callers decide whether to abort
# or just surface a warning.

# (c) Plug the discovery and destruction handlers into your agent
# discovery stream so reverse tunnels are opened on discovery and
# closed on destruction.
tunnel_manager = SSHTunnelManager()
tunnel_manager.start_reverse_tunnel_health_check()
on_discovered = LatchkeyDiscoveryHandler(
    latchkey=latchkey, tunnel_manager=tunnel_manager, concurrency_group=cg
)
on_destroyed = LatchkeyDestructionHandler(tunnel_manager=tunnel_manager)
```

The `latchkey_directory` is used both as the upstream `LATCHKEY_DIRECTORY`
for spawned `latchkey` subprocesses and as the root of this package's own
metadata subdirectory (`<latchkey_directory>/mngr_latchkey/`, accessible
via `Latchkey.plugin_data_dir`).

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). UI surfaces (such as the minds permission
dialog) read and update it via the helpers in
`imbue.mngr_latchkey.store`.
