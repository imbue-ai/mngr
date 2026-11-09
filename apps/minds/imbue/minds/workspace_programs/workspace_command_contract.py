"""The commands Imbue Studio runs inside a workspace, declared once for both repos.

Every command the app (or one of its workspace programs) runs from the workspace's copy of
default-workspace-template is declared here: the file or entry point, how it is run, the
arguments the app passes, and the exit statuses and output each command answers with. The app
builds each command's arguments with the builders below, and ``workspace_command_contract_test.py``
checks the shapes they build against the invocations declared beside it, in both directions, so a
builder cannot send what this module does not declare.

The template enforces the other half. ``workspace_command_contract.json`` beside this module is
the same declaration as data (``render_published_contract``; a unit test keeps the two equal).
The template's build vendors that file from the mngr commit it pins
(``system/scripts/fetch_mngr_assets.sh``), and its always-run guard
``system/test_studio_command_contract.py`` checks every declared command against the
template's real scripts. A workspace agent that renames, moves or drops one of them, or one of
the options the app passes, fails that guard on its next test gate.

Raise ``CONTRACT_VERSION`` whenever a change here alters what an existing command must do (an
exit status's meaning, its output's shape), which the template's structural checks cannot see;
the template's guard pins the version it was last reviewed against, so the bump fails there
until someone re-checks the scripts. Adding a command or an option needs no bump: the
structural checks catch a template without it. See ``apps/minds/docs/workspace-command-contract.md``.
"""

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Final
from typing import TypedDict
from typing import assert_never

CONTRACT_VERSION: Final[int] = 1


class CommandRunner(StrEnum):
    """How the app runs a command, from the workspace root."""

    SYSTEM_PYTHON = "system_python"
    """``python3 <path> ...``: the image's own interpreter, with no venv, so standard library only."""
    ROOT_VENV_ENTRY_POINT = "root_venv_entry_point"
    """``uv run --no-sync <name> ...``: a console script of the template's root venv."""


class Invocation(TypedDict):
    """One shape of arguments the app passes a command."""

    purpose: str
    # The leading positional words that select a subcommand, if the command has any.
    subcommand: tuple[str, ...]
    # Options followed by a value, as one token (``--name=x``) or two (``--name x``).
    value_options: tuple[str, ...]
    flag_options: tuple[str, ...]
    positional_count: int
    # Values the app passes verbatim where the command takes one of a fixed set (an argparse choice).
    fixed_values: tuple[str, ...]


class ExitStatus(TypedDict):
    """One exit status the command answers with, and what it means; the app may read several alike."""

    code: int
    meaning: str


class WorkspaceCommand(TypedDict):
    """One command the app runs inside the workspace."""

    name: str
    # A path relative to the workspace root for SYSTEM_PYTHON, a console script name otherwise.
    target: str
    runner: CommandRunner
    invocations: tuple[Invocation, ...]
    exit_statuses: tuple[ExitStatus, ...]
    output: str
    # The status an argument the command does not know exits with; the app reads it as a
    # template from before an invocation and falls back. None when the app relies on no such status.
    usage_error_exit_code: int | None


class RequiredPath(TypedDict):
    """A template file or directory the app checks for, or runs, by its path rather than through a declared command."""

    path: str
    reason: str


def _invocation(
    purpose: str,
    *,
    subcommand: tuple[str, ...] = (),
    value_options: tuple[str, ...] = (),
    flag_options: tuple[str, ...] = (),
    positional_count: int = 0,
    fixed_values: tuple[str, ...] = (),
) -> Invocation:
    return Invocation(
        purpose=purpose,
        subcommand=subcommand,
        value_options=value_options,
        flag_options=flag_options,
        positional_count=positional_count,
        fixed_values=fixed_values,
    )


# ``message_chat.py``'s statuses, in ``mngr message``'s vocabulary.
MESSAGE_CHAT_EXIT_DELIVERED: Final[int] = 0
MESSAGE_CHAT_EXIT_FAILED: Final[int] = 1
MESSAGE_CHAT_EXIT_USAGE_ERROR: Final[int] = 2
MESSAGE_CHAT_EXIT_DELIVERED_BEHIND_DIALOG: Final[int] = 7
MESSAGE_CHAT_EXIT_CHAT_GONE: Final[int] = 8

# In argparse's last stderr line when it rejects an argument (``<prog>: error: <why>``), which sets a
# usage error apart from python's own "can't open file" for a missing script, at the same status.
ARGPARSE_USAGE_ERROR_MARKER: Final[str] = ": error: "

MESSAGE_CHAT: Final[WorkspaceCommand] = WorkspaceCommand(
    name="message_chat",
    target="system/scripts/message_chat.py",
    runner=CommandRunner.SYSTEM_PYTHON,
    invocations=(
        _invocation(
            "send a message to a chat by its id (a latchkey resolution nudge, an update chat's reminder)",
            value_options=("-m",),
            positional_count=1,
        ),
        _invocation(
            "create a chat through the chat app, its first message given inline (the /assist launch)",
            value_options=("--name", "--label", "-m"),
            flag_options=("--create",),
        ),
        _invocation(
            "create a chat through the chat app, its first message read from a file (the update chat)",
            value_options=("--name", "--label", "--message-file"),
            flag_options=("--create",),
        ),
    ),
    exit_statuses=(
        ExitStatus(code=MESSAGE_CHAT_EXIT_DELIVERED, meaning="delivered or queued, or the chat was created"),
        ExitStatus(code=MESSAGE_CHAT_EXIT_FAILED, meaning="refused; the last stderr line is the reason"),
        ExitStatus(
            code=MESSAGE_CHAT_EXIT_DELIVERED_BEHIND_DIALOG,
            meaning="delivered, but the agent's input is blocked on a dialog; a resend would type it again",
        ),
        ExitStatus(
            code=MESSAGE_CHAT_EXIT_CHAT_GONE,
            meaning="neither the chat app nor mngr knows the chat, so no later try can deliver it",
        ),
    ),
    output=(
        "a create prints one JSON line on stdout, an object with string keys chat_id, name and "
        "display_name; a send prints nothing the app reads"
    ),
    usage_error_exit_code=MESSAGE_CHAT_EXIT_USAGE_ERROR,
)

SEED_WELCOME_CHAT: Final[WorkspaceCommand] = WorkspaceCommand(
    name="seed_welcome_chat",
    target="system/scripts/seed_welcome_chat.py",
    runner=CommandRunner.SYSTEM_PYTHON,
    invocations=(
        _invocation(
            "open the welcome chat on the conversation the app had before the workspace existed",
            value_options=("--transcript-base64",),
        ),
    ),
    exit_statuses=(
        ExitStatus(code=0, meaning="seeded"),
        ExitStatus(code=1, meaning="not seeded; stderr says why"),
    ),
    output=(
        'one JSON line on stdout, {"chat_id": <string>}; the transcript is base64 of the JSON '
        'object {"title": <string>, "turns": [{"role": "user" | "assistant", "text": <string>}]}'
    ),
    usage_error_exit_code=None,
)

COLLECT_BUG_REPORT_DIAGNOSTICS: Final[WorkspaceCommand] = WorkspaceCommand(
    name="collect_bug_report_diagnostics",
    target="system/scripts/collect_bug_report_diagnostics.py",
    runner=CommandRunner.SYSTEM_PYTHON,
    invocations=(
        _invocation(
            "collect the workspace logs and/or recent transcripts for a bug report",
            value_options=("--scan-timeout",),
            flag_options=("--logs", "--transcript"),
        ),
    ),
    exit_statuses=(ExitStatus(code=0, meaning="collected (or nothing was requested)"),),
    output=(
        "one line of base64 on stdout, a zip archive the app stages verbatim; nothing when neither "
        "--logs nor --transcript was given"
    ),
    usage_error_exit_code=None,
)


class BackgroundTasksFormat(StrEnum):
    """``background_tasks.py list --format``'s values."""

    TSV = "tsv"
    JSON = "json"


BACKGROUND_TASKS: Final[WorkspaceCommand] = WorkspaceCommand(
    name="background_tasks",
    target="system/scripts/background_tasks.py",
    runner=CommandRunner.SYSTEM_PYTHON,
    invocations=(
        _invocation(
            "list the chats waiting on background tasks (the update run probe, its dispatcher, the backup gate)",
            subcommand=("list",),
            value_options=("--format",),
            fixed_values=tuple(BackgroundTasksFormat),
        ),
    ),
    exit_statuses=(ExitStatus(code=0, meaning="listed; any other status reads as no chat busy"),),
    output=(
        "with --format tsv, one line per chat: chat_id, active_agent_id, active_agent_name, is_busy "
        "('true' or 'false'), task_count, tab-separated; with --format json, an object whose chats "
        "list holds objects with chat_id, active_agent_id, active_agent_name, is_busy and "
        "background_tasks (objects with source and pid)"
    ),
    usage_error_exit_code=None,
)


class UpdateSelfVerdict(StrEnum):
    """The verdicts the app records through ``update_self.py run-status verdict``."""

    STUCK = "STUCK"


UPDATE_SELF: Final[WorkspaceCommand] = WorkspaceCommand(
    name="update_self",
    target=".agents/skills/update-self/scripts/update_self.py",
    runner=CommandRunner.SYSTEM_PYTHON,
    invocations=(
        _invocation(
            "record a verdict on the running update (the dispatcher's STUCK when the update chat is gone)",
            subcommand=("run-status", "verdict"),
            value_options=("--chat", "--detail"),
            positional_count=1,
            fixed_values=tuple(UpdateSelfVerdict),
        ),
    ),
    exit_statuses=(ExitStatus(code=0, meaning="recorded; any other status is retried on the next tick"),),
    output="nothing the app reads; the verdict lands in data/.state/update-apply/run.json",
    usage_error_exit_code=None,
)


class WorkspaceLayoutIfPresent(StrEnum):
    """The ``workspace-layout open --if-present`` values the app passes."""

    NEW = "new"
    """Open another window even when the app already has one."""


WORKSPACE_LAYOUT: Final[WorkspaceCommand] = WorkspaceCommand(
    name="workspace_layout",
    target="workspace-layout",
    runner=CommandRunner.ROOT_VENV_ENTRY_POINT,
    invocations=(
        _invocation(
            "open a new chat window on the provider chooser (the sign-in prompt before an update)",
            subcommand=("open",),
            value_options=("--path", "--if-present"),
            positional_count=1,
            fixed_values=tuple(WorkspaceLayoutIfPresent),
        ),
    ),
    exit_statuses=(ExitStatus(code=0, meaning="opened; any other status is retried while the client connects"),),
    output="nothing the app reads",
    usage_error_exit_code=None,
)

AGENT_ACCESS: Final[WorkspaceCommand] = WorkspaceCommand(
    name="agent_access",
    target="agent-access",
    runner=CommandRunner.ROOT_VENV_ENTRY_POINT,
    invocations=(
        _invocation("list the visiting agents", subcommand=("list",), flag_options=("--json",)),
        _invocation(
            "mint an invite for a visiting agent",
            subcommand=("invite",),
            value_options=("--granter", "--scope", "--expires-in-days", "--name-hint"),
            flag_options=("--json",),
        ),
        _invocation(
            "revoke a visiting agent as the workspace's owner",
            subcommand=("revoke",),
            value_options=("--revoker",),
            flag_options=("--owner", "--json"),
            positional_count=1,
        ),
    ),
    exit_statuses=(
        ExitStatus(code=0, meaning="done; stdout is the answer"),
        ExitStatus(
            code=2,
            meaning='refused; the last stderr line is {"error": <string>, "kind": "refused"}',
        ),
        ExitStatus(
            code=3,
            meaning='misconfigured; the last stderr line is {"error": <string>, "kind": "misconfigured"}',
        ),
    ),
    output=(
        "one JSON object on stdout: list has agents (objects with agent_id, name, fingerprint, scope, "
        "granter, created_at, expires_at, last_seen_at); invite has code, expires_at, scope, granter, "
        "auth_origin, is_published, instructions; revoke has revoked"
    ),
    usage_error_exit_code=None,
)

WORKSPACE_COMMANDS: Final[tuple[WorkspaceCommand, ...]] = (
    MESSAGE_CHAT,
    SEED_WELCOME_CHAT,
    COLLECT_BUG_REPORT_DIAGNOSTICS,
    BACKGROUND_TASKS,
    UPDATE_SELF,
    WORKSPACE_LAYOUT,
    AGENT_ACCESS,
)


def skill_definition_path(skill_name: str) -> str:
    return f".agents/skills/{skill_name}/SKILL.md"


ASSIST_SKILL_DEFINITION: Final[str] = skill_definition_path("assist")
UPDATE_SELF_SKILL_DEFINITION: Final[str] = skill_definition_path("update-self")
ACCOUNT_GATE_SCRIPT: Final[str] = "system/scripts/require_create_account.py"
AGENT_ACCESS_MODULE: Final[str] = "system/services/share_gateway/src/share_gateway/agent_cli.py"
AGENT_ENVIRONMENT_WRAPPER: Final[str] = "system/libs/routines/with_agent_env.sh"
SHARE_GATEWAY_SERVICE_DIRECTORY: Final[str] = "system/services/share_gateway"
BACKUP_SERVICE_DIRECTORY: Final[str] = "system/services/host_backup"

REQUIRED_PATHS: Final[tuple[RequiredPath, ...]] = (
    RequiredPath(
        path=ASSIST_SKILL_DEFINITION,
        reason="the help modal's /assist launch runs only in a workspace that has the skill",
    ),
    RequiredPath(
        path=UPDATE_SELF_SKILL_DEFINITION,
        reason="the app updates only a workspace that has the skill; without it the workspace is too old to update",
    ),
    RequiredPath(
        path=ACCOUNT_GATE_SCRIPT,
        reason=(
            "the update dispatcher reads its presence, beside a missing .mngr/settings.local.toml, "
            "as no provider account signed in"
        ),
    ),
    RequiredPath(
        path=AGENT_ACCESS_MODULE,
        reason="the app runs agent-access only where this module exists, and otherwise says the workspace predates it",
    ),
    RequiredPath(
        path=AGENT_ENVIRONMENT_WRAPPER,
        reason="the update dispatcher's cron tick reads an agent's environment through it (bash <path> env -0)",
    ),
    RequiredPath(
        path=SHARE_GATEWAY_SERVICE_DIRECTORY,
        reason="the app publishes only a workspace that has the share gateway, and otherwise says it predates sharing",
    ),
    RequiredPath(
        path=BACKUP_SERVICE_DIRECTORY,
        reason="the backup programs find the backup service here, and otherwise read the workspace as an older layout",
    ),
)


def command_argv(command: WorkspaceCommand, arguments: Sequence[str], python: str = "python3") -> list[str]:
    """The full argv that runs ``command`` with ``arguments`` from the workspace root."""
    match command["runner"]:
        case CommandRunner.SYSTEM_PYTHON:
            return [python, command["target"], *arguments]
        case CommandRunner.ROOT_VENV_ENTRY_POINT:
            return ["uv", "run", "--no-sync", command["target"], *arguments]
        case _ as unreachable:
            assert_never(unreachable)


def message_chat_send_arguments(chat_id: str, text: str) -> list[str]:
    return [chat_id, "-m", text]


def _message_chat_create_head(name: str, labels: Sequence[tuple[str, str]]) -> list[str]:
    arguments = ["--create", "--name", name]
    for key, value in labels:
        arguments += ["--label", "%s=%s" % (key, value)]
    return arguments


def message_chat_create_arguments(name: str, labels: Sequence[tuple[str, str]], message: str) -> list[str]:
    return [*_message_chat_create_head(name, labels), "-m", message]


def message_chat_create_from_file_arguments(
    name: str, labels: Sequence[tuple[str, str]], message_path: str
) -> list[str]:
    return [*_message_chat_create_head(name, labels), "--message-file", message_path]


def seed_welcome_chat_arguments(transcript_base64: str) -> list[str]:
    return ["--transcript-base64", transcript_base64]


def collect_bug_report_diagnostics_arguments(
    is_logs_included: bool, is_transcript_included: bool, scan_timeout_seconds: int
) -> list[str]:
    arguments = []
    if is_logs_included:
        arguments.append("--logs")
    if is_transcript_included:
        arguments.append("--transcript")
    arguments.append("--scan-timeout=%d" % scan_timeout_seconds)
    return arguments


def background_tasks_list_arguments(output_format: BackgroundTasksFormat) -> list[str]:
    return ["list", "--format", output_format]


def update_self_verdict_arguments(verdict: UpdateSelfVerdict, chat_name: str, detail: str) -> list[str]:
    return ["run-status", "verdict", verdict, "--chat", chat_name, "--detail", detail]


def workspace_layout_open_arguments(app_name: str, path: str, if_present: WorkspaceLayoutIfPresent) -> list[str]:
    return ["open", app_name, "--path", path, "--if-present", if_present]


def agent_access_list_arguments() -> list[str]:
    return ["list", "--json"]


def agent_access_invite_arguments(
    granter: str, scope: str, expires_in_days: float | None, name_hint: str
) -> list[str]:
    arguments = ["invite", "--granter", granter, "--scope", scope]
    if expires_in_days is not None:
        arguments += ["--expires-in-days", str(expires_in_days)]
    if name_hint:
        arguments += ["--name-hint", name_hint]
    return [*arguments, "--json"]


def agent_access_revoke_arguments(agent_id: str, revoker: str) -> list[str]:
    return ["revoke", agent_id, "--revoker", revoker, "--owner", "--json"]


def _matches_invocation(invocation: Invocation, arguments: Sequence[str]) -> bool:
    subcommand = invocation["subcommand"]
    if tuple(arguments[: len(subcommand)]) != subcommand:
        return False
    rest = list(arguments[len(subcommand) :])
    positional_count = 0
    index = 0
    while index < len(rest):
        token = rest[index]
        option, separator, _ = token.partition("=")
        has_inline_value = separator == "="
        if token.startswith("-") and option in invocation["value_options"]:
            if not has_inline_value and index + 1 >= len(rest):
                return False
            index += 1 if has_inline_value else 2
        elif token.startswith("-") and token in invocation["flag_options"]:
            index += 1
        elif token.startswith("-"):
            return False
        else:
            positional_count += 1
            index += 1
    return positional_count == invocation["positional_count"]


def find_matching_invocation(command: WorkspaceCommand, arguments: Sequence[str]) -> Invocation | None:
    """The declared invocation ``arguments`` (everything after the command itself) fits, or None."""
    for invocation in command["invocations"]:
        if _matches_invocation(invocation, arguments):
            return invocation
    return None


def render_published_contract() -> str:
    """The declaration as the JSON the template vendors: ``workspace_command_contract.json``'s exact text."""
    published = {
        "contract_version": CONTRACT_VERSION,
        "commands": WORKSPACE_COMMANDS,
        "required_paths": REQUIRED_PATHS,
    }
    return json.dumps(published, indent=2) + "\n"
