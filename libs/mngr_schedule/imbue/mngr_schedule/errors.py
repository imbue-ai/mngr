from imbue.mngr.errors import MngrError


class ScheduleDeployError(MngrError):
    """Raised when schedule deployment fails."""


class UploadSpecError(ScheduleDeployError, ValueError):
    """Raised when an upload spec is malformed or its source does not exist."""
