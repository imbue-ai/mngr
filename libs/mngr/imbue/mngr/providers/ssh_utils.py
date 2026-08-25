import fcntl
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from loguru import logger
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory
from pyinfra.connectors.sshuserclient.client import get_host_keys

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import clear_endpoint_pins
from imbue.mngr.providers.host_key_store import format_as_known_hosts_address
from imbue.mngr.providers.host_key_store import has_host_key_store
from imbue.mngr.providers.host_key_store import pin_host_key
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.polling import poll_until

# A word on vocabulary, since "SSH protocol banner" turns up all over this file.
# The banner is the server's identification string: the plaintext line, something
# like "SSH-2.0-OpenSSH_9.6", that an SSH server sends the instant the TCP
# connection opens, before any key exchange. RFC 4253 section 4.2 calls this the
# Protocol Version Exchange. paramiko reads the line in Transport._check_banner,
# and if the line never arrives or the peer hangs up before sending it, paramiko
# raises SSHException("Error reading SSH protocol banner").
#
# That is a different thing from the RFC 4252 SSH_MSG_USERAUTH_BANNER, the
# human-readable "authorized users only" notice some servers print during login.
# People call that one an "SSH banner" too, but it comes later in the handshake
# and is governed by auth_timeout, so this constant has nothing to do with it.
#
# This constant sets how long paramiko waits for the banner after the TCP connect,
# and we pass it to every provider SSH connection through ssh_paramiko_connect_kwargs.
# paramiko defaults to 15 seconds. Degraded Modal sandbox tunnels were measured
# hovering right at that line, 13 to 16 seconds end to end per exec on 2026-08-17,
# so at the default every connection dies with "Error reading SSH protocol banner"
# however many times you retry it. Thirty seconds gives a slow but working tunnel
# room to answer. A genuinely dead endpoint still fails fast at the TCP layer,
# refused or unreachable, which this timeout does not stretch.
SSH_BANNER_TIMEOUT_SECONDS: Final[float] = 30.0


def _generate_ed25519_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair, serialized as text.

    Returns a tuple of (private_key_pem, public_key_openssh): the private key
    in the OpenSSH container format (PEM-armored) and the public key as an
    OpenSSH one-line entry.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_key_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )
    return private_key_pem, public_key_openssh


def generate_ssh_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair for SSH client authentication.

    Ed25519 rather than RSA so the same client key also works with services
    that only accept Ed25519 signatures (e.g. the minds workspaces' owner-exec
    envelope auth). Every consumer auto-detects the key type, and existing RSA
    keypairs on disk keep working -- load_or_create_ssh_keypair only generates
    when no pair exists.

    Returns a tuple of (private_key_pem, public_key_openssh).
    """
    return _generate_ed25519_keypair()


def save_ssh_keypair(key_dir: Path, key_name: str = "ssh_key") -> tuple[Path, Path]:
    """Generate and save an SSH keypair to the specified directory.

    Both files are written atomically (temp file + ``os.replace``) so a
    concurrent reader never observes a truncated key or public-key file. This
    matters because the public-key file is probed by pyinfra/paramiko on every
    SSH connection (as a possible certificate), and a half-written ``.pub``
    raises ``ValueError: Not enough fields for public blob``.

    Returns a tuple of (private_key_path, public_key_path).
    """
    key_dir.mkdir(parents=True, exist_ok=True)

    private_key_path = key_dir / key_name
    public_key_path = key_dir / f"{key_name}.pub"

    private_key_pem, public_key_openssh = generate_ssh_keypair()

    atomic_write(private_key_path, private_key_pem)
    private_key_path.chmod(0o600)
    atomic_write(public_key_path, public_key_openssh)
    public_key_path.chmod(0o644)

    return private_key_path, public_key_path


def load_or_create_ssh_keypair(key_dir: Path, key_name: str = "ssh_key") -> tuple[Path, str]:
    """Load an existing SSH keypair or create a new one if it doesn't exist.

    Creation is serialized with an exclusive file lock so that concurrent
    callers (e.g. the parallel host-discovery fan-out, which opens one SSH
    connection per VPS and lazily creates this keypair on first use) do not
    each generate and write a different keypair over the top of one another --
    which previously produced a transient zero-byte / mismatched ``.pub`` and a
    ``ValueError`` deep in paramiko's certificate probe. Exactly one caller
    creates the pair; the rest wait, then read the completed files.

    Returns a tuple of (private_key_path, public_key_content).
    """
    private_key_path = key_dir / key_name
    public_key_path = key_dir / f"{key_name}.pub"

    if private_key_path.exists() and public_key_path.exists():
        return private_key_path, public_key_path.read_text().strip()

    key_dir.mkdir(parents=True, exist_ok=True)
    lock_path = key_dir / f".{key_name}.lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        # Re-check under the lock: another caller may have created the pair
        # while we waited to acquire it.
        if not (private_key_path.exists() and public_key_path.exists()):
            save_ssh_keypair(key_dir, key_name)
    return private_key_path, public_key_path.read_text().strip()


def generate_ed25519_host_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair for SSH host key.

    Returns a tuple of (private_key_pem, public_key_openssh).
    Ed25519 is preferred for SSH host keys due to its security and performance.
    """
    return _generate_ed25519_keypair()


def load_or_create_host_keypair(key_dir: Path, key_name: str = "host_key") -> tuple[Path, str]:
    """Load an existing SSH host keypair or create a new one if it doesn't exist.

    This key is used as the SSH host key for containers/sandboxes, allowing us
    to pre-trust the key and avoid host key verification prompts.

    Returns a tuple of (private_key_path, public_key_content).
    """
    key_dir.mkdir(parents=True, exist_ok=True)

    private_key_path = key_dir / key_name
    public_key_path = key_dir / f"{key_name}.pub"

    if private_key_path.exists() and public_key_path.exists():
        return private_key_path, public_key_path.read_text().strip()

    private_key_pem, public_key_openssh = generate_ed25519_host_keypair()

    private_key_path.write_text(private_key_pem)
    private_key_path.chmod(0o600)

    public_key_path.write_text(public_key_openssh)
    public_key_path.chmod(0o644)

    return private_key_path, public_key_openssh


# Subdirectory of a provider instance's key dir holding per-host keys. Host
# keys are unique per host -- a host key proves "you reached the host you
# expected", so reusing one across hosts would let a party who holds it
# impersonate any sibling host. Per-host *client* keys keep synced workspace
# records free of provider-wide material: a record carries only a key that
# opens its one host, never one that opens all of the user's hosts.
_PER_HOST_KEY_SUBDIR: Final[str] = "host_keys"


def per_host_key_dir(base_key_dir: Path, host_id: HostId) -> Path:
    """Directory holding ``host_id``'s unique keypairs under a provider key dir."""
    return base_key_dir / _PER_HOST_KEY_SUBDIR / host_id.get_uuid().hex


def load_or_create_per_host_host_keypair(base_key_dir: Path, host_id: HostId, key_name: str) -> tuple[Path, str]:
    """Load-or-create ``host_id``'s unique sshd host keypair under ``base_key_dir``.

    A fresh host always gets its own keypair, so a host key can never be reused
    to impersonate a different host. Deliberately never falls back to a legacy
    provider-global key -- read fallbacks belong to the resolution helpers used
    on paths that serve hosts created before per-host keys existed.
    """
    return load_or_create_host_keypair(per_host_key_dir(base_key_dir, host_id), key_name)


def load_or_create_per_host_client_keypair(
    base_key_dir: Path, host_id: HostId, key_name: str, known_hosts_file_name: str
) -> tuple[Path, str]:
    """Load-or-create ``host_id``'s unique SSH client keypair under ``base_key_dir``.

    Used at host creation so every new host is opened by its own client key;
    lock-serialized against concurrent creation like the shared-keypair path.
    The key dir also gets a ``known_hosts`` symlink to the provider-wide
    ``known_hosts_file_name`` (see ``ensure_per_host_known_hosts_link``) so
    key-sibling consumers keep finding the pinned host keys.
    """
    keypair = load_or_create_ssh_keypair(per_host_key_dir(base_key_dir, host_id), key_name)
    ensure_per_host_known_hosts_link(base_key_dir, host_id, known_hosts_file_name)
    return keypair


def resolve_keypair_with_fallback(
    preferred_dir: Path,
    fallback_dir: Path,
    key_name: str,
    create: Callable[[], tuple[Path, str]],
) -> tuple[Path, str]:
    """Shared resolution order: complete pair in ``preferred_dir``, then in ``fallback_dir``, then ``create()``.

    The per-host/legacy resolution used by every provider: the preferred dir is
    the host's own key dir, the fallback dir holds the legacy shared pair that
    hosts created before per-host keys existed still authorize, and ``create``
    mints a fresh preferred pair when neither is on disk.
    """
    preferred_private = preferred_dir / key_name
    preferred_public = preferred_dir / f"{key_name}.pub"
    if preferred_private.exists() and preferred_public.exists():
        return preferred_private, preferred_public.read_text().strip()
    fallback_private = fallback_dir / key_name
    fallback_public = fallback_dir / f"{key_name}.pub"
    if fallback_private.exists() and fallback_public.exists():
        return fallback_private, fallback_public.read_text().strip()
    return create()


def resolve_per_host_client_keypair(
    base_key_dir: Path, host_id: HostId, key_name: str, known_hosts_file_name: str
) -> tuple[Path, str]:
    """Resolve the client keypair that opens ``host_id``: per-host first, legacy shared as fallback.

    Hosts created before per-host client keys existed only authorize the legacy
    shared key, so it wins when no per-host pair is on disk. When neither
    exists a fresh per-host pair is created (per-host is the canonical layout
    going forward). Whenever the per-host pair wins, its key dir gets a
    ``known_hosts`` symlink to the provider-wide ``known_hosts_file_name`` (see
    ``ensure_per_host_known_hosts_link``) -- this also retrofits the link onto
    per-host dirs minted before the link existed.
    """
    private_key_path, public_key = resolve_keypair_with_fallback(
        per_host_key_dir(base_key_dir, host_id),
        base_key_dir,
        key_name,
        lambda: load_or_create_per_host_client_keypair(base_key_dir, host_id, key_name, known_hosts_file_name),
    )
    if private_key_path.parent != base_key_dir:
        ensure_per_host_known_hosts_link(base_key_dir, host_id, known_hosts_file_name)
    return private_key_path, public_key


def resolve_per_host_host_keypair(base_key_dir: Path, host_id: HostId, key_name: str) -> tuple[Path, str]:
    """Resolve the sshd host keypair for ``host_id``: per-host first, legacy shared as fallback.

    Used on re-injection paths (restart/restore) so a host created before
    per-host host keys existed keeps serving the key already pinned for it,
    instead of churning keys on every restart.
    """
    return resolve_keypair_with_fallback(
        per_host_key_dir(base_key_dir, host_id),
        base_key_dir,
        key_name,
        lambda: load_or_create_per_host_host_keypair(base_key_dir, host_id, key_name),
    )


def ensure_per_host_known_hosts_link(base_key_dir: Path, host_id: HostId, known_hosts_file_name: str) -> None:
    """Ensure ``host_id``'s key dir has a ``known_hosts`` symlink to the provider-wide file.

    Consumers that are handed only a private key path derive the pinned-host-keys
    file as its sibling ``known_hosts`` (the forward SSH tunnel does this, per
    the long-standing "mngr stores it next to the key" convention). A per-host
    client key would otherwise sit in a dir with no known_hosts and fail strict
    host-key checking. The symlink keeps the sibling convention true while the
    provider-wide file remains the single rendered artifact (renders write
    through symlinks, so the link never goes stale).
    """
    link_path = per_host_key_dir(base_key_dir, host_id) / "known_hosts"
    if link_path.is_symlink() or link_path.exists():
        return
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(Path("..") / ".." / known_hosts_file_name)
    except FileExistsError:
        # A concurrent caller created it between the check and the symlink.
        pass


def read_host_public_key_with_legacy_fallback(base_key_dir: Path, host_id: HostId, key_name: str) -> str | None:
    """Return ``host_id``'s public host key: per-host if present, else the legacy shared key.

    Read-only (creates nothing). Used by read paths that must reproduce the key
    the *running* host actually serves: the per-host key for hosts created
    after per-host keys landed, or the provider-global key for older hosts.
    Returns ``None`` when neither exists.
    """
    per_host_public_key_path = per_host_key_dir(base_key_dir, host_id) / f"{key_name}.pub"
    if per_host_public_key_path.exists():
        return per_host_public_key_path.read_text().strip()
    legacy_public_key_path = base_key_dir / f"{key_name}.pub"
    if legacy_public_key_path.exists():
        return legacy_public_key_path.read_text().strip()
    return None


def clear_host_from_known_hosts(
    known_hosts_path: Path,
    hostname: str,
    port: int,
) -> None:
    """Remove all entries for a host:port from the known_hosts file.

    When a host-key pin store exists for the file, the endpoint's pins are
    dropped from the store and the file is re-rendered from it. Otherwise the
    legacy behavior applies: if the file does not exist, returns without
    error; else takes an exclusive lock, drops any line whose leading host
    pattern matches the given host:port, and rewrites the file in place if any
    line was removed.
    """
    if has_host_key_store(known_hosts_path):
        clear_endpoint_pins(known_hosts_path, hostname, port)
        return

    if not known_hosts_path.exists():
        return

    host_pattern = format_as_known_hosts_address(hostname, port)

    with open(known_hosts_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        lines = f.readlines()
        new_lines = [line for line in lines if not line.startswith(f"{host_pattern} ")]
        if len(new_lines) != len(lines):
            f.seek(0)
            f.truncate()
            f.writelines(new_lines)
            f.flush()
            os.fsync(f.fileno())


def add_host_to_known_hosts(
    known_hosts_path: Path,
    hostname: str,
    port: int,
    public_key: str,
    host_id: HostId | None = None,
    origin: HostKeyOrigin = HostKeyOrigin.BOOTSTRAP,
    is_add_if_absent: bool = False,
) -> None:
    """Add a host entry to the known_hosts file.

    The entry format is: [hostname]:port key_type base64_key
    This allows SSH to verify the host key without prompting.

    When ``host_id`` is given -- or a host-key pin store already exists for
    the file -- the pin is written through the store (attributed to that
    host's record) and the known_hosts file is re-rendered from it, so the
    file becomes a derived artifact governed by the store's origin-precedence
    rules. Passing ``is_add_if_absent`` makes an existing same-endpoint+keytype
    pin win outright (only meaningful on the store path).

    Callers that pass neither get the legacy direct-write behavior (throwaway
    known_hosts files stay sidecar-free): file locking to prevent races, and
    replace-per-(host:port, keytype) line semantics.
    """
    if host_id is not None or has_host_key_store(known_hosts_path):
        pin_host_key(
            known_hosts_path,
            hostname,
            port,
            public_key,
            host_id=host_id,
            origin=origin,
            is_add_if_absent=is_add_if_absent,
        )
        return

    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)

    host_pattern = format_as_known_hosts_address(hostname, port)

    # The public key should already be in OpenSSH format: "ssh-ed25519 AAAA..."
    entry = f"{host_pattern} {public_key}\n"

    # Use file locking to prevent race conditions.
    # The lock is released automatically when the file is closed on exit of the with block.
    with open(known_hosts_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

        # Read existing content to check if entry already exists
        f.seek(0)
        existing_content = f.read()

        # Check if this exact entry already exists
        if entry.strip() not in existing_content:
            # Remove any existing entry for this host with the same key type
            # (might be stale), but preserve entries with different key types
            # so that multiple key types can coexist for the same host.
            key_type = public_key.split()[0]
            entry_prefix = f"{host_pattern} {key_type} "
            lines = existing_content.splitlines(keepends=True)
            new_lines = [line for line in lines if not line.startswith(entry_prefix)]
            new_lines.append(entry)

            # Rewrite the file
            f.seek(0)
            f.truncate()
            f.writelines(new_lines)

        # Ensure the file is flushed to disk before we return
        # This prevents race conditions where paramiko reads a stale version
        f.flush()
        os.fsync(f.fileno())


def wait_for_sshd(hostname: str, port: int, timeout_seconds: float = 60.0) -> None:
    """Wait for sshd to be ready to accept connections.

    Attempts a full SSH transport handshake (key exchange) rather than just
    checking for the SSH protocol banner. This prevents race conditions where the
    banner is available but the key exchange hasn't completed yet, which causes
    "No existing session" errors on the subsequent real connection.
    """
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        transport = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(5.0, max(1.0, timeout_seconds - (time.time() - start_time))))
            sock.connect((hostname, port))
            transport = paramiko.Transport(sock)
            transport.connect()
            return
        except (socket.error, socket.timeout, paramiko.SSHException, EOFError, OSError):
            pass
        finally:
            if transport is not None:
                try:
                    transport.close()
                except (OSError, paramiko.SSHException):
                    pass
            else:
                sock.close()
    raise MngrError(f"SSH server not ready after {timeout_seconds}s at {hostname}:{port}")


def _can_authenticate_to_server(
    hostname: str,
    port: int,
    private_key_path: Path,
    username: str,
    timeout_seconds: float,
) -> bool:
    """Check if we can authenticate and open a session to the SSH server.

    A full SSH connection with session open verifies the server is ready to
    handle requests, not just accepting TCP connections. The username must be
    passed explicitly: paramiko defaults to the local OS user, which is almost
    never the user the server's key is authorized for. The wait for the SSH
    protocol banner is the shared SSH_BANNER_TIMEOUT_SECONDS rather than the
    per-attempt budget because degraded tunnels (e.g. Modal) are slow specifically
    at sending that banner.
    """
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=hostname,
            port=port,
            username=username,
            key_filename=str(private_key_path),
            timeout=timeout_seconds,
            auth_timeout=timeout_seconds,
            banner_timeout=SSH_BANNER_TIMEOUT_SECONDS,
        )
        transport = client.get_transport()
        if transport is None:
            return False
        transport.open_session(timeout=timeout_seconds)
        return True
    except (socket.error, socket.timeout, paramiko.SSHException, EOFError, OSError) as e:
        logger.trace("SSH session-open probe to {}:{} as user {} failed: {}", hostname, port, username, e)
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except (OSError, paramiko.SSHException):
                pass


def wait_for_sshd_with_retry(
    hostname: str,
    port: int,
    timeout_seconds: float = 60.0,
    private_key_path: Path | None = None,
    *,
    username: str,
) -> None:
    """Wait for sshd to be ready, optionally verifying it can open sessions.

    First waits for the SSH transport handshake, then optionally verifies
    the server can actually open authenticated sessions. This absorbs
    cold-start latency where the tunnel is available but sshd is still
    initializing. ``username`` is the SSH user the private key authenticates
    as (only used when ``private_key_path`` is given).
    """
    wait_for_sshd(hostname, port, timeout_seconds)

    if private_key_path is not None:
        if not poll_until(
            lambda: _can_authenticate_to_server(hostname, port, private_key_path, username, 5.0),
            timeout=timeout_seconds,
            poll_interval=0.5,
        ):
            raise MngrError(
                f"SSH server at {hostname}:{port} accepted connections but could not open sessions "
                f"after {timeout_seconds}s (sshd may still be initializing)"
            )


def parse_openssh_public_key_blob(public_key: str) -> tuple[str, str]:
    """Split an OpenSSH public key line into its (key_type, base64_blob).

    ``"ssh-ed25519 AAAA... comment"`` -> ``("ssh-ed25519", "AAAA...")``. The
    optional trailing comment is ignored.
    """
    parts = public_key.split()
    if len(parts) < 2:
        raise MngrError(f"Malformed OpenSSH public key (expected '<type> <base64> [comment]'): {public_key!r}")
    return parts[0], parts[1]


def _server_presents_host_key(hostname: str, port: int, expected_type: str, expected_blob: str) -> bool:
    """Return True iff the server at hostname:port currently serves the expected host key.

    One handshake attempt: any connection/SSH error (including a non-matching key)
    is a clean False so the caller can keep polling.
    """
    transport = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(5.0)
        sock.connect((hostname, port))
        transport = paramiko.Transport(sock)
        transport.connect()
        remote_key = transport.get_remote_server_key()
        return remote_key.get_name() == expected_type and remote_key.get_base64() == expected_blob
    except (socket.error, socket.timeout, paramiko.SSHException, EOFError, OSError):
        return False
    finally:
        if transport is not None:
            try:
                transport.close()
            except (OSError, paramiko.SSHException):
                pass
        else:
            sock.close()


def is_server_presenting_host_key(hostname: str, port: int, public_key: str) -> bool:
    """One-shot probe: whether the server at ``hostname:port`` currently serves exactly ``public_key``.

    Unauthenticated (only a transport handshake), so it can decide "which key is
    live" even when the caller's pins or credentials are stale. Any
    connection/SSH error is a clean False.
    """
    expected_type, expected_blob = parse_openssh_public_key_blob(public_key)
    return _server_presents_host_key(hostname, port, expected_type, expected_blob)


def wait_for_expected_host_key(
    hostname: str,
    port: int,
    expected_host_public_key: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Poll until the server presents exactly ``expected_host_public_key``, then return.

    For backends that install the host key after sshd starts (a GCE
    ``startup-script``, unlike cloud-init which sets it pre-sshd), the server briefly
    serves a boot-generated key first. Waiting here, before the strict-checked
    provisioning connection, avoids a mismatch abort. Not TOFU: only the exact
    expected key is accepted. Raises ``MngrError`` on timeout.
    """
    expected_type, expected_blob = parse_openssh_public_key_blob(expected_host_public_key)
    if not poll_until(
        lambda: _server_presents_host_key(hostname, port, expected_type, expected_blob),
        timeout=timeout_seconds,
        poll_interval=poll_interval_seconds,
    ):
        raise MngrError(
            f"Server at {hostname}:{port} did not present the expected SSH host key within {timeout_seconds}s"
        )


def create_pyinfra_host(
    hostname: str,
    port: int,
    private_key_path: Path,
    known_hosts_path: Path,
    ssh_user: str = "root",
) -> PyinfraHost:
    """Create a pyinfra host with SSH connector.

    Clears pyinfra's memoized known_hosts cache to ensure fresh reads,
    since we add new entries dynamically.
    """
    get_host_keys.cache.clear()

    host_data = {
        "ssh_user": ssh_user,
        "ssh_port": port,
        "ssh_key": str(private_key_path),
        "ssh_known_hosts_file": str(known_hosts_path),
        "ssh_strict_host_key_checking": "yes",
        "ssh_paramiko_connect_kwargs": {"banner_timeout": SSH_BANNER_TIMEOUT_SECONDS},
    }

    names_data = ([(hostname, host_data)], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)

    pyinfra_host = inventory.get_host(hostname)
    pyinfra_host.init(state)

    return pyinfra_host
