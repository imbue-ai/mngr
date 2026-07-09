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


class LimaCommandUnavailableError(ProviderUnavailableError):
    """Raised when limactl is installed and correctly versioned but fails to run.

    Distinct from LimaNotInstalledError (binary absent) and LimaVersionError
    (binary too old): here the binary is present and new enough, but a limactl
    invocation failed at runtime (e.g. it crashed at startup). No Lima host can
    be reached, so this is surfaced as provider unavailability rather than an
    opaque command error.
    """

    def __init__(self, provider_name: ProviderInstanceName, reason: str) -> None:
        super().__init__(
            provider_name,
            reason,
            user_help_text=(
                "limactl is installed but a limactl command failed to run (it may have crashed). "
                "This is often transient -- try again. If it persists, reinstall Lima: "
                "https://lima-vm.io/docs/installation/"
            ),
            short_remediation="retry; reinstall Lima if it persists",
        )


class LimaConfigError(MngrError, ValueError):
    """Raised when a LimaProviderConfig combines mutually-incompatible options."""


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
