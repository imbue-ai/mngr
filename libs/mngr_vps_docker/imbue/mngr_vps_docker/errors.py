from imbue.mngr.errors import MngrError


class VpsDockerError(MngrError):
    """Base error for VPS Docker provider operations."""


class VpsProvisioningError(VpsDockerError):
    """Failed to provision a VPS instance."""


class VpsConnectionError(VpsDockerError, ConnectionError):
    """Failed to connect to the VPS via SSH."""


class DockerNotReadyError(VpsDockerError):
    """Docker is not installed or not running on the VPS."""


class ContainerSetupError(VpsDockerError):
    """Failed to set up the Docker container on the VPS."""


class VpsApiError(VpsDockerError):
    """Error from the VPS provider API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"VPS API error {status_code}: {message}")
