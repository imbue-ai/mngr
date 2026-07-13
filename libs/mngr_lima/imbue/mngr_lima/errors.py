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


class LimaConfigError(MngrError, ValueError):
    """Raised when a LimaProviderConfig combines mutually-incompatible options."""


class LimaInstanceNameTooLongError(MngrError):
    """Raised when no Lima instance name fits UNIX_PATH_MAX for the current LIMA_HOME.

    Lima derives an SSH control-socket path from the instance name and rejects
    the VM if that path reaches UNIX_PATH_MAX. When the mngr prefix plus
    LIMA_HOME already consume the whole budget, even a minimally-shortened name
    cannot fit, so we fail early with an actionable message instead of letting
    limactl abort with a cryptic fatal error.
    """

    def __init__(self, prefix: str, lima_home: str) -> None:
        super().__init__(
            f"Cannot build a Lima instance name short enough for UNIX_PATH_MAX with mngr prefix {prefix!r} "
            f"and LIMA_HOME {lima_home!r}. Shorten the mngr prefix or point LIMA_HOME at a shorter path."
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

    def __init__(self, provider_name: ProviderInstanceName, reason: str, build_log: str = "") -> None:
        self.build_log = build_log
        super().__init__(provider_name, f"Failed to create Lima VM: {reason}")
