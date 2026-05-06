"""SSH-to-VPS-root helpers used by destroy/start/stop on the imbue_cloud provider.

Background: the connector's ``/hosts/lease`` endpoint installs the user's
public SSH key on BOTH the leased VPS root account (port 22) and the docker
container (``container_ssh_port``), using the management key. So once a lease
has succeeded, the same per-host private key the user already has is
sufficient to run docker commands as root on the VPS.

This module wraps that flow: open an SSH session to ``root@vps_ip:22`` using
the per-host private key from
``providers/imbue_cloud/<instance>/hosts/<host_id>/ssh_key`` and run the
requested ``docker stop``/``docker start``/``docker rm`` command against the
container labeled ``com.imbue.mngr.host-id=<host_id>`` (the canonical
``LABEL_HOST_ID`` from ``mngr_vps_docker``).
"""

import shlex
from pathlib import Path
from typing import Final

import paramiko
from loguru import logger

from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError

VPS_ROOT_SSH_PORT = 22
VPS_ROOT_USER = "root"
SSH_CONNECT_TIMEOUT_SECONDS = 30.0
DOCKER_OP_TIMEOUT_SECONDS = 60.0


def _load_private_key(private_key_path: Path) -> paramiko.PKey:
    """Load an SSH private key file as the right paramiko key type.

    Pool hosts get an Ed25519 keypair from ``save_ssh_keypair``'s RSA path or
    a per-host Ed25519 from ``load_or_create_host_keypair``; we try Ed25519
    first and fall back to RSA so both work without requiring the caller to
    know which is in play.
    """
    try:
        with private_key_path.open() as f:
            head = f.read(80)
    except OSError as exc:
        raise ImbueCloudConnectorError(f"Cannot read SSH private key {private_key_path}: {exc}") from exc
    if "OPENSSH" in head:
        return paramiko.Ed25519Key.from_private_key_file(str(private_key_path))
    return paramiko.RSAKey.from_private_key_file(str(private_key_path))


def _open_root_ssh(vps_ip: str, private_key_path: Path) -> paramiko.SSHClient:
    """Open an authenticated paramiko session to root@vps_ip:22.

    Caller is responsible for closing the returned client.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = _load_private_key(private_key_path)
    client.connect(
        hostname=vps_ip,
        port=VPS_ROOT_SSH_PORT,
        username=VPS_ROOT_USER,
        pkey=pkey,
        timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _run_root_command(client: paramiko.SSHClient, command: str, *, label: str) -> str:
    """Run a single command on the open SSH client and raise on non-zero exit.

    Returns combined stdout. A failure surfaces both stdout and stderr in the
    error message so the caller can debug docker error states (e.g.
    "no such container").
    """
    _stdin, stdout, stderr = client.exec_command(command, timeout=DOCKER_OP_TIMEOUT_SECONDS)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if exit_code != 0:
        raise ImbueCloudConnectorError(
            f"VPS root SSH command {label!r} failed (exit {exit_code}): stdout={out!r} stderr={err!r}"
        )
    return out


_DOCKER_HOST_ID_LABEL: Final[str] = "com.imbue.mngr.host-id"


def _container_id_for_host_filter(host_id: str) -> str:
    """Build a ``docker ps -aq --filter ...`` expression for the host's container.

    Pool hosts label the per-agent docker container with
    ``com.imbue.mngr.host-id=<id>`` at provisioning time (the
    ``LABEL_HOST_ID`` constant from ``mngr_vps_docker``); use that as
    the canonical identifier instead of assuming a specific container
    name format.
    """
    return f"docker ps -aq --filter label={_DOCKER_HOST_ID_LABEL}={shlex.quote(host_id)}"


def _resolve_container_id(client: paramiko.SSHClient, host_id: str) -> str | None:
    """Look up the docker container id for a host. Returns None when there is none."""
    output = _run_root_command(
        client,
        f"{_container_id_for_host_filter(host_id)} | head -1",
        label="resolve-container",
    )
    return output or None


def stop_container(vps_ip: str, host_id: str, private_key_path: Path) -> None:
    """SSH to root@vps_ip and stop the container labeled with this host_id.

    Idempotent: a missing or already-stopped container is treated as success
    so callers (mngr destroy, mngr stop) never have to special-case state.
    """
    client = _open_root_ssh(vps_ip, private_key_path)
    try:
        container_id = _resolve_container_id(client, host_id)
        if container_id is None:
            logger.debug("stop_container: no container for host {}; nothing to do", host_id)
            return
        _run_root_command(client, f"docker stop {shlex.quote(container_id)}", label="docker-stop")
        logger.debug("Stopped container {} for host {}", container_id, host_id)
    finally:
        client.close()


def start_container(vps_ip: str, host_id: str, private_key_path: Path) -> None:
    """SSH to root@vps_ip and start the container labeled with this host_id.

    Used by ``mngr start <agent>`` after a previous destroy. Raises if no
    container exists -- we cannot reconstitute one from outside the lease.
    """
    client = _open_root_ssh(vps_ip, private_key_path)
    try:
        container_id = _resolve_container_id(client, host_id)
        if container_id is None:
            raise ImbueCloudConnectorError(
                f"start_container: no docker container with label mngr-host-id={host_id} on {vps_ip}"
            )
        _run_root_command(client, f"docker start {shlex.quote(container_id)}", label="docker-start")
        logger.debug("Started container {} for host {}", container_id, host_id)
    finally:
        client.close()


def remove_container(vps_ip: str, host_id: str, private_key_path: Path) -> None:
    """SSH to root@vps_ip and remove the container labeled with this host_id.

    Used by ``delete_host`` to drop the on-disk container before the lease is
    released; the volumes are removed because the container holds them.
    """
    client = _open_root_ssh(vps_ip, private_key_path)
    try:
        container_id = _resolve_container_id(client, host_id)
        if container_id is None:
            logger.debug("remove_container: no container for host {}; nothing to do", host_id)
            return
        _run_root_command(client, f"docker rm -f -v {shlex.quote(container_id)}", label="docker-rm")
        logger.debug("Removed container {} for host {}", container_id, host_id)
    finally:
        client.close()


def is_container_running(vps_ip: str, host_id: str, private_key_path: Path) -> bool:
    """Return True if the labeled container exists and is running."""
    client = _open_root_ssh(vps_ip, private_key_path)
    try:
        output = _run_root_command(
            client,
            f"docker ps -q --filter label=mngr-host-id={shlex.quote(host_id)}",
            label="docker-ps",
        )
        return bool(output)
    finally:
        client.close()
