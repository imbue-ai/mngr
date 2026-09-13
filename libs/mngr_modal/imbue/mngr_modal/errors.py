from imbue.mngr.errors import MngrError


class ModalMngrError(MngrError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngrError(ModalMngrError):
    """Raised when a Modal host has no snapshots available."""


class ModalSandboxTimeoutMngrError(ModalMngrError):
    """Raised when a Modal sandbox fails to come online in time."""


class ModalCliOutputError(ModalMngrError, ValueError):
    """Raised when a `modal ... list --json` payload does not carry the keys we read.

    Loud on purpose. These listings are read to find Modal resources to reap,
    so a key we cannot find yields an empty result that looks exactly like
    "nothing to clean up" and leaks apps, volumes and environments silently.
    """

    user_help_text = "The Modal CLI's JSON output shape may have changed; check `modal --version`."

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        super().__init__(f"Unexpected output from `{command} --json`: {reason}")
