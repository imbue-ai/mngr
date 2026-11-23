import copy
import importlib.resources
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.agents.upgrade import AGENT_DATA_RELATIVE_PATH
from imbue.mngr.agents.upgrade import build_command_script_files
from imbue.mngr.agents.upgrade import build_shared_shell_lib_files
from imbue.mngr.agents.upgrade import is_hooks_value_well_formed
from imbue.mngr.agents.upgrade import parse_json_object_input
from imbue.mngr.agents.upgrade import read_stored_launch_command
from imbue.mngr.agents.upgrade import rebuild_hook_groups_keeping_other_entries
from imbue.mngr.errors import InvalidUpgradeInputError
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.data_types import HarnessFileSpec


def test_build_command_script_files_puts_each_script_in_commands_as_an_executable() -> None:
    assert build_command_script_files({"a.sh": "echo a-48213\n", "b.py": "print('b-48213')\n"}) == {
        Path("commands/a.sh"): HarnessFileSpec(content="echo a-48213\n", mode="0755"),
        Path("commands/b.py"): HarnessFileSpec(content="print('b-48213')\n", mode="0755"),
    }


def test_build_shared_shell_lib_files_holds_every_shared_lib_as_this_mngr_ships_it() -> None:
    shared_lib_files = build_shared_shell_lib_files()

    assert list(shared_lib_files) == [Path("commands") / name for name in SHARED_SHELL_LIB_NAMES]
    for name in SHARED_SHELL_LIB_NAMES:
        shipped_content = importlib.resources.files(mngr_resources).joinpath(name).read_text()
        assert shared_lib_files[Path("commands") / name] == HarnessFileSpec(content=shipped_content, mode="0755")


@pytest.mark.parametrize(
    ("agent_data_content", "expected_command"),
    [
        pytest.param(None, "", id="missing"),
        pytest.param(json.dumps({"id": "agent-48213", "command": "sleep 48213"}), "sleep 48213", id="stored"),
        pytest.param(json.dumps({"id": "agent-48213"}), "", id="no_command"),
        pytest.param(json.dumps({"command": None}), "", id="null_command"),
        pytest.param(json.dumps(["sleep 48213"]), "", id="not_an_object"),
    ],
)
def test_read_stored_launch_command_returns_the_stored_command_or_empty(
    agent_data_content: str | None, expected_command: str
) -> None:
    assert read_stored_launch_command(agent_data_content) == expected_command


def test_read_stored_launch_command_refuses_a_record_that_is_not_json() -> None:
    with pytest.raises(InvalidUpgradeInputError, match=f"^{AGENT_DATA_RELATIVE_PATH} is not valid JSON: "):
        read_stored_launch_command("{not json 48213")


_SETTINGS_PATH = Path("plugin/settings-48213.json")


@pytest.mark.parametrize(
    ("content", "expected_object"),
    [
        pytest.param(None, {}, id="missing"),
        pytest.param("{}", {}, id="empty_object"),
        pytest.param('{"hooks": {}, "model": "m-48213"}', {"hooks": {}, "model": "m-48213"}, id="object"),
    ],
)
def test_parse_json_object_input_returns_the_object_or_empty_for_a_missing_file(
    content: str | None, expected_object: dict[str, Any]
) -> None:
    assert parse_json_object_input(_SETTINGS_PATH, content) == expected_object


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        pytest.param(
            "{not json",
            f"{_SETTINGS_PATH} is not valid JSON: Expecting property name enclosed in double quotes: "
            "line 1 column 2 (char 1)",
            id="not_json",
        ),
        pytest.param("[]", f"{_SETTINGS_PATH} does not hold a JSON object", id="list"),
        pytest.param('"text-48213"', f"{_SETTINGS_PATH} does not hold a JSON object", id="string"),
    ],
)
def test_parse_json_object_input_refuses_invalid_json_and_other_values(content: str, expected_message: str) -> None:
    with pytest.raises(InvalidUpgradeInputError) as exc_info:
        parse_json_object_input(_SETTINGS_PATH, content)

    assert str(exc_info.value) == expected_message


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        pytest.param({}, True, id="empty"),
        pytest.param({"Stop": [{"hooks": [{"type": "command", "command": "echo 48213"}]}]}, True, id="one_entry"),
        pytest.param({"Stop": [{"matcher": "startup"}]}, True, id="group_without_entries"),
        pytest.param({"Stop": [{"hooks": ["not-an-object"]}]}, True, id="entry_that_is_not_an_object"),
        pytest.param([], False, id="list"),
        pytest.param(None, False, id="null"),
        pytest.param("hooks", False, id="string"),
        pytest.param({"Stop": {}}, False, id="event_that_is_not_a_list"),
        pytest.param({"Stop": ["not-an-object"]}, False, id="group_that_is_not_an_object"),
        pytest.param({"Stop": [{"hooks": {}}]}, False, id="entries_that_are_not_a_list"),
    ],
)
def test_is_hooks_value_well_formed_accepts_only_events_mapped_to_lists_of_groups(hooks: Any, expected: bool) -> None:
    assert is_hooks_value_well_formed(hooks) is expected


def _command_entry(command: str) -> dict[str, str]:
    return {"type": "command", "command": command}


def _command_group(*commands: str, matcher: str | None = None) -> dict[str, Any]:
    hook_group: dict[str, Any] = {"hooks": [_command_entry(command) for command in commands]}
    return hook_group if matcher is None else {"matcher": matcher, **hook_group}


def _is_test_mngr_hook_entry(hook_entry: Any, hook_group: Mapping[str, Any]) -> bool:
    """An entry is mngr's when its command starts with ``mngr-48213``, or it is ``allow-48213`` in a group matching every tool."""
    command = hook_entry.get("command") if isinstance(hook_entry, dict) else None
    if not isinstance(command, str):
        return False
    return command.startswith("mngr-48213") or (command == "allow-48213" and hook_group.get("matcher") == "*")


_MNGR_STOP_GROUP = _command_group("mngr-48213 stop")
_MNGR_HOOKS: dict[str, Any] = {
    "Stop": [_MNGR_STOP_GROUP],
    "SessionStart": [_command_group("mngr-48213 session-start", matcher="startup")],
}
_STALE_MNGR_GROUP = _command_group("mngr-48213 stale")


def test_rebuild_hook_groups_puts_mngrs_groups_where_the_first_group_holding_a_mngr_entry_was() -> None:
    group_before = _command_group("echo before-48213")
    group_between = _command_group("echo between-48213")
    group_after = _command_group("echo after-48213")
    live_hooks = {
        "Stop": [
            group_before,
            _command_group("mngr-48213 stale", "echo mixed-48213", matcher="mixed"),
            group_between,
            _STALE_MNGR_GROUP,
            group_after,
        ]
    }

    rebuilt_hooks = rebuild_hook_groups_keeping_other_entries(live_hooks, _MNGR_HOOKS, _is_test_mngr_hook_entry)

    assert rebuilt_hooks["Stop"] == [
        group_before,
        _MNGR_STOP_GROUP,
        _command_group("echo mixed-48213", matcher="mixed"),
        group_between,
        group_after,
    ]


def test_rebuild_hook_groups_appends_mngrs_groups_to_an_event_holding_none_of_its_entries() -> None:
    scoped_allow_group = _command_group("allow-48213", matcher="Bash")
    live_hooks = {"Stop": [scoped_allow_group]}

    rebuilt_hooks = rebuild_hook_groups_keeping_other_entries(live_hooks, _MNGR_HOOKS, _is_test_mngr_hook_entry)

    assert rebuilt_hooks["Stop"] == [scoped_allow_group, _MNGR_STOP_GROUP]


def test_rebuild_hook_groups_drops_events_left_without_groups_and_appends_events_only_mngr_uses() -> None:
    user_group = _command_group("echo user-48213")
    live_hooks = {
        "PermissionRequest": [_command_group("allow-48213", matcher="*")],
        "Notification": [],
        "Stop": [user_group],
    }

    rebuilt_hooks = rebuild_hook_groups_keeping_other_entries(live_hooks, _MNGR_HOOKS, _is_test_mngr_hook_entry)

    assert rebuilt_hooks == {
        "Notification": [],
        "Stop": [user_group, _MNGR_STOP_GROUP],
        "SessionStart": _MNGR_HOOKS["SessionStart"],
    }
    assert list(rebuilt_hooks) == ["Notification", "Stop", "SessionStart"]


def test_rebuild_hook_groups_is_idempotent_and_leaves_its_input_alone() -> None:
    live_hooks = {
        "Stop": [_command_group("echo before-48213"), _STALE_MNGR_GROUP, _command_group("echo after-48213")],
        "SubagentStop": [_command_group("mngr-48213 stale", "echo mixed-48213")],
    }
    live_hooks_before = copy.deepcopy(live_hooks)

    rebuilt_hooks = rebuild_hook_groups_keeping_other_entries(live_hooks, _MNGR_HOOKS, _is_test_mngr_hook_entry)
    rebuilt_again_hooks = rebuild_hook_groups_keeping_other_entries(
        rebuilt_hooks, _MNGR_HOOKS, _is_test_mngr_hook_entry
    )

    assert rebuilt_again_hooks == rebuilt_hooks
    assert list(rebuilt_again_hooks) == list(rebuilt_hooks)
    assert live_hooks == live_hooks_before
