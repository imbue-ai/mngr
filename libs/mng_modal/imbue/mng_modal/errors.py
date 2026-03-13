from imbue.mng.errors import MngError


class ModalMngError(MngError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngError(ModalMngError):
    """Raised when a Modal host has no snapshots available."""
