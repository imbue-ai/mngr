"""What a desktop is to the machines it connects to, and how a machine records it.

Every desktop (each of the user's computers running the forward supervisor)
keeps a reverse SSH tunnel of its own to each remote host's machine and
announces itself there: one file per desktop under the machine's RAM-backed
``devices/`` directory, named by the desktop's device id and holding a
:class:`DeviceRecord`. The desktop rewrites the file on every discovery cycle
(:data:`DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS`), so its modification time says
when the desktop was last heard from. The machine's gateway extension reports
that on ``/devices`` beside the interval and passes no judgement on it: a
request without a target goes to the most recently announced desktop whatever
its age, and a caller that wants to reason about a desktop's liveness has the
two numbers to do so. What retires a desktop that is gone is its tunnel: the
machine's sshd probes the session (a ``ClientAlive`` drop-in the package
ships) and drops the forwarded port with it, so a request for that desktop is
refused rather than left hanging. The record carries the loopback port the
desktop's tunnel binds on the machine and the two secrets the extension
presents when it proxies a request back to that desktop's gateway, which is why
it lives in RAM beside the machine's own secrets rather than on disk.

The desktop gateway serves the desktop-owned routes itself and ignores
:data:`DESKTOP_HEADER`; its ``device_list.mjs`` extension answers ``/devices``
with the desktop itself (handed to it as :data:`LOCAL_DEVICE_ID_ENV_VAR` and
:data:`LOCAL_DEVICE_HOSTNAME_ENV_VAR`) as the one device. A machine that
resolves the header to a single desktop relays that desktop's response
unwrapped, so an agent asks about desktops, and addresses them, the same way
whether its gateway is on a machine or on the desktop.
"""

import re
from typing import Any
from typing import Final
from typing import Self

from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

# Where a machine keeps the records of the desktops connected to it, under its
# RAM-backed secrets directory, and how each record's file is named after the
# desktop's device id.
DEVICES_DIR_NAME: Final[str] = "devices"
DEVICE_RECORD_SUFFIX: Final[str] = ".json"

# How often a connected desktop refreshes its record: once per discovery cycle
# of the forward supervisor's ``mngr observe``, whose default this is. Reported
# on ``/devices`` so a caller can read a record's age against it.
DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS: Final[int] = 30

# The header an agent names the desktop(s) a desktop-owned request is for:
# one device id, a comma-separated list of them (unknown ones ignored), or
# ``*`` for every desktop the gateway knows. Absent, the request goes to the
# most recently announced desktop, which is what every agent built before
# the header did. Only a machine's gateway reads it.
DESKTOP_HEADER: Final[str] = "X-Latchkey-Desktop"
DESKTOP_HEADER_ALL_DEVICES: Final[str] = "*"
# The response header marking an answer as the array of several desktops'
# responses rather than one desktop's own response.
MULTIPLE_DESKTOPS_MATCHED_HEADER: Final[str] = "X-Latchkey-Multiple-Desktops-Matched"

# The route listing the desktops a gateway knows, connected or not.
DEVICES_ROUTE: Final[str] = "/devices"

# How a machine's extension is told where the records are, and how the desktop
# gateway's ``device_list.mjs`` is told the one device it is.
DEVICES_DIR_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_DEVICES_DIR"
DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS"
LOCAL_DEVICE_ID_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_LOCAL_DEVICE_ID"
LOCAL_DEVICE_HOSTNAME_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_LOCAL_DEVICE_HOSTNAME"

# A device id names a file on the machine, so it is held to characters that
# are safe there (and cannot start with a dot).
_DEVICE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


class DesktopDeviceId(NonEmptyStr):
    """The stable id of one desktop: the desktop app's device id, or the mngr host id of a standalone forward."""

    def __new__(cls, value: str) -> Self:
        stripped = super().__new__(cls, value)
        if _DEVICE_ID_PATTERN.match(stripped) is None:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} may only contain letters, digits, '-', '_' and '.', and may not start with a dot: "
                f"{value!r}"
            )
        return stripped

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )


class DesktopDeviceIdentity(FrozenModel):
    """Who this desktop is to the machines it connects to."""

    device_id: DesktopDeviceId = Field(description="The desktop's stable id, which names its record on a machine.")
    hostname: str = Field(description="The desktop's hostname, for a human reading a list of desktops.")


class DeviceRecord(FrozenModel):
    """What a machine holds about one connected desktop: the record its gateway extension reads.

    Serialized as JSON with these exact field names; the extension
    (``desktop_gateway_proxy.mjs``) reads the same keys.
    """

    device_id: DesktopDeviceId = Field(description="The desktop's stable id.")
    hostname: str = Field(description="The desktop's hostname.")
    port: int = Field(description="The machine loopback port the desktop's tunnel exposes its gateway on.")
    gateway_password: str = Field(description="The desktop gateway's own listen password.")
    permissions_override: str = Field(
        description="A JWT targeting the host's permissions file on the desktop that minted it."
    )


@pure
def build_local_device_env(identity: DesktopDeviceIdentity) -> dict[str, str]:
    """Build the environment that makes a desktop gateway's ``device_list.mjs`` know the one device it is."""
    return {
        LOCAL_DEVICE_ID_ENV_VAR: str(identity.device_id),
        LOCAL_DEVICE_HOSTNAME_ENV_VAR: identity.hostname,
        DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR: str(DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS),
    }


@pure
def device_record_filename(device_id: DesktopDeviceId) -> str:
    return f"{device_id}{DEVICE_RECORD_SUFFIX}"
