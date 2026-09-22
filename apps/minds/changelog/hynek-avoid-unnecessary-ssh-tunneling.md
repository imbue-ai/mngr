# Latchkey remote-workspace release test follows the docker-bridge gateway route

`test_latchkey_e2e.py` now expects a VPS workspace's gateway at
`http://host.docker.internal:1989` and creates its fake-VPS container with the
`--add-host host.docker.internal:host-gateway` mapping the VPS provider adds by
default (the docker provider the test stands a VPS in with does not), so it
exercises the route production remote workspaces take, and asserts that no
VPS->container SSH tunnel is registered on the VPS and nothing is tunneled onto
the container's own loopback port. No product code in minds changed; remote
workspaces get the new URL from `mngr_latchkey` at create time.

`docs/latchkey-permissions.md` no longer says every workspace sees its gateway
at the same loopback URL: it now names the per-location URL (loopback for a
local workspace, `host.docker.internal` for a remote one) and notes that a
remote workspace whose container predates that mapping (one adopted from a
pool host baked by an older mngr) keeps the loopback URL and the reverse SSH
tunnel, which `mngr_latchkey` decides inside `mngr create`. Pool hosts need no
re-bake before a desktop carrying this change is promoted.
