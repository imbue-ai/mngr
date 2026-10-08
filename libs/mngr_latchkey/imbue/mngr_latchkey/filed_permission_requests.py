"""The permission requests a remote host's machine keeps for the user's desktops.

An agent on a remote host files a permission request with its machine's gateway,
which forwards it to the user's desktops (see the ``X-Latchkey-Device`` header in
:mod:`imbue.mngr_latchkey.devices`). The request outlives that forwarding: the
user answers it on whichever desktop they are at, and a desktop that was offline
shows it once it connects again. So the machine's forwarding extension
(``desktop_gateway_proxy.mjs``) gives the request its id before forwarding, so
that every desktop files it under the same one, and keeps a record of what the
agent sent under ``<latchkey directory>/filed_permission_requests/v1/`` for the
desktops to sync against: :class:`FiledPermissionRequest` is that record, with
the same field names the extension writes.

The machine validates nothing about the request: the record holds the agent's
body as it was sent, and a desktop that syncs it files it through its own
gateway, which judges it exactly as it judges a request forwarded live. A body
the desktop refuses is dropped from the machine by the desktop that found it so
(see :func:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials.forget_permission_request`),
since no desktop will ever take it.
"""

import json
from datetime import datetime
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.devices import DEVICE_HEADER_ALL_DEVICES
from imbue.mngr_latchkey.devices import DesktopDeviceId

# Where a machine keeps its records, under its latchkey directory, and how each
# is named after the request's id. The version segment changes with any
# incompatible change to the record's shape (a field removed, renamed or
# retyped), so an older record is never read as a newer one; a field added is
# not one, since a reader ignores what it does not know. Kept in step with
# ``desktop_gateway_proxy.mjs``.
FILED_REQUESTS_DIR_NAME: Final[str] = "filed_permission_requests"
FILED_REQUESTS_SCHEMA_VERSION: Final[str] = "v1"
FILED_REQUEST_FILE_SUFFIX: Final[str] = ".json"


class FiledPermissionRequestsError(LatchkeyError, ValueError):
    """Raised when a machine's records of filed requests are not what its extension writes."""


class FiledPermissionRequest(FrozenModel):
    """One request a machine keeps: what the agent sent, who it is for, and when it was filed."""

    # The machine's package is provisioned by whichever of the user's desktops
    # upgraded first, so an older desktop routinely reads records a newer package
    # wrote: a field this build does not know is skipped, not a reason to fail
    # the whole sync.
    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(description="The id the machine gave the request, which every desktop files it under.")
    devices: str | tuple[str, ...] = Field(
        description=(
            "The desktops the request is for: ``*`` for every desktop of the user's, or the device ids the agent "
            "named (known to the machine or not), as the machine recorded them."
        )
    )
    created_at: datetime = Field(description="When the machine filed the request.")
    body: dict[str, JsonValue] = Field(
        description="The agent's request body as it was sent, without the id; what a desktop files on its own gateway."
    )

    def is_for_desktop(self, device_id: DesktopDeviceId) -> bool:
        if isinstance(self.devices, str):
            return self.devices == DEVICE_HEADER_ALL_DEVICES
        return str(device_id) in self.devices


def parse_filed_permission_requests(records_json: str) -> tuple[FiledPermissionRequest, ...]:
    """Read the records a machine's ``list-requests`` answers with: a JSON array of them.

    Raises:
        FiledPermissionRequestsError: when the answer is not an array of records.
    """
    try:
        parsed = json.loads(records_json)
    except json.JSONDecodeError as e:
        raise FiledPermissionRequestsError(f"The machine's filed permission requests are not JSON: {e}") from e
    if not isinstance(parsed, list):
        raise FiledPermissionRequestsError(
            f"The machine's filed permission requests are not the JSON array they should be: {records_json[:200]!r}"
        )
    try:
        return tuple(FiledPermissionRequest.model_validate(entry) for entry in parsed)
    except ValueError as e:
        raise FiledPermissionRequestsError(
            f"A filed permission request is not in the shape the machine writes: {e}"
        ) from e
