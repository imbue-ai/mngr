"""How a file-sharing grant is named in a host's policy.

A file-sharing grant lets an agent reach one path on one of the user's
desktops through that desktop's WebDAV file server, which it serves under
``/api/v1/files/<device id>/<absolute path>``. The gateway's
``permission_requests.mjs`` extension mints the grant as a permission schema
named ``minds-file-server-<access>-<device id>:<absolute path>`` (for example
``minds-file-server-read-host-3f9c:/home/kim/notes``). A device id never
contains a ``:``, so the first ``:`` is where the device id ends and the path
begins.

The extension owns how a grant's schema is built (the URL pattern and the verb
set); this module only reads and writes the name, which is what the desktop
app's permission screens and the policy migrations need.

A grant made before desktops were told apart is named
``minds-file-server-<access>-<absolute path>`` and matches the device-less URL
``/api/v1/files/<absolute path>``. Nothing mints one any more, but a policy
that holds one keeps it (see :class:`LegacyFileSharingGrant`), so an agent that
reaches shared files through the device-less URL goes on reaching what it was
given.
"""

from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.devices import DesktopDeviceId

FILE_SHARING_PERMISSION_PREFIX: Final[str] = "minds-file-server-"


class FileSharingAccess(UpperCaseStrEnum):
    """Access mode of a file-sharing grant.

    ``READ`` unlocks the non-mutating WebDAV verbs only (GET, HEAD, OPTIONS,
    PROPFIND); ``WRITE`` is a strict superset that also unlocks the verbs that
    mutate the resource (PUT, DELETE, PROPPATCH, MKCOL, LOCK, UNLOCK). Read-only
    and read-write grants for the same path are distinct schemas, so the two can
    be held independently.
    """

    READ = auto()
    WRITE = auto()


class FileSharingGrant(FrozenModel):
    """One file-sharing permission: an access mode on a path of one desktop."""

    access: FileSharingAccess = Field(description="What the grant lets the agent do with the path.")
    device_id: DesktopDeviceId = Field(description="The desktop whose file the path names.")
    path: str = Field(description="The absolute path on that desktop.")


@pure
def file_sharing_permission_name(grant: FileSharingGrant) -> str:
    return f"{FILE_SHARING_PERMISSION_PREFIX}{grant.access.lower()}-{grant.device_id}:{grant.path}"


@pure
def parse_file_sharing_permission(permission_name: str) -> FileSharingGrant | None:
    """The grant a ``minds-file-server-*`` permission name describes, or ``None`` for any other name.

    Names from before grants were scoped to a desktop (``...-<access>-/<path>``)
    carry no device id and are not grants of this shape; see
    :func:`parse_legacy_file_sharing_permission`.
    """
    if not permission_name.startswith(FILE_SHARING_PERMISSION_PREFIX):
        return None
    remainder = permission_name[len(FILE_SHARING_PERMISSION_PREFIX) :]
    raw_access, access_separator, device_and_path = remainder.partition("-")
    raw_device_id, _, path = device_and_path.partition(":")
    if not access_separator or not path.startswith("/") or raw_access not in ("read", "write"):
        return None
    try:
        device_id = DesktopDeviceId(raw_device_id)
    except InvalidPrimitiveValueError:
        return None
    grant = FileSharingGrant(access=FileSharingAccess(raw_access.upper()), device_id=device_id, path=path)
    # The device id primitive strips surrounding whitespace, which a name that is really a grant never carries.
    return grant if file_sharing_permission_name(grant) == permission_name else None


# CLEANUP: once no supported agent reaches shared files through the device-less
# URL and a permissions migration has deleted the grants that permit it, drop
# legacy_file_sharing_permission_name and move LegacyFileSharingGrant and its
# parser into the migrations, their only remaining readers.
class LegacyFileSharingGrant(FrozenModel):
    """A file-sharing permission from before grants named a desktop: an access mode on a path of whichever desktop answers."""

    access: FileSharingAccess = Field(description="What the grant lets the agent do with the path.")
    path: str = Field(description="The absolute path on the desktop that answers the request.")


@pure
def legacy_file_sharing_permission_name(grant: LegacyFileSharingGrant) -> str:
    return f"{FILE_SHARING_PERMISSION_PREFIX}{grant.access.lower()}-{grant.path}"


@pure
def parse_legacy_file_sharing_permission(permission_name: str) -> LegacyFileSharingGrant | None:
    """The grant a ``minds-file-server-<access>-<absolute path>`` name describes, or ``None`` for any other name."""
    if not permission_name.startswith(FILE_SHARING_PERMISSION_PREFIX):
        return None
    raw_access, separator, path = permission_name[len(FILE_SHARING_PERMISSION_PREFIX) :].partition("-")
    if not separator or raw_access not in ("read", "write") or not path.startswith("/"):
        return None
    return LegacyFileSharingGrant(access=FileSharingAccess(raw_access.upper()), path=path)
