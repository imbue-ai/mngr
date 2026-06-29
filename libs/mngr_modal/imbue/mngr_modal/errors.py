from imbue.mngr.errors import MngrError


class ModalMngrError(MngrError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngrError(ModalMngrError):
    """Raised when a Modal host has no snapshots available."""


class ModalSandboxTimeoutMngrError(ModalMngrError):
    """Raised when a Modal sandbox fails to come online in time."""
