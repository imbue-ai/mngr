"""SSH endpoint data model used by the minds desktop client.

The actual tunnel manager lives in :mod:`imbue.mngr_forward.ssh_tunnel`
(used by both the ``mngr forward`` plugin's forward + reverse paths and
by the ``mngr latchkey forward`` supervisor's reverse-tunnel-the-gateway
path). Minds itself only needs the connection-info model -- the
``open_ssh_client`` / ``_create_ssh_client`` helpers and the
``SSHTunnelError`` exception that used to live here were the last
callers of paramiko in this package, and both went away with the
``MindsApiUrlWriter`` removal.
"""

from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class RemoteSSHInfo(FrozenModel):
    """SSH connection info for a remote agent host."""

    user: str = Field(description="SSH username (e.g. 'root')")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key file")
