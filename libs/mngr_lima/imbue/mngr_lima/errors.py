from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName


class LimaNotInstalledError(ProviderUnavailableError):
    """Raised when limactl is not found on PATH."""

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(
            provider_name,
            "limactl is not installed. Install Lima: https://lima-vm.io/docs/installation/",
        )


class LimaVersionError(ProviderUnavailableError):
    """Raised when the installed Lima version is too old."""

    def __init__(
        self,
        provider_name: ProviderInstanceName,
        installed_version: str,
        minimum_version: str,
    ) -> None:
        super().__init__(
            provider_name,
            f"Lima version {installed_version} is too old (minimum: {minimum_version}). "
            "Upgrade Lima: https://lima-vm.io/docs/installation/",
        )


class LimaCommandError(MngrError):
    """Raised when a limactl command fails."""

    def __init__(self, command: str, returncode: int | None, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"limactl {command} failed (exit code {returncode}): {stderr}")


class LimaHostCreationError(HostCreationError):
    """Raised when creating a Lima VM host fails."""

    def __init__(self, reason: str, build_log: str = "") -> None:
        self.build_log = build_log
        super().__init__(f"Failed to create Lima VM: {reason}")


class LimaHostRenameError(MngrError):
    """Raised when attempting to rename a Lima host."""

    def __init__(self) -> None:
        super().__init__("Lima instances cannot be renamed. Create a new host with the desired name instead.")
