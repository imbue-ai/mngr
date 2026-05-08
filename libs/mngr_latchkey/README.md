# mngr-latchkey

Latchkey gateway management for [mngr](https://github.com/imbue-ai/mngr).

This plugin owns the lifecycle of a single shared `latchkey gateway`
subprocess and the per-agent state that points the gateway at each
agent's own permissions file. It exposes both a CLI command and a
Python API so other code (e.g. the minds desktop client) can drive
the same gateway and the same permissions store.

## CLI

```bash
mngr latchkey ensure-gateway [--latchkey-dir PATH]
```

Idempotent. Starts the shared `latchkey gateway` if it is not already
running; otherwise adopts the existing one. Persists its record under
the supplied directory (default: `<profile>/latchkey` inside the active
mngr profile directory) so subsequent in-process API users on the same
machine see the same gateway. The same directory is also used as the
spawned subprocess's `LATCHKEY_DIRECTORY` (credential / config store).

## Python API

```python
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.agent_setup import (
    prepare_agent_latchkey,
    finalize_agent_permissions,
)
from imbue.mngr_latchkey.tunnel import LatchkeyTunnelManager

latchkey = Latchkey(latchkey_directory=data_dir / "latchkey")
latchkey.initialize(data_dir=data_dir)

# (a) Pre-create env vars + opaque permissions handle for a new agent.
setup = prepare_agent_latchkey(latchkey, data_dir=data_dir, is_tunneled=True)
# setup.env: LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]
# setup.opaque_permissions_path: pass to finalize_agent_permissions later

# ... mngr create returns the canonical agent id ...

# (b) Point the opaque handle at the canonical agent permissions path.
finalize_agent_permissions(data_dir, setup.opaque_permissions_path, agent_id)

# (c) For tunneled agents (containers, VMs, VPS), open a reverse SSH
# tunnel from the agent's loopback into the host-side gateway.
tunnel_manager = LatchkeyTunnelManager()
tunnel_manager.start_health_check()
tunnel_manager.setup_for_agent(latchkey, ssh_info)
```

## Permissions config

The plugin owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). UI surfaces (such as the minds permission
dialog) read and update it via the helpers in `store.py`.
