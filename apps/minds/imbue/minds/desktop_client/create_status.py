"""User-facing status captions for the create flow."""

from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.primitives import LaunchMode

_STATUS_TEXT_DEFAULT: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Cloning repository...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "CREATING_WORKSPACE": "Creating workspace...",
    "WAITING_FOR_READY": "Waiting for workspace to be ready...",
    "DONE": "Done. Redirecting...",
}

# IMBUE_CLOUD's captions follow the onboarding copy; the other launch modes
# keep the plain ones above.
_STATUS_TEXT_IMBUE_CLOUD: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Getting things ready",
    "CHECKING_OUT_BRANCH": "Building your workspace",
    "CREATING_WORKSPACE": "Installing your tools",
    "WAITING_FOR_READY": "Almost there",
    "DONE": "Done. Redirecting...",
}


@pure
def status_text_for(
    status: str,
    error: str | None = None,
    launch_mode: LaunchMode = LaunchMode.DOCKER,
) -> str:
    """Resolve the UI caption for an ``AgentCreateAttemptStatus`` value.

    ``status`` is the stringified enum value (e.g. ``"CLONING_REPO"``).
    ``error`` is consulted only for the ``FAILED`` case so the caption
    can surface the underlying error message; for every other status the
    text comes from the mode-aware ``_STATUS_TEXT_*`` maps.
    """
    if status == "FAILED":
        return "Failed: {}".format(error or "unknown error")
    text_map = _STATUS_TEXT_IMBUE_CLOUD if launch_mode is LaunchMode.IMBUE_CLOUD else _STATUS_TEXT_DEFAULT
    return text_map.get(status, "Working...")
