import pytest

from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.file_sharing import FileSharingAccess
from imbue.mngr_latchkey.file_sharing import FileSharingGrant
from imbue.mngr_latchkey.file_sharing import LegacyFileSharingGrant
from imbue.mngr_latchkey.file_sharing import file_sharing_permission_name
from imbue.mngr_latchkey.file_sharing import legacy_file_sharing_permission_name
from imbue.mngr_latchkey.file_sharing import parse_file_sharing_permission
from imbue.mngr_latchkey.file_sharing import parse_legacy_file_sharing_permission


def test_a_grant_is_named_after_the_file_servers_url_and_read_back_from_it() -> None:
    grant = FileSharingGrant(
        access=FileSharingAccess.WRITE, device_id=DesktopDeviceId("host-3f9c"), path="/Users/kim/My notes-2024"
    )

    name = file_sharing_permission_name(grant)

    assert name == "minds-file-server-write-host-3f9c:/Users/kim/My notes-2024"
    assert parse_file_sharing_permission(name) == grant


@pytest.mark.parametrize(
    "permission_name",
    [
        # From before grants named a desktop.
        "minds-file-server-read-/Users/kim/notes",
        "minds-file-server-read-/Users/kim/notes:/draft",
        "minds-file-server-read-host-3f9c/Users/kim/notes",
        "minds-file-server-read-host-3f9c:Users/kim/notes",
        "minds-file-server-execute-host-3f9c:/Users/kim/notes",
        "minds-file-server-read-host-3f9c",
        "minds-file-server-read- host-3f9c:/Users/kim/notes",
        "minds-file-server-read-.hidden:/Users/kim/notes",
        "minds-workspaces-read",
        "minds-api-proxy-call-agent-123",
    ],
)
def test_names_that_are_not_a_desktops_grant_are_not_parsed(permission_name: str) -> None:
    assert parse_file_sharing_permission(permission_name) is None


# CLEANUP: once a permissions migration has deleted the device-less grants, move the two tests below
# into the migrations with the parser they cover, without the naming half of the first.
def test_a_grant_from_before_devices_is_named_after_its_path_alone_and_read_back_from_it() -> None:
    grant = LegacyFileSharingGrant(access=FileSharingAccess.WRITE, path="/Users/kim/My notes-2024")

    name = legacy_file_sharing_permission_name(grant)

    assert name == "minds-file-server-write-/Users/kim/My notes-2024"
    assert parse_legacy_file_sharing_permission(name) == grant


@pytest.mark.parametrize(
    "permission_name",
    [
        "minds-file-server-read-host-3f9c:/Users/kim/notes",
        "minds-file-server-execute-/Users/kim/notes",
        "minds-file-server-read",
        "minds-workspaces-read",
    ],
)
def test_names_that_are_not_a_grant_from_before_devices_are_not_parsed_as_one(permission_name: str) -> None:
    assert parse_legacy_file_sharing_permission(permission_name) is None
