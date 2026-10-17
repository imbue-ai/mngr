from imbue.mngr.errors import MngrError


class ModalMngrError(MngrError):
    """Base error for Modal provider operations."""


class NoSnapshotsModalMngrError(ModalMngrError):
    """Raised when a Modal host has no snapshots available."""


class ModalSandboxTimeoutMngrError(ModalMngrError):
    """Raised when a Modal sandbox fails to come online in time."""


class ModalSandboxDiedMngrError(ModalMngrError):
    """Raised when the Modal sandbox a command was running in is no longer alive."""


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


class ModalSnapshotEndpointTimeoutMngrError(ModalMngrError, TimeoutError):
    """Raised when a host's bring-up spends its whole budget waiting for the snapshot endpoint."""

    def __init__(self, app_name: str, timeout_seconds: float) -> None:
        self.app_name = app_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Modal app {app_name!r} did not publish its snapshot_and_shutdown endpoint within "
            f"{timeout_seconds}s. The endpoint is deployed once per app, so concurrent creates "
            f"against the same app queue behind Modal's per-app deploy lock."
        )
