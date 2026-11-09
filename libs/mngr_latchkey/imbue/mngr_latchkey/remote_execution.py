"""How a remote-execution grant is named in a host's policy.

A remote-execution grant lets every agent of a host run commands on one of
the user's desktops, through that desktop's ``POST
/api/v1/remote-execution/<device id>?program=<program>`` route. The gateway's
``permission_requests.mjs`` extension mints the grant as a permission schema
pinned to the exact method and path of that one desktop's route, so a grant
for one desktop admits nothing on another. A grant comes in two shapes:

* ``remote-execution-<device id>`` admits any program on that desktop;
* ``remote-execution-<device id>:<program>`` admits only calls whose
  ``program`` query parameter is exactly ``<program>``: a bare name the
  desktop resolves on the user's PATH (``rsync``) or a ``~``-rooted or
  absolute path (``~/.studio/custom-scripts/deploy.sh``). The program rides
  the URL, not the body, because the gateway checks an extension route's
  permissions without reading its body; pinning the query parameter is what
  makes the restriction hold before the request reaches the desktop.

The extension owns how a grant's schema is built; this module only reads and
writes the name, which is what the desktop's permission screens need. The name
deliberately carries no product prefix: this package is mngr-level code. A
device id never contains ``:`` (``DesktopDeviceId`` refuses one that would), so
the first ``:`` after the prefix always separates the device from the program.
"""

import re
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.devices import DesktopDeviceId

REMOTE_EXECUTION_PERMISSION_PREFIX: Final[str] = "remote-execution-"
# The query parameter of the desktop's route that names the program to run; a
# program grant's schema pins it, so the desktop must read the same parameter.
REMOTE_EXECUTION_PROGRAM_QUERY_PARAMETER: Final[str] = "program"
_PROGRAM_SEPARATOR: Final[str] = ":"

# What a grant may name as its program. A bare name has no slash and does not
# start with ``~`` or ``-`` (a shell would read the one as a home directory and
# the other as a flag); a path starts with ``/`` or ``~/``. Nothing may carry
# whitespace at either end, a control character, or be empty, and the length cap
# keeps a permission name readable. The gateway extension enforces the same
# grammar on the request payload.
REMOTE_EXECUTION_PROGRAM_MAX_LENGTH: Final[int] = 512
_PROGRAM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:/|~/)?[^\x00-\x20\x7f/~-][^\x00-\x1f\x7f]*$")


class RemoteExecutionGrant(FrozenModel):
    """One remote-execution permission: the right to run commands on one desktop, or one program there."""

    device_id: DesktopDeviceId = Field(description="The desktop the grant lets agents run commands on.")
    program: str | None = Field(
        default=None,
        description=(
            "The one program the grant admits as the route's ``program`` query parameter (a bare name or a "
            "``~``-rooted or absolute path), or None when the grant admits any program."
        ),
    )


@pure
def is_valid_remote_execution_program(program: str) -> bool:
    """Whether ``program`` is something a grant may name: a bare name or a ``~``-rooted or absolute path."""
    if not program or len(program) > REMOTE_EXECUTION_PROGRAM_MAX_LENGTH:
        return False
    if program != program.strip():
        return False
    if _PROGRAM_PATTERN.match(program) is None:
        return False
    # Anything with a slash is a path, and a path is only admitted rooted.
    return "/" not in program or program.startswith("/") or program.startswith("~/")


@pure
def remote_execution_permission_name(grant: RemoteExecutionGrant) -> str:
    base = f"{REMOTE_EXECUTION_PERMISSION_PREFIX}{grant.device_id}"
    if grant.program is None:
        return base
    return f"{base}{_PROGRAM_SEPARATOR}{grant.program}"


@pure
def parse_remote_execution_permission(permission_name: str) -> RemoteExecutionGrant | None:
    """The grant a ``remote-execution-<device id>[:<program>]`` permission name describes, or ``None`` otherwise."""
    if not permission_name.startswith(REMOTE_EXECUTION_PERMISSION_PREFIX):
        return None
    rest = permission_name[len(REMOTE_EXECUTION_PERMISSION_PREFIX) :]
    raw_device_id, separator, raw_program = rest.partition(_PROGRAM_SEPARATOR)
    try:
        device_id = DesktopDeviceId(raw_device_id)
    except InvalidPrimitiveValueError:
        return None
    program: str | None
    if separator:
        if not is_valid_remote_execution_program(raw_program):
            return None
        program = raw_program
    else:
        program = None
    grant = RemoteExecutionGrant(device_id=device_id, program=program)
    # The device id primitive strips surrounding whitespace, which a name that is really a grant never carries.
    return grant if remote_execution_permission_name(grant) == permission_name else None
