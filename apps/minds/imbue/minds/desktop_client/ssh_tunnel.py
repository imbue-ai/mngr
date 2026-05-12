"""Tiny SSH helpers minds still needs after the tunnel manager moved out.

The :class:`SSHTunnelManager` and its reverse-tunnel machinery used to
live in this file but were genuinely only used by the latchkey
discovery flow, which now goes through :mod:`imbue.mngr_latchkey`.
The forward-tunnel manager that ``mngr forward`` uses lives in
:mod:`imbue.mngr_forward.ssh_tunnel`.

What remains here is the small bit minds itself still calls directly:

* :class:`RemoteSSHInfo` -- the model for an SSH endpoint, used by
  :mod:`imbue.minds.desktop_client.backend_resolver`,
  :mod:`imbue.minds.desktop_client.forward_cli`, and the
  ``MindsRemoteSSHInfo`` adapter in :mod:`imbue.minds.cli.run`.
* :func:`open_ssh_client` -- public wrapper used by
  ``forward_cli.MindsApiUrlWriter`` to write ``minds_api_url`` on
  remote agent hosts without taking on a private dependency.
"""

from pathlib import Path

import paramiko
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class RemoteSSHInfo(FrozenModel):
    """SSH connection info for a remote agent host."""

    user: str = Field(description="SSH username (e.g. 'root')")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key file")


class SSHTunnelError(Exception):
    """Raised when an SSH tunnel operation fails."""

    ...


def open_ssh_client(ssh_info: RemoteSSHInfo) -> paramiko.SSHClient:
    """Open a paramiko SSH client to the given host using the cached known_hosts.

    Public wrapper around the internal ``_create_ssh_client`` helper. Used
    by ``forward_cli.MindsApiUrlWriter`` to write ``minds_api_url`` on
    remote agent hosts without depending on a private symbol.
    """
    return _create_ssh_client(ssh_info)


def _create_ssh_client(ssh_info: RemoteSSHInfo) -> paramiko.SSHClient:
    """Create a paramiko SSH connection to the given host.

    Uses the known_hosts file from the same directory as the SSH key (this is
    where mngr stores it for each provider). Falls back to AutoAddPolicy if
    no known_hosts file is found.
    """
    client = paramiko.SSHClient()

    known_hosts_path = ssh_info.key_path.parent / "known_hosts"
    if known_hosts_path.exists():
        client.load_host_keys(str(known_hosts_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        logger.warning("No known_hosts file at {}, using AutoAddPolicy", known_hosts_path)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=ssh_info.host,
        port=ssh_info.port,
        username=ssh_info.user,
        key_filename=str(ssh_info.key_path),
        timeout=10.0,
    )

    return client
