import pytest

from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.remote_execution import RemoteExecutionGrant
from imbue.mngr_latchkey.remote_execution import parse_remote_execution_permission
from imbue.mngr_latchkey.remote_execution import remote_execution_permission_name


def test_a_grant_is_named_after_its_desktop_and_read_back_from_it() -> None:
    grant = RemoteExecutionGrant(device_id=DesktopDeviceId("host-3f9c.laptop"))

    name = remote_execution_permission_name(grant)

    assert name == "remote-execution-host-3f9c.laptop"
    assert parse_remote_execution_permission(name) == grant


@pytest.mark.parametrize(
    "permission_name",
    [
        "remote-execution-",
        "remote-execution- host-3f9c",
        "remote-execution-.hidden",
        "remote-execution-host-3f9c/extra",
        "remote-execution-host-3f9c:/path",
        "minds-file-server-read-host-3f9c:/Users/kim/notes",
        "minds-workspaces-read",
        "latchkey-self-read-devices",
    ],
)
def test_names_that_are_not_a_remote_execution_grant_are_not_parsed(permission_name: str) -> None:
    assert parse_remote_execution_permission(permission_name) is None
