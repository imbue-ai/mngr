import base64
import socket
import time
from pathlib import Path

import paramiko
from loguru import logger
from paramiko.pkey import PKey

from imbue.imbue_common.logging import log_span
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_vps_docker.errors import VpsProvisioningError

_TOFU_CONNECT_BACKOFF_SECONDS: float = 5.0
_TOFU_CONNECT_BANNER_TIMEOUT_SECONDS: float = 30.0


class _SilentAcceptHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Paramiko policy that silently accepts any host key on first sight.

    Equivalent to ``StrictHostKeyChecking=accept-new`` semantics: the
    first connection lets the handshake proceed without verification.
    Callers read the actually-presented key out of the live transport
    via ``client.get_transport().get_remote_server_key()`` and pin it
    to a strict ``known_hosts`` file before any further connections.
    """

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: PKey) -> None:
        pass


def pin_host_key_via_tofu(
    *,
    hostname: str,
    port: int,
    ssh_user: str,
    private_key_path: Path,
    known_hosts_path: Path,
    timeout_seconds: float,
) -> str:
    """Open one SSH session with TOFU host-key acceptance, pin the key, return it.

    On the very first connection to a freshly-rebuilt OVH VPS we have no
    way to know the host key out-of-band (OVH exposes no cloud-init
    userData and no fingerprint API). This helper:

    1. Polls SSH until the daemon accepts our key auth.
    2. Captures the host key the server presented during that handshake.
    3. Writes a strict ``known_hosts`` entry for ``hostname:port`` so all
       subsequent connections via mngr's normal SSH machinery verify
       strictly against that key.

    Because key-auth is already enforced (the rebuild pre-installed our
    public key), a MITM during this window can passively read the
    session but cannot impersonate the VPS to us. See the README for
    the full caveat.

    Returns the OpenSSH-formatted public host key string that was pinned.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    private_key = paramiko.Ed25519Key.from_private_key_file(str(private_key_path))

    while time.monotonic() < deadline:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_SilentAcceptHostKeyPolicy())
        try:
            client.connect(
                hostname=hostname,
                port=port,
                username=ssh_user,
                pkey=private_key,
                allow_agent=False,
                look_for_keys=False,
                timeout=10.0,
                banner_timeout=_TOFU_CONNECT_BANNER_TIMEOUT_SECONDS,
                auth_timeout=15.0,
            )
        except (paramiko.SSHException, socket.error, socket.timeout, EOFError, OSError) as e:
            last_error = e
            time.sleep(_TOFU_CONNECT_BACKOFF_SECONDS)
            continue
        try:
            transport = client.get_transport()
            captured_key = transport.get_remote_server_key() if transport is not None else None
            if captured_key is None:
                raise VpsProvisioningError(f"Connected to {hostname}:{port} but paramiko did not report a host key")
            host_key_str = _format_openssh_public_key(captured_key)
            add_host_to_known_hosts(
                known_hosts_path=known_hosts_path,
                hostname=hostname,
                port=port,
                public_key=host_key_str,
            )
            logger.info("Pinned OVH VPS host key for {}:{} ({})", hostname, port, captured_key.get_name())
            return host_key_str
        finally:
            client.close()
    raise VpsProvisioningError(
        f"Could not SSH into {ssh_user}@{hostname}:{port} within {timeout_seconds}s to pin the host key "
        f"(last error: {last_error!r})"
    )


def _format_openssh_public_key(key: PKey) -> str:
    """Render a paramiko ``PKey`` as ``<type> <base64>`` for known_hosts."""
    encoded = base64.b64encode(key.asbytes()).decode("ascii")
    return f"{key.get_name()} {encoded}"


def wait_for_ssh_after_rebuild(
    *,
    hostname: str,
    port: int,
    timeout_seconds: float,
) -> None:
    """Block until SSH on the post-rebuild VPS is reachable enough to handshake.

    Used as a guard before ``pin_host_key_via_tofu`` so the TOFU attempts
    don't burn budget on a still-rebooting host.
    """
    with log_span("Waiting for OVH VPS SSH after rebuild ({}:{})", hostname, port):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((hostname, port), timeout=5.0):
                    return
            except OSError:
                time.sleep(2.0)
    raise VpsProvisioningError(f"SSH on {hostname}:{port} not reachable within {timeout_seconds}s")
