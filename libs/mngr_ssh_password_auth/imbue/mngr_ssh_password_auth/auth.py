import shlex
import shutil
from pathlib import Path
from typing import Any
from typing import Literal

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.interfaces.ssh_auth import SSHAuthMethod
from imbue.mngr.interfaces.ssh_auth import SSHConnectionError
from imbue.mngr.interfaces.ssh_auth import SSHTransportCommand


class SSHPasswordAuth(SSHAuthMethod):
    """SSH password-based authentication.

    Uses sshpass for CLI transport (rsync, git) and paramiko's password auth
    for direct connections. The password is stored as SecretStr to prevent
    accidental logging.

    Provider plugins that need password auth (e.g. Daytona, Proxmox) add
    imbue-mngr-ssh-password-auth as a dependency. The auto-registration via
    __init_subclass__ makes the type available for Pydantic deserialization
    as soon as this module is imported.
    """

    auth_type: Literal["password"] = "password"
    password: SecretStr = Field(description="SSH password (stored as SecretStr, masked in logs and serialization)")
    known_hosts_file: Path | None = Field(
        default=None, description="Path to known_hosts file for host key verification"
    )

    def configure_pyinfra_host_data(self, host_data: dict[str, Any]) -> None:
        """Populate pyinfra host_data with password-based SSH settings.

        Disables key-based auth to force password authentication.
        """
        host_data["ssh_password"] = self.password.get_secret_value()
        host_data["ssh_look_for_keys"] = False
        host_data["ssh_allow_agent"] = False
        if self.known_hosts_file is not None:
            host_data["ssh_known_hosts_file"] = str(self.known_hosts_file)
            host_data["ssh_strict_host_key_checking"] = "yes"

    def build_transport_command(self, port: int, known_hosts_file: Path | None) -> SSHTransportCommand:
        """Build SSH transport command using sshpass for password auth.

        sshpass -e reads the password from the SSHPASS environment variable,
        keeping it out of the process argument list (not visible in `ps` output).

        Raises BinaryNotInstalledError if sshpass is not found on the system.
        """
        if shutil.which("sshpass") is None:
            raise BinaryNotInstalledError(
                binary="sshpass",
                purpose="SSH password authentication",
                install_hint="Install with: apt-get install sshpass (Debian/Ubuntu) or brew install hudochenkov/sshpass/sshpass (macOS)",
            )

        effective_known_hosts = known_hosts_file if known_hosts_file is not None else self.known_hosts_file
        ssh_parts = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=yes"]
        if effective_known_hosts is not None:
            ssh_parts.extend(["-o", f"UserKnownHostsFile={shlex.quote(str(effective_known_hosts))}"])
        # Disable key-based auth to avoid confusing failures
        ssh_parts.extend(["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"])

        command = f"sshpass -e {' '.join(ssh_parts)}"
        env = {"SSHPASS": SecretStr(self.password.get_secret_value())}

        return SSHTransportCommand(command=command, env=env)

    def connect_paramiko(self, client: paramiko.SSHClient, hostname: str, port: int, username: str) -> None:
        """Connect using password auth with host key checking.

        Uses RejectPolicy when a known_hosts file is available. Falls back to
        AutoAddPolicy with a warning when no known_hosts file exists.
        Disables key-based auth lookups to avoid confusing failures.
        """
        if self.known_hosts_file is not None and self.known_hosts_file.exists():
            client.load_host_keys(str(self.known_hosts_file))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            logger.warning(
                "No known_hosts file available (path={}), using AutoAddPolicy -- host key not verified",
                self.known_hosts_file,
            )
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=self.password.get_secret_value(),
                look_for_keys=False,
                allow_agent=False,
                timeout=10.0,
            )
        except (paramiko.SSHException, OSError) as e:
            raise SSHConnectionError(
                f"SSH password auth connection to {username}@{hostname}:{port} failed: {type(e).__name__}"
            ) from e

    def get_display_command(self, user: str, hostname: str, port: int) -> str:
        """Return a display-safe SSH command string (no password shown)."""
        return f"ssh -p {port} {user}@{hostname}"
