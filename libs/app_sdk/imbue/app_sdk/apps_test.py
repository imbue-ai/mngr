import json

import pytest

from imbue.app_sdk.apps import resolve_app_url
from imbue.app_sdk.conftest import ShellStandIn
from imbue.app_sdk.errors import AppNotRegisteredError
from imbue.app_sdk.errors import ShellUnreachableError

_INVENTORY = {
    "projects": [],
    "everything": {"id": "everything", "tabs": []},
    "apps": [
        {"name": "chat", "url": "http://localhost:8010", "display_name": "Chat", "is_running": True},
        {"name": "notes", "url": "http://localhost:8084/", "display_name": "Notes", "is_running": False},
    ],
    "clients": [],
    "is_preview": False,
}


def test_an_app_is_found_by_its_registered_name(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/api/inventory"] = json.dumps(_INVENTORY).encode()

    assert resolve_app_url("chat") == "http://localhost:8010"
    assert resolve_app_url("notes") == "http://localhost:8084"


def test_a_name_the_workspace_does_not_register_is_refused_by_name(shell_stand_in: ShellStandIn) -> None:
    shell_stand_in.body_by_path["/api/inventory"] = json.dumps(_INVENTORY).encode()

    with pytest.raises(AppNotRegisteredError, match="datalib"):
        resolve_app_url("datalib")


def test_with_no_shell_reachable_the_lookup_says_so(unreachable_shell_url: str) -> None:
    with pytest.raises(ShellUnreachableError, match=unreachable_shell_url):
        resolve_app_url("chat")


@pytest.mark.parametrize("body", [b"<html>not the inventory</html>", b'{"apps": [{"name": "chat"}]}', b"\xff\xfe"])
def test_an_answer_that_is_not_the_inventory_is_not_read_as_one(shell_stand_in: ShellStandIn, body: bytes) -> None:
    shell_stand_in.body_by_path["/api/inventory"] = body

    with pytest.raises(ShellUnreachableError, match="inventory"):
        resolve_app_url("chat")
