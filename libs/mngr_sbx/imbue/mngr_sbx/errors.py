from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName


class SbxNotInstalledError(ProviderUnavailableError):
    """Raised when the sbx CLI is not found on PATH."""

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(
            provider_name,
            "sbx is not installed. Install Docker Sandboxes: https://docs.docker.com/ai/sandboxes/",
        )


class SbxNotAuthorizedError(ProviderNotAuthorizedError):
    """Raised when sbx has no usable Docker credentials."""

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(
            provider_name,
            auth_help=(
                "Run 'sbx login' to authenticate with Docker. "
                "If you are running headless, pre-authenticate on a host you control "
                "and mount the sbx state directory into this environment."
            ),
        )


class SbxCommandError(MngrError):
    """Raised when an sbx CLI invocation fails."""

    def __init__(self, command: str, returncode: int | None, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"sbx {command} failed (exit code {returncode}): {stderr}")


class SbxHostCreationError(HostCreationError):
    """Raised when creating a Docker sandbox host fails."""

    def __init__(self, reason: str, build_log: str = "") -> None:
        self.build_log = build_log
        super().__init__(f"Failed to create Docker sandbox: {reason}")


class SbxHostRenameError(MngrError):
    """Raised when attempting to rename a Docker sandbox host."""

    def __init__(self) -> None:
        super().__init__("Docker sandboxes cannot be renamed. Create a new host with the desired name instead.")
