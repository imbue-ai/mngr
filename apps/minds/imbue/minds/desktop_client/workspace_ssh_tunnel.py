"""Broker a reverse SSH tunnel from the minds hub into a calling workspace.

The cross-workspace SSH route hands a calling workspace a way to ``ssh`` into a
target workspace. When the target is *remote* its host is reachable from
anywhere and the caller connects directly. When the target is *local*
(Docker/Lima) its sshd is published only on the hub's own loopback
(``127.0.0.1:<published port>``), which a different container -- or a remote
caller -- cannot reach. The hub can reach both machines, so it brokers the
connection: it opens a reverse SSH tunnel into the caller's container (a fresh
loopback listener there) that relays, through the hub, to the target's hub-local
sshd. The caller then connects to ``127.0.0.1:<assigned port>`` with its own key.

This reuses ``mngr_forward``'s ``SSHTunnelManager`` -- the same reverse-tunnel
machinery the latchkey gateway forwarding uses -- so the only minds-specific
logic here is deciding when to broker and wrapping the manager's errors.
"""

import paramiko

from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager

# SSH hosts reachable only from the hub itself: a local Docker/Lima target
# publishes its sshd on one of these, so a peer (or remote) workspace cannot
# reach it directly and the hub must broker a tunnel. Any other host is a
# routable address the caller connects to directly.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class WorkspaceSshTunnelError(Exception):
    """Raised when brokering a reverse SSH tunnel into the calling workspace fails."""


def is_loopback_host(host: str) -> bool:
    """Whether an SSH host is a hub-only loopback address (not reachable by a peer workspace)."""
    return host.strip().lower() in _LOOPBACK_HOSTS


def broker_reverse_tunnel_into_caller(
    tunnel_manager: SSHTunnelManager,
    *,
    caller_ssh: RemoteSSHInfo,
    target_ssh: RemoteSSHInfo,
    target_agent_id: str,
) -> int:
    """Open (or reuse) a reverse tunnel so the caller can reach a hub-local target's sshd.

    The caller's container gets a fresh loopback listener that the hub relays to
    the target's hub-reachable sshd (``127.0.0.1:<target port>`` for a local
    target). Returns the loopback port assigned inside the caller's container;
    the caller connects there with its own key. The manager reuses an existing
    tunnel for the same caller + target port, so a re-request refreshes rather
    than stacks.
    """
    try:
        return tunnel_manager.setup_reverse_tunnel(
            caller_ssh,
            local_port=target_ssh.port,
            remote_port=0,
            agent_id=target_agent_id,
        )
    except (SSHTunnelError, OSError, paramiko.SSHException) as e:
        raise WorkspaceSshTunnelError(str(e)) from e
