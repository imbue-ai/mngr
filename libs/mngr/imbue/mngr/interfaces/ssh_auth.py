import shlex
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Literal

import paramiko
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel


class SSHTransportCommand(FrozenModel):
    """SSH command string + env vars for rsync/git subprocesses."""

    command: str = Field(description="SSH command for rsync -e or GIT_SSH_COMMAND, e.g. 'ssh -i key -p 22'")
    env: dict[str, SecretStr] = Field(default_factory=dict, description="Environment variables (secrets masked)")


def expose_secrets_for_subprocess(env: dict[str, SecretStr]) -> dict[str, str]:
    """Unwrap SecretStr values for subprocess env dicts.

    DANGER: output must not be logged. Pass the result directly to the subprocess
    call and never assign it to a named variable that could be logged.
    """
    return {k: v.get_secret_value() for k, v in env.items()}


class SSHAuthMethod(FrozenModel, ABC):
    """Base class for SSH authentication methods.

    Subclass and set auth_type to a Literal string to register a new auth method.
    Registration is automatic via __init_subclass__. The auth_type discriminator
    enables Pydantic deserialization as a tagged union.

    Callers use auth objects polymorphically -- never inspect auth_type directly.
    """

    auth_type: str = Field(description="Discriminator for the SSH auth method type")

    _registry: ClassVar[dict[str, type["SSHAuthMethod"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Extract auth_type default from class annotations, since Pydantic's
        # model_fields is not yet populated when __init_subclass__ runs.
        auth_type_default = cls.__dict__.get("auth_type")
        # Skip if no default was set in this class (inherited or abstract)
        if auth_type_default is None or not isinstance(auth_type_default, str):
            return
        existing = SSHAuthMethod._registry.get(auth_type_default)
        if existing is not None and existing is not cls:
            raise TypeError(
                f"Duplicate SSHAuthMethod auth_type {auth_type_default!r}: "
                f"{cls.__name__} conflicts with {existing.__name__}"
            )
        SSHAuthMethod._registry[auth_type_default] = cls

    @abstractmethod
    def configure_pyinfra_host_data(self, host_data: dict[str, Any]) -> None:
        """Populate pyinfra host_data dict with auth-specific SSH settings."""
        ...

    @abstractmethod
    def build_transport_command(self, port: int, known_hosts_file: Path | None) -> SSHTransportCommand:
        """Build an SSH transport command for rsync -e or GIT_SSH_COMMAND."""
        ...

    @abstractmethod
    def connect_paramiko(self, client: paramiko.SSHClient, hostname: str, port: int, username: str) -> None:
        """Connect a paramiko SSHClient using this auth method."""
        ...

    @abstractmethod
    def get_display_command(self, user: str, hostname: str, port: int) -> str:
        """Return a human-readable SSH command string (safe for display, no secrets)."""
        ...


class SSHKeyAuth(SSHAuthMethod):
    """SSH key-based authentication."""

    auth_type: Literal["key"] = "key"
    key_path: Path = Field(description="Path to SSH private key")
    known_hosts_file: Path | None = Field(default=None, description="Path to known_hosts file")

    def configure_pyinfra_host_data(self, host_data: dict[str, Any]) -> None:
        """Populate pyinfra host_data with key-based SSH settings."""
        host_data["ssh_key"] = str(self.key_path)
        if self.known_hosts_file is not None:
            host_data["ssh_known_hosts_file"] = str(self.known_hosts_file)
            host_data["ssh_strict_host_key_checking"] = "yes"

    def build_transport_command(self, port: int, known_hosts_file: Path | None) -> SSHTransportCommand:
        """Build SSH transport command with key-based auth.

        The known_hosts_file parameter overrides self.known_hosts_file when provided,
        allowing callers to specify a different known_hosts file (e.g. from the host's
        connector data). When None, falls back to self.known_hosts_file.

        Always uses StrictHostKeyChecking=yes.
        """
        effective_known_hosts = known_hosts_file if known_hosts_file is not None else self.known_hosts_file
        parts = ["ssh", "-i", shlex.quote(str(self.key_path)), "-p", str(port)]
        if effective_known_hosts is not None:
            parts.extend(
                ["-o", f"UserKnownHostsFile={shlex.quote(str(effective_known_hosts))}", "-o", "StrictHostKeyChecking=yes"]
            )
        else:
            parts.extend(["-o", "StrictHostKeyChecking=yes"])
        return SSHTransportCommand(command=" ".join(parts))

    def connect_paramiko(self, client: paramiko.SSHClient, hostname: str, port: int, username: str) -> None:
        """Connect using key-based auth with strict host key checking."""
        if self.known_hosts_file is not None and self.known_hosts_file.exists():
            client.load_host_keys(str(self.known_hosts_file))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=hostname,
            port=port,
            username=username,
            key_filename=str(self.key_path),
            timeout=10.0,
        )

    def get_display_command(self, user: str, hostname: str, port: int) -> str:
        """Return a display-safe SSH command string."""
        return f"ssh -i {self.key_path} -p {port} {user}@{hostname}"


# Type alias for SSHAuthMethod fields in Pydantic models.
# Currently only SSHKeyAuth is built in. Plugin packages that add new auth types
# (e.g. SSHPasswordAuth) should register via __init_subclass__ for runtime dispatch;
# for Pydantic deserialization, extend this union by redefining SSHAuthField in
# the plugin package and using it in plugin-specific models.
SSHAuthField = SSHKeyAuth


class SSHConnectionInfo(FrozenModel):
    """SSH connection information for a remote host."""

    user: str = Field(description="SSH username")
    hostname: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    auth: SSHAuthField = Field(description="SSH authentication method")
