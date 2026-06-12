from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName


class SmolvmNotInstalledError(ProviderUnavailableError):
    """Raised when the smolvm binary is not found on PATH."""

    def __init__(self, provider_name: ProviderInstanceName, smolvm_command: str) -> None:
        super().__init__(
            provider_name,
            f"smolvm is not installed ('{smolvm_command}' not found on PATH). "
            "Install smolvm or point providers.smolvm.smolvm_command at the binary.",
        )


class SmolvmVersionError(ProviderUnavailableError):
    """Raised when the installed smolvm version is too old."""

    def __init__(
        self,
        provider_name: ProviderInstanceName,
        installed_version: str,
        minimum_version: str,
    ) -> None:
        super().__init__(
            provider_name,
            f"smolvm version {installed_version} is too old (minimum: {minimum_version}). Upgrade smolvm.",
        )


class SmolvmCapabilityError(ProviderUnavailableError):
    """Raised when the installed smolvm build lacks a required capability."""

    def __init__(self, provider_name: ProviderInstanceName, capability: str) -> None:
        super().__init__(
            provider_name,
            f"the installed smolvm build does not support {capability}. "
            "This feature requires a smolvm build with btrfs data-disk support.",
        )


class SmolvmConfigError(MngrError, ValueError):
    """Raised when a SmolvmProviderConfig combines mutually-incompatible options."""


class SmolvmCommandError(MngrError):
    """Raised when a smolvm CLI command fails."""

    def __init__(self, command: str, returncode: int | None, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"smolvm {command} failed (exit code {returncode}): {stderr}")


class SmolvmHostCreationError(HostCreationError):
    """Raised when creating a smolvm machine host fails."""

    def __init__(self, provider_name: ProviderInstanceName, reason: str, build_log: str = "") -> None:
        self.build_log = build_log
        super().__init__(provider_name, f"Failed to create smolvm machine: {reason}")


class SmolvmHostRenameError(MngrError):
    """Raised when attempting to rename a smolvm host."""

    def __init__(self) -> None:
        super().__init__("smolvm machines cannot be renamed. Create a new host with the desired name instead.")


class SmolvmProvisioningError(MngrError):
    """Raised when in-guest provisioning (sshd install/start) fails."""

    def __init__(self, machine_name: str, reason: str) -> None:
        super().__init__(f"Failed to provision smolvm machine {machine_name}: {reason}")
