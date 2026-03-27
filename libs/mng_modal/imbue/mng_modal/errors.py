from imbue.mng.errors import MngError


class ModalMngError(MngError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngError(ModalMngError):
    """Raised when a Modal host has no snapshots available."""


class ModalSandboxTimeoutMngError(ModalMngError):
    """Raised when a Modal sandbox fails to come online in time."""
