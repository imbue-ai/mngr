from imbue.mngr.errors import MngrError


class ModalMngrError(MngrError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngrError(ModalMngrError):
    """Raised when a Modal host has no snapshots available."""


class NoResumableSnapshotModalMngrError(ModalMngrError):
    """Raised when a Modal host has only its bare "initial" snapshot and no resumable state.

    Auto-restarting such a host would create a sandbox with no agent (an
    agent-less, billing orphan), so callers should recreate the workspace instead.
    """


class ModalSandboxTimeoutMngrError(ModalMngrError):
    """Raised when a Modal sandbox fails to come online in time."""
