"""User-facing status captions and progress-bar timing for the create flow."""

from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.primitives import LaunchMode

_STATUS_TEXT_DEFAULT: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Cloning repository...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "CREATING_WORKSPACE": "Creating machine...",
    "WAITING_FOR_READY": "Waiting for machine to be ready...",
    "DONE": "Done. Redirecting...",
}

# IMBUE_CLOUD diverges in wording for the connection / agent-setup phases
# where the user-facing mental model is "connecting to / setting up an
# existing pool host" rather than "cloning / creating a new workspace".
_STATUS_TEXT_IMBUE_CLOUD: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Connecting to host...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "CREATING_WORKSPACE": "Setting up agent...",
    "WAITING_FOR_READY": "Waiting for machine to be ready...",
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


# Expected wall-clock duration of ``mngr create`` per compute provider,
# used only to drive the client-side progress-bar animation on the
# creating page (the bar eases toward ~80% over this duration). These are
# rough estimates, not guarantees.
# LIMA now boots a VM *and* builds the project image inside it (the agent runs
# in a Docker container in the VM), so a cold create is closer to a VPS build
# than the old run-directly-in-the-VM path -- bump its progress-bar estimate
# accordingly.
EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS_BY_LAUNCH_MODE: Final[dict[LaunchMode, float]] = {
    LaunchMode.DOCKER: 30.0,
    LaunchMode.LIMA: 600.0,
    LaunchMode.VULTR: 300.0,
    LaunchMode.AWS: 300.0,
    LaunchMode.GCP: 300.0,
    LaunchMode.AZURE: 300.0,
    LaunchMode.IMBUE_CLOUD: 30.0,
}

# Fallback when the launch mode is somehow not in the map above.
DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS: Final[float] = 60.0


@pure
def expected_create_attempt_duration_seconds(launch_mode: LaunchMode) -> float:
    """Resolve the per-provider expected create attempt duration for the progress bar."""
    return EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS_BY_LAUNCH_MODE.get(
        launch_mode, DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS
    )
