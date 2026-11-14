"""What a latchkey failure tells the user: our sentence on the visible line, the technical text behind it.

The exceptions this area raises describe what went wrong for a log -- a gateway
URL and errno, a host id, a command's output. A route that reports one picks
the sentence for the action the user attempted here, and keeps the exception's
text for See details.
"""

from typing import Final

from flask import Response

from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientNotInitializedError
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayInitializationError
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.ui_models import ErrorRemedy
from imbue.minds.desktop_client.ui_models import UiFailureNotice
from imbue.minds.desktop_client.user_facing_failure import UserFacingFailure

# The local permission service never came up (or is not up yet): nothing a
# retry in a moment fixes, so the remedy is the restart that respawns it.
PERMISSION_SERVICE_NOT_RUNNING_FOR_CHANGE: Final[UserFacingFailure] = UserFacingFailure(
    message=(
        "Imbue Studio's permission service isn't running, so this couldn't be saved. *Restart Imbue Studio "
        "and try again.* If this persists, report a bug."
    ),
    remedy=ErrorRemedy.RESTART_APP,
)
PERMISSION_SERVICE_NOT_RUNNING_FOR_READ: Final[UserFacingFailure] = UserFacingFailure(
    message=(
        "Imbue Studio's permission service isn't running, so this workspace's permissions can't be shown. "
        "*Restart Imbue Studio and try again.* If this persists, report a bug."
    ),
    remedy=ErrorRemedy.RESTART_APP,
)


# An Approve on a workspace whose machine discovery has not placed yet.
HOST_NOT_RESOLVED_FOR_APPROVAL_SENTENCE: Final[str] = (
    "Imbue Studio is still finding the machine this workspace runs on. *Wait a moment, then approve again.*"
)

# A desktop with no device id cannot name the computer a shared file is on.
NO_DEVICE_ID_SENTENCE: Final[str] = "Imbue Studio can't share files from this computer."

MACHINE_STORE_UNREADABLE_SENTENCE: Final[str] = (
    "Imbue Studio can't read this workspace's connections right now. Try again in a moment."
)


def is_permission_service_not_running(error: LatchkeyGatewayClientError) -> bool:
    """Whether ``error`` says the gateway never came up, rather than that one call to it failed."""
    return isinstance(error, (LatchkeyGatewayInitializationError, LatchkeyGatewayClientNotInitializedError))


def describe_gateway_failure(
    error: LatchkeyGatewayClientError,
    sentence_while_running: str,
    not_running_failure: UserFacingFailure,
) -> UserFacingFailure:
    """The failure to show for ``error``: the not-running one when the gateway never came up, else ``sentence``."""
    if is_permission_service_not_running(error):
        return not_running_failure
    return UserFacingFailure(message=sentence_while_running, remedy=None)


def make_failure_response(
    failure: UserFacingFailure, status_code: int, detail: str, is_request_resolved: bool = False
) -> Response:
    """The JSON error envelope for ``failure``, with ``detail`` behind See details."""
    return make_json_error_response(
        failure.message,
        status_code,
        detail=detail,
        remedy=failure.remedy,
        is_request_resolved=is_request_resolved,
    )


def build_failure_notice(failure: UserFacingFailure, detail: str) -> UiFailureNotice:
    """``failure`` as a payload field, for a response that reports it inside a successful answer."""
    return UiFailureNotice(message=failure.message, detail=detail, remedy=failure.remedy)
