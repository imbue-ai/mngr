# VPS workspaces reach their gateway over the docker bridge, not an SSH tunnel

A VPS-backed workspace now reaches its VPS-resident latchkey gateway at
`http://host.docker.internal:1989`. The gateway binds the VPS's docker bridge
address (never a public interface), and the workspace container resolves that
name to it through the `--add-host` mapping the VPS provider now creates every
container with. The VPS->container reverse SSH tunnel, the ad-hoc keypair it
authenticated with, and the `docker exec` that authorized it are no longer set
up for such containers.

Desktop-gateway workspaces are unchanged: their gateway URL stays
`http://127.0.0.1:1989`, reverse-tunneled in from the desktop.

Compatibility: a remote workspace whose `LATCHKEY_GATEWAY` still names its own
loopback keeps the reverse tunnel, now pointed at the bridge address the
gateway binds; neither the URL nor the container's mapping can change for the
life of the container. Provisioning recognizes such a workspace by a container
created without the mapping (inspected from its creation-time extra hosts), or
by a tunnel an earlier provisioning registered on the VPS -- which is how a
workspace an older client created in a container that already carried the
mapping is told apart from a new one. A registered tunnel is therefore never
dropped, and no tunnel is ever registered for a new workspace. Each kept tunnel
is logged. The tunnel code is marked `CLEANUP:` for removal once no such
workspace remains.

Rollout: the URL `mngr latchkey create-agent-env` emits is decided from the
gateway location alone, before the host exists, while the imbue_cloud fast path
adopts a pre-baked pool-host container as-is, so a pool host baked by an mngr
without the mapping hands out containers that cannot resolve the new URL. The
plugin therefore now hooks `mngr create` (`on_host_created`, after the host env
is written and before any agent starts): when the host's `LATCHKEY_GATEWAY`
names `host.docker.internal` but the container's `/etc/hosts` does not map it,
the URL is rewritten to `http://127.0.0.1:1989`, which is what provisioning
serves over the reverse tunnel it keeps for exactly such containers. Pool hosts
need no re-bake before a client carrying this change is promoted; until they
are re-baked, workspaces created on them simply keep the tunnel. Workspaces an
older client creates on a re-baked pool host in the meantime keep working after
that client upgrades (their tunnel is kept).

`prepare_agent_latchkey` / `mngr latchkey create-agent-env --gateway-location
VPS` emit the new URL. `imbue.mngr.primitives.OUTER_HOST_HOSTNAME_IN_CONTAINER`
(shared with the VPS provider, which builds its `--add-host` mapping from it)
names the host, and the docker-bridge address resolution the owner-exec VM
daemon already used moved to a shared `docker_bridge.py`; a provisioning pass
resolves the address once and binds both the daemon and the gateway to it.
