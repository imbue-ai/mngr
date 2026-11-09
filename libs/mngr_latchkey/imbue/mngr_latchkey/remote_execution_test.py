import pytest

from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.remote_execution import RemoteExecutionGrant
from imbue.mngr_latchkey.remote_execution import is_valid_remote_execution_program
from imbue.mngr_latchkey.remote_execution import parse_remote_execution_permission
from imbue.mngr_latchkey.remote_execution import remote_execution_permission_name


def test_a_grant_is_named_after_its_desktop_and_read_back_from_it() -> None:
    grant = RemoteExecutionGrant(device_id=DesktopDeviceId("host-3f9c.laptop"))

    name = remote_execution_permission_name(grant)

    assert name == "remote-execution-host-3f9c.laptop"
    assert parse_remote_execution_permission(name) == grant


@pytest.mark.parametrize(
    ("program", "expected_name"),
    [
        ("rsync", "remote-execution-host-3f9c:rsync"),
        ("~/.studio/custom-scripts/deploy.sh", "remote-execution-host-3f9c:~/.studio/custom-scripts/deploy.sh"),
        ("/opt/homebrew/bin/gh", "remote-execution-host-3f9c:/opt/homebrew/bin/gh"),
        ("tool:with:colons", "remote-execution-host-3f9c:tool:with:colons"),
    ],
)
def test_a_program_grant_carries_the_program_after_the_device_and_reads_back(program: str, expected_name: str) -> None:
    grant = RemoteExecutionGrant(device_id=DesktopDeviceId("host-3f9c"), program=program)

    name = remote_execution_permission_name(grant)

    assert name == expected_name
    assert parse_remote_execution_permission(name) == grant


@pytest.mark.parametrize(
    "permission_name",
    [
        "remote-execution-",
        "remote-execution- host-3f9c",
        "remote-execution-.hidden",
        "remote-execution-host-3f9c/extra",
        "remote-execution-host-3f9c:",
        "remote-execution-host-3f9c: rsync",
        "remote-execution-host-3f9c:rsync ",
        "remote-execution-host-3f9c:bin/tool",
        "remote-execution-host-3f9c:./tool",
        "remote-execution-host-3f9c:rsync\n",
        "minds-file-server-read-host-3f9c:/Users/kim/notes",
        "minds-workspaces-read",
        "latchkey-self-read-devices",
    ],
)
def test_names_that_are_not_a_remote_execution_grant_are_not_parsed(permission_name: str) -> None:
    assert parse_remote_execution_permission(permission_name) is None


@pytest.mark.parametrize("program", ["rsync", "gh", "a", "~/x", "/usr/bin/env", "name.with-dots_and:colons"])
def test_bare_names_and_rooted_paths_are_valid_programs(program: str) -> None:
    assert is_valid_remote_execution_program(program)


@pytest.mark.parametrize(
    "program", ["", " rsync", "rsync ", "bin/tool", "./tool", "../tool", "~tool", "a\tb", "a\x00b", "-", "x" * 513]
)
def test_relative_paths_whitespace_and_control_characters_are_not_valid_programs(program: str) -> None:
    assert not is_valid_remote_execution_program(program)
