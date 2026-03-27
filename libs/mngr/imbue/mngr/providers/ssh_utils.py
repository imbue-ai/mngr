import fcntl
import os
import socket
import time
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory
from pyinfra.connectors.sshuserclient.client import get_host_keys

from imbue.mngr.errors import MngrError


def generate_ssh_keypair() -> tuple[str, str]:
    """Generate a new RSA keypair for SSH authentication.

    Returns a tuple of (private_key_pem, public_key_openssh).
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
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


def save_ssh_keypair(key_dir: Path, key_name: str = "ssh_key") -> tuple[Path, Path]:
    """Generate and save an SSH keypair to the specified directory.

    Returns a tuple of (private_key_path, public_key_path).
    """
    key_dir.mkdir(parents=True, exist_ok=True)

    private_key_path = key_dir / key_name
    public_key_path = key_dir / f"{key_name}.pub"

    private_key_pem, public_key_openssh = generate_ssh_keypair()

    private_key_path.write_text(private_key_pem)
    private_key_path.chmod(0o600)

    public_key_path.write_text(public_key_openssh)
    public_key_path.chmod(0o644)

    return private_key_path, public_key_path


def load_or_create_ssh_keypair(key_dir: Path, key_name: str = "ssh_key") -> tuple[Path, str]:
    """Load an existing SSH keypair or create a new one if it doesn't exist.

    Returns a tuple of (private_key_path, public_key_content).
    """
    private_key_path = key_dir / key_name
    public_key_path = key_dir / f"{key_name}.pub"

    if private_key_path.exists() and public_key_path.exists():
        return private_key_path, public_key_path.read_text().strip()

    save_ssh_keypair(key_dir, key_name)
    return private_key_path, public_key_path.read_text().strip()


def generate_ed25519_host_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair for SSH host key.

    Returns a tuple of (private_key_pem, public_key_openssh).
    Ed25519 is preferred for SSH host keys due to its security and performance.
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


def add_host_to_known_hosts(
    known_hosts_path: Path,
    hostname: str,
    port: int,
    public_key: str,
) -> None:
    """Add a host entry to the known_hosts file.

    The entry format is: [hostname]:port key_type base64_key
    This allows SSH to verify the host key without prompting.

    Uses file locking to prevent race conditions when multiple processes
    try to update the known_hosts file simultaneously.
    """
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)

    # Format the host entry - use [host]:port format for non-standard ports
    if port == 22:
        host_pattern = hostname
    else:
        host_pattern = f"[{hostname}]:{port}"

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
            # Also check if we already have an entry for this host (might be stale)
            # and remove it before adding the new one
            lines = existing_content.splitlines(keepends=True)
            new_lines = [line for line in lines if not line.startswith(f"{host_pattern} ")]
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
    checking for the SSH banner. This prevents race conditions where the banner
    is available but the key exchange hasn't completed yet, which causes
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


def create_pyinfra_host(hostname: str, port: int, private_key_path: Path, known_hosts_path: Path) -> PyinfraHost:
    """Create a pyinfra host with SSH connector.

    Clears pyinfra's memoized known_hosts cache to ensure fresh reads,
    since we add new entries dynamically.
    """
    get_host_keys.cache.clear()

    host_data = {
        "ssh_user": "root",
        "ssh_port": port,
        "ssh_key": str(private_key_path),
        "ssh_known_hosts_file": str(known_hosts_path),
        "ssh_strict_host_key_checking": "yes",
    }

    names_data = ([(hostname, host_data)], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)

    pyinfra_host = inventory.get_host(hostname)
    pyinfra_host.init(state)

    return pyinfra_host
