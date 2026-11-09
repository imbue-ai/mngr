import shlex
from pathlib import Path

import pytest

from imbue.minds.desktop_client.assist_chat import ASSIST_SKILL_NAME
from imbue.minds.desktop_client.skill_chat import build_create_chat_script_args
from imbue.minds.desktop_client.skill_chat import build_skill_chat_launch_command
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRequest
from imbue.minds.desktop_client.welcome_chat import WelcomeChatRole
from imbue.minds.desktop_client.welcome_chat import WelcomeChatTurn
from imbue.minds.desktop_client.welcome_chat import build_seed_welcome_chat_args
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_COLLECTOR_PATH
from imbue.minds.desktop_client.workspace_diagnostics import build_diagnostics_argv
from imbue.minds.workspace_programs import workspace_command_contract
from imbue.minds.workspace_programs.workspace_command_contract import AGENT_ACCESS
from imbue.minds.workspace_programs.workspace_command_contract import ASSIST_SKILL_DEFINITION
from imbue.minds.workspace_programs.workspace_command_contract import BACKGROUND_TASKS
from imbue.minds.workspace_programs.workspace_command_contract import BackgroundTasksFormat
from imbue.minds.workspace_programs.workspace_command_contract import COLLECT_BUG_REPORT_DIAGNOSTICS
from imbue.minds.workspace_programs.workspace_command_contract import MESSAGE_CHAT
from imbue.minds.workspace_programs.workspace_command_contract import REQUIRED_PATHS
from imbue.minds.workspace_programs.workspace_command_contract import SEED_WELCOME_CHAT
from imbue.minds.workspace_programs.workspace_command_contract import UPDATE_SELF
from imbue.minds.workspace_programs.workspace_command_contract import UpdateSelfVerdict
from imbue.minds.workspace_programs.workspace_command_contract import WORKSPACE_COMMANDS
from imbue.minds.workspace_programs.workspace_command_contract import WORKSPACE_LAYOUT
from imbue.minds.workspace_programs.workspace_command_contract import WorkspaceCommand
from imbue.minds.workspace_programs.workspace_command_contract import WorkspaceLayoutIfPresent
from imbue.minds.workspace_programs.workspace_command_contract import agent_access_invite_arguments
from imbue.minds.workspace_programs.workspace_command_contract import agent_access_list_arguments
from imbue.minds.workspace_programs.workspace_command_contract import agent_access_revoke_arguments
from imbue.minds.workspace_programs.workspace_command_contract import background_tasks_list_arguments
from imbue.minds.workspace_programs.workspace_command_contract import collect_bug_report_diagnostics_arguments
from imbue.minds.workspace_programs.workspace_command_contract import command_argv
from imbue.minds.workspace_programs.workspace_command_contract import find_matching_invocation
from imbue.minds.workspace_programs.workspace_command_contract import message_chat_create_arguments
from imbue.minds.workspace_programs.workspace_command_contract import message_chat_create_from_file_arguments
from imbue.minds.workspace_programs.workspace_command_contract import message_chat_send_arguments
from imbue.minds.workspace_programs.workspace_command_contract import render_published_contract
from imbue.minds.workspace_programs.workspace_command_contract import seed_welcome_chat_arguments
from imbue.minds.workspace_programs.workspace_command_contract import update_self_verdict_arguments
from imbue.minds.workspace_programs.workspace_command_contract import workspace_layout_open_arguments

_PUBLISHED_CONTRACT_PATH = Path(workspace_command_contract.__file__).with_suffix(".json")

# Every argument shape the builders produce, with the optional parts both present and absent.
_BUILT_ARGUMENTS: tuple[tuple[WorkspaceCommand, list[str]], ...] = (
    (MESSAGE_CHAT, message_chat_send_arguments("agent-1", "hi")),
    (MESSAGE_CHAT, message_chat_send_arguments("agent-1", "-starts with a dash")),
    (MESSAGE_CHAT, message_chat_create_arguments("assist-1a2b3c", [("assist", "true"), ("auto_open", "true")], "/x")),
    (MESSAGE_CHAT, message_chat_create_arguments("Chat 3", [], "")),
    (MESSAGE_CHAT, message_chat_create_from_file_arguments("Update", [("a", "b")], "/tmp/message.md")),
    (SEED_WELCOME_CHAT, seed_welcome_chat_arguments("eyJ0aXRsZSI6ICJ4In0=")),
    (COLLECT_BUG_REPORT_DIAGNOSTICS, collect_bug_report_diagnostics_arguments(True, True, 180)),
    (COLLECT_BUG_REPORT_DIAGNOSTICS, collect_bug_report_diagnostics_arguments(False, False, 180)),
    (BACKGROUND_TASKS, background_tasks_list_arguments(BackgroundTasksFormat.TSV)),
    (BACKGROUND_TASKS, background_tasks_list_arguments(BackgroundTasksFormat.JSON)),
    (UPDATE_SELF, update_self_verdict_arguments(UpdateSelfVerdict.STUCK, "update-1", "the lead is gone")),
    (WORKSPACE_LAYOUT, workspace_layout_open_arguments("chat", "/?open=providers", WorkspaceLayoutIfPresent.NEW)),
    (AGENT_ACCESS, agent_access_list_arguments()),
    (AGENT_ACCESS, agent_access_invite_arguments("user:8c0f", "workspace", 7.0, "Ada's Codex")),
    (AGENT_ACCESS, agent_access_invite_arguments("user:8c0f", "chat,files", None, "")),
    (AGENT_ACCESS, agent_access_revoke_arguments("va-" + "1" * 32, "user:8c0f")),
)


def _built_arguments_id(value: WorkspaceCommand | list[str]) -> str:
    return " ".join(value) if isinstance(value, list) else value["name"]


@pytest.mark.parametrize(("command", "arguments"), _BUILT_ARGUMENTS, ids=_built_arguments_id)
def test_every_built_argument_shape_is_a_declared_invocation(command: WorkspaceCommand, arguments: list[str]) -> None:
    assert find_matching_invocation(command, arguments) is not None, (
        f"{command['name']} is run with {arguments}, which no invocation in the contract declares"
    )


def test_every_declared_invocation_is_one_the_app_builds() -> None:
    """A declared invocation the app no longer passes would still bind the template to support it."""
    built = [find_matching_invocation(command, arguments) for command, arguments in _BUILT_ARGUMENTS]
    unbuilt = [
        f"{command['name']}: {invocation['purpose']}"
        for command in WORKSPACE_COMMANDS
        for invocation in command["invocations"]
        if not any(match is invocation for match in built)
    ]

    assert unbuilt == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["agent-1", "-m", "hi", "--browser-fleet"],
        ["agent-1", "agent-2", "-m", "hi"],
        ["agent-1", "-m"],
        ["--create", "--name", "x", "-m", "hi", "--message-file", "f"],
    ],
)
def test_an_undeclared_argument_shape_matches_no_invocation(arguments: list[str]) -> None:
    assert find_matching_invocation(MESSAGE_CHAT, arguments) is None


def test_a_subcommand_the_contract_does_not_declare_matches_no_invocation() -> None:
    assert find_matching_invocation(BACKGROUND_TASKS, ["is-busy", "chat-1"]) is None
    assert find_matching_invocation(AGENT_ACCESS, ["primer", "--json"]) is None


def test_the_commands_are_run_from_the_workspace_root_by_their_runner() -> None:
    assert command_argv(MESSAGE_CHAT, ["agent-1", "-m", "hi"], "/usr/bin/python3") == [
        "/usr/bin/python3",
        "system/scripts/message_chat.py",
        "agent-1",
        "-m",
        "hi",
    ]
    assert command_argv(AGENT_ACCESS, ["list", "--json"]) == [
        "uv",
        "run",
        "--no-sync",
        "agent-access",
        "list",
        "--json",
    ]


def test_command_names_and_targets_are_unique_and_every_command_declares_an_invocation() -> None:
    names = [command["name"] for command in WORKSPACE_COMMANDS]
    targets = [command["target"] for command in WORKSPACE_COMMANDS]

    assert len(set(names)) == len(names)
    assert len(set(targets)) == len(targets)
    assert all(command["invocations"] for command in WORKSPACE_COMMANDS)


def test_the_published_contract_file_is_the_declaration_the_app_builds_from() -> None:
    """The template checks itself against the JSON file, so it must say what this module says."""
    assert _PUBLISHED_CONTRACT_PATH.read_text() == render_published_contract(), (
        "workspace_command_contract.json is out of date; from the repo root, rewrite it with: "
        "uv run python -c 'from imbue.minds.workspace_programs.workspace_command_contract import "
        'render_published_contract as r; print(r(), end="")\' '
        "> apps/minds/imbue/minds/workspace_programs/workspace_command_contract.json"
    )


def test_the_assist_launch_creates_its_chat_with_a_declared_invocation() -> None:
    arguments = build_create_chat_script_args(chat_name="assist-1a2b3c", message="/assist it broke")

    assert find_matching_invocation(MESSAGE_CHAT, arguments) is not None


def test_the_assist_launch_checks_for_the_declared_skill_definition() -> None:
    launch = build_skill_chat_launch_command(
        skill_name=ASSIST_SKILL_NAME, chat_name="assist-1a2b3c", message="/assist"
    )

    assert launch.startswith(f"if [ -f {ASSIST_SKILL_DEFINITION} ]; then ")
    assert ASSIST_SKILL_DEFINITION in {required["path"] for required in REQUIRED_PATHS}


def test_the_welcome_chat_seed_runs_a_declared_invocation() -> None:
    request = WelcomeChatRequest(title="Welcome", turns=(WelcomeChatTurn(role=WelcomeChatRole.USER, text="hi"),))
    command = shlex.split(build_seed_welcome_chat_args("agent-1", request)[3])

    assert command[:2] == ["python3", SEED_WELCOME_CHAT["target"]]
    assert find_matching_invocation(SEED_WELCOME_CHAT, command[2:]) is not None


@pytest.mark.parametrize(("is_logs_included", "is_transcript_included"), [(True, True), (False, True), (True, False)])
def test_the_bug_report_collector_runs_a_declared_invocation(
    is_logs_included: bool, is_transcript_included: bool
) -> None:
    command = shlex.split(build_diagnostics_argv("agent-1", is_logs_included, is_transcript_included)[3])

    assert command[1] == WORKSPACE_COLLECTOR_PATH
    assert find_matching_invocation(COLLECT_BUG_REPORT_DIAGNOSTICS, command[2:]) is not None
