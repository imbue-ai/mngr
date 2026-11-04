"""How a remote-execution grant is named in a host's policy.

A remote-execution grant lets every agent of a host run commands on one of
the user's desktops, through that desktop's ``POST
/api/v1/remote-execution/<device id>`` route. The gateway's
``permission_requests.mjs`` extension mints the grant as a permission schema
named ``remote-execution-<device id>`` (for example
``remote-execution-host-3f9c``), pinned to the exact method and path of that
one desktop's route, so a grant for one desktop admits nothing on another.

The extension owns how a grant's schema is built; this module only reads and
writes the name, which is what the desktop's permission screens need. The name
deliberately carries no product prefix: this package is mngr-level code.
"""

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.devices import DesktopDeviceId

REMOTE_EXECUTION_PERMISSION_PREFIX: Final[str] = "remote-execution-"


class RemoteExecutionGrant(FrozenModel):
    """One remote-execution permission: the right to run commands on one desktop."""

    device_id: DesktopDeviceId = Field(description="The desktop the grant lets agents run commands on.")


@pure
def remote_execution_permission_name(grant: RemoteExecutionGrant) -> str:
    return f"{REMOTE_EXECUTION_PERMISSION_PREFIX}{grant.device_id}"


@pure
def parse_remote_execution_permission(permission_name: str) -> RemoteExecutionGrant | None:
    """The grant a ``remote-execution-<device id>`` permission name describes, or ``None`` for any other name."""
    if not permission_name.startswith(REMOTE_EXECUTION_PERMISSION_PREFIX):
        return None
    raw_device_id = permission_name[len(REMOTE_EXECUTION_PERMISSION_PREFIX) :]
    try:
        device_id = DesktopDeviceId(raw_device_id)
    except InvalidPrimitiveValueError:
        return None
    grant = RemoteExecutionGrant(device_id=device_id)
    # The device id primitive strips surrounding whitespace, which a name that is really a grant never carries.
    return grant if remote_execution_permission_name(grant) == permission_name else None
