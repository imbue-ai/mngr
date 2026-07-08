import base64
import socket
import time
from pathlib import Path

import paramiko
from loguru import logger
from paramiko.pkey import PKey

from imbue.imbue_common.logging import log_span
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr_vps.errors import VpsProvisioningError

_SSH_CONNECT_BACKOFF_SECONDS: float = 5.0
_SSH_CONNECT_BANNER_TIMEOUT_SECONDS: float = 30.0
_SSH_CONNECT_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    paramiko.SSHException,
    socket.error,
    socket.timeout,
    EOFError,
    OSError,
)

_ROOT_AUTHORIZED_KEYS_COPY_COMMAND: str = (
    "set -e && "
    "sudo -n install -m 700 -o root -g root -d /root/.ssh && "
    "sudo -n install -m 600 -o root -g root "
    "~/.ssh/authorized_keys /root/.ssh/authorized_keys"
)
_ROOT_VERIFY_COMMAND: str = 'test "$(whoami)" = root && echo OK'


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
    client = _connect_with_retry(
        hostname=hostname,
        port=port,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        known_hosts_path=None,
        timeout_seconds=timeout_seconds,
        failure_label=f"SSH as {ssh_user} for host-key TOFU",
    )
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


def bootstrap_root_authorized_keys_via_user(
    *,
    hostname: str,
    port: int,
    bootstrap_user: str,
    private_key_path: Path,
    known_hosts_path: Path,
    timeout_seconds: float,
) -> None:
    """Copy the rebuild SSH key from ``bootstrap_user``'s account to ``root``'s.

    OVH's Debian-family VPS images (verified: ``Debian 12 - Docker``)
    install the rebuild ``publicSshKey`` into the image's default
    non-root user (``debian``), not into ``/root/.ssh/authorized_keys``.
    The default user is in the ``sudo`` group with passwordless sudo and
    in the ``docker`` group, so it can bootstrap root login itself.

    mngr operates as root everywhere downstream (the base
    ``VpsProvider`` opens its outer SSH sessions as ``root``,
    ``docker_over_ssh`` shells out as ``root``, etc.), so this helper
    bridges the OVH-default and the mngr expectation by sudo-copying
    the authorized_keys file into root's home before any other code
    tries to connect as root.

    Assumes the host key is already pinned in ``known_hosts_path`` --
    call ``pin_host_key_via_tofu`` first with the same
    ``bootstrap_user`` so the strict host-key verification here matches.

    Idempotent: running twice produces the same on-disk state.
    """
    with log_span("Bootstrapping root SSH via {}@{}:{}", bootstrap_user, hostname, port):
        client = _connect_with_retry(
            hostname=hostname,
            port=port,
            ssh_user=bootstrap_user,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=timeout_seconds,
            failure_label=f"SSH as {bootstrap_user} to bootstrap root SSH",
        )
        try:
            _run_or_raise(
                client,
                _ROOT_AUTHORIZED_KEYS_COPY_COMMAND,
                failure_label="copy authorized_keys to /root",
            )
            logger.info("Bootstrapped root SSH on {} (copied {}'s authorized_keys)", hostname, bootstrap_user)
        finally:
            client.close()


def verify_root_ssh(
    *,
    hostname: str,
    port: int,
    private_key_path: Path,
    known_hosts_path: Path,
    timeout_seconds: float,
) -> None:
    """Open one SSH session as ``root`` and confirm the bootstrap worked.

    Run after ``bootstrap_root_authorized_keys_via_user`` to fail loudly
    here -- with a clear error message naming SSH-as-root -- rather than
    deep inside the first ``DockerOverSsh`` call that assumes root works.
    """
    client = _connect_with_retry(
        hostname=hostname,
        port=port,
        ssh_user="root",
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
        timeout_seconds=timeout_seconds,
        failure_label="SSH as root (post-bootstrap verification; check sshd PermitRootLogin if this fails)",
    )
    try:
        _run_or_raise(client, _ROOT_VERIFY_COMMAND, failure_label="root smoke-test")
        logger.info("Verified SSH as root works on {}:{}", hostname, port)
    finally:
        client.close()


def _connect_with_retry(
    *,
    hostname: str,
    port: int,
    ssh_user: str,
    private_key_path: Path,
    known_hosts_path: Path | None,
    timeout_seconds: float,
    failure_label: str,
) -> paramiko.SSHClient:
    """Open an SSH session with retry-on-transient-errors; return the connected client.

    Caller owns the returned client (must call ``.close()`` when done).

    When ``known_hosts_path`` is None, uses ``_SilentAcceptHostKeyPolicy``
    (TOFU semantics; required for the very first connection to a
    freshly-rebuilt VPS where no host key has been pinned yet). When a
    path is given, strict host-key verification is enforced against that
    file; any unknown key raises.

    Centralises the retry loop so we have exactly one ``time.sleep``
    across all SSH-with-retry call sites in this module.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    private_key = _load_private_key(private_key_path)

    while time.monotonic() < deadline:
        client = paramiko.SSHClient()
        if known_hosts_path is None:
            client.set_missing_host_key_policy(_SilentAcceptHostKeyPolicy())
        else:
            client.load_host_keys(str(known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=hostname,
                port=port,
                username=ssh_user,
                pkey=private_key,
                allow_agent=False,
                look_for_keys=False,
                timeout=10.0,
                banner_timeout=_SSH_CONNECT_BANNER_TIMEOUT_SECONDS,
                auth_timeout=15.0,
            )
        except _SSH_CONNECT_RETRY_EXCEPTIONS as e:
            last_error = e
            # paramiko's SSHClient.close() warns that GC-based cleanup is
            # unreliable and may hang at end-of-process; the loop creates a
            # fresh client per iteration, so explicitly close the failed
            # one before retrying to avoid stacking zombie Transport
            # threads when auth takes several attempts to succeed. The
            # narrow except guards against paramiko teardown errors
            # masking the retryable error we're about to recover from.
            try:
                client.close()
            except (OSError, paramiko.SSHException, EOFError):
                pass
            time.sleep(_SSH_CONNECT_BACKOFF_SECONDS)
            continue
        return client

    raise VpsProvisioningError(
        f"OVH bootstrap step {failure_label!r} on {hostname}:{port} did not succeed within "
        f"{timeout_seconds}s (last error: {last_error!r})"
    )


def _load_private_key(private_key_path: Path) -> paramiko.PKey:
    """Load an SSH private key by trying each supported key type in turn.

    The base ``VpsProvider`` produces SSH keypairs via
    ``ssh_utils.load_or_create_ssh_keypair`` -> ``generate_ssh_keypair``,
    which currently returns an **RSA** key in TraditionalOpenSSL PEM
    format. paramiko's per-class ``from_private_key_file`` constructors
    are strict: ``Ed25519Key.from_private_key_file`` raises if the file
    isn't an OpenSSH-format Ed25519 key, even though paramiko itself can
    handle RSA fine. Rather than hardcode either type (which would break
    if the base class swaps generator), try each type and use the one
    that parses; this keeps the OVH provider working regardless of which
    key flavor the base class produces.
    """
    last_error: paramiko.SSHException | None = None
    for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_class.from_private_key_file(str(private_key_path))
        except paramiko.SSHException as e:
            last_error = e
    raise VpsProvisioningError(
        f"Could not parse SSH private key at {private_key_path} as any supported type "
        f"(Ed25519, RSA, ECDSA); last paramiko error: {last_error!r}"
    )


def _run_or_raise(
    client: paramiko.SSHClient,
    command: str,
    *,
    failure_label: str,
    command_timeout_seconds: float = 60.0,
) -> None:
    """Execute ``command`` over the open SSH session; raise on non-zero exit.

    Captures stderr into the raised error to make remote sudo failures
    diagnosable without re-running. ``command_timeout_seconds`` is the
    per-read channel timeout; bump it for commands that may sit idle
    between bursts of output (e.g. ``apt-get install`` waiting for a
    slow mirror handshake before the first byte).
    """
    _stdin, stdout, stderr = client.exec_command(command, timeout=command_timeout_seconds)
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        err = stderr.read().decode("utf-8", errors="replace").strip()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        raise VpsProvisioningError(
            f"OVH bootstrap step {failure_label!r} failed (exit={exit_status}): stderr={err!r} stdout={out!r}"
        )
