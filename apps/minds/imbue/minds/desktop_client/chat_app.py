"""Ask a workspace's chat app for something through the template's ``system/scripts/message_chat.py``.

Every chat the app reaches goes through this one script, run inside the workspace by
``mngr exec``: a message to a chat by id (``message_chat.py <chat-id> -m ...``) and a new
chat (``message_chat.py --create ...``) are the same run with different arguments, so the
chat app mints, binds, and delivers exactly as it does for a chat started inside the
workspace. What differs between callers is only what they do with the answer: retry it,
show it, or fall back to their own ``mngr`` command when the script gave none.

``mngr exec --format jsonl`` reports one ``exec_result`` event with the inner command's
stdout and stderr and only a boolean ``success``, so the script's exit status, which is its
verdict, is echoed behind :data:`EXIT_SENTINEL` and read back from the inner stdout.
"""

from collections.abc import Sequence
from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.in_workspace_mngr import FAILURE_DETAIL_MAX_CHARS
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_command
from imbue.minds.desktop_client.in_workspace_mngr import exec_log_detail
from imbue.minds.desktop_client.in_workspace_mngr import exec_verdict_detail
from imbue.minds.desktop_client.in_workspace_mngr import inner_stderr_from_exec_result
from imbue.minds.desktop_client.in_workspace_mngr import inner_stdout_from_exec_result
from imbue.minds.utils.mngr_caller import MngrCaller

# Relative to the workspace repo root, which is the cwd ``mngr exec`` gives every command
# there. Its path is the contract; see its docstring in the template for the exit codes.
MESSAGE_CHAT_SCRIPT: Final[str] = "system/scripts/message_chat.py"

EXIT_SENTINEL: Final[str] = "MNGR_INNER_EXIT="


class ChatAppVerdict(UpperCaseStrEnum):
    """What the chat app answered, read from the script's exit status."""

    DELIVERED = auto()
    """The message was delivered or queued, or the chat was created."""
    DELIVERED_BEHIND_DIALOG = auto()
    """The text is in the pane, but the agent's input is blocked on a dialog; a resend would type it again."""
    REFUSED = auto()
    """The chat app (or the script's own backoff) declined; its reason is the answer's detail."""
    NO_VERDICT = auto()
    """The script could not run or did not know the arguments: the template predates it or the mode."""
    UNANSWERED = auto()
    """The exec never ran the script to its end: the workspace was down, unreachable, or timed out."""


class ChatAppAnswer(FrozenModel):
    """One run of the messaging script, as the caller needs to act on it."""

    verdict: ChatAppVerdict = Field(description="What the chat app answered")
    detail: str = Field(
        description=(
            "The workspace's own account of anything short of delivery, bounded and stripped of the "
            "outer mngr's chatter so it can be shown as-is; '' when it gave none"
        )
    )
    log_detail: str = Field(description="``detail``, or failing that minds' own words about the run; for logs only")
    script_exit_code: int | None = Field(description="The script's exit status; None when it never ran to its echo")
    exec_returncode: int = Field(description="The outer ``mngr exec``'s own exit status")

    @property
    def is_delivered(self) -> bool:
        return self.verdict in (ChatAppVerdict.DELIVERED, ChatAppVerdict.DELIVERED_BEHIND_DIALOG)


# The script speaks ``mngr message``'s exit codes; any status not listed is a refusal. 2 is
# what python exits with when it cannot open the script (a template from before it) and
# what argparse exits with on an argument the script does not know (one from before a mode).
_VERDICT_BY_EXIT_CODE: Final[dict[int, ChatAppVerdict]] = {
    0: ChatAppVerdict.DELIVERED,
    7: ChatAppVerdict.DELIVERED_BEHIND_DIALOG,
    2: ChatAppVerdict.NO_VERDICT,
}


def build_message_chat_command(argv: Sequence[str]) -> str:
    """The shell string that runs the messaging script with ``argv`` and echoes its exit status.

    The echo runs whatever the script exits with, so the outer ``mngr exec`` succeeds and
    the status is read from the inner stdout (:func:`inner_exit_code`), where the script's
    own JSON line, if any, sits above it. The environment prefix reaches the script's own
    ``mngr`` backoff too.
    """
    script = build_in_workspace_command(["python3", MESSAGE_CHAT_SCRIPT, *argv])
    return f"{script}; echo {EXIT_SENTINEL}$?"


@pure
def build_message_chat_args(exec_agent_address: str, script_args: Sequence[str]) -> list[str]:
    """The ``mngr`` args that run the messaging script with ``script_args`` on ``exec_agent_address``.

    ``exec_agent_address`` is any agent of the workspace; the script finds the chat app from the
    workspace root, and the chat it acts on is named in ``script_args``. ``mngr exec`` takes
    ONE shell command string after its agents, so the invocation is quoted into a single
    argument. ``--no-start``: asking a chat app must never boot a stopped workspace.
    """
    return [
        "exec",
        "--agent",
        exec_agent_address,
        build_message_chat_command(script_args),
        "--no-start",
        "--format",
        "jsonl",
    ]


@pure
def inner_exit_code(inner_stdout: str) -> int | None:
    """The exit status a :func:`build_message_chat_command` shell echoed; None when the command never ran to its echo."""
    for line in reversed(inner_stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith(EXIT_SENTINEL):
            code = stripped.removeprefix(EXIT_SENTINEL)
            return int(code) if code.isdigit() else None
    return None


@pure
def _script_failure_detail(inner_stderr: str) -> str:
    """What the script said last on stderr, bounded for rendering; '' when it said nothing."""
    lines = [line.strip() for line in inner_stderr.splitlines() if line.strip()]
    return lines[-1][:FAILURE_DETAIL_MAX_CHARS] if lines else ""


def ask_chat_app(
    mngr_caller: MngrCaller, exec_agent_address: str, script_args: Sequence[str], *, timeout: float
) -> ChatAppAnswer:
    """Run the messaging script with ``script_args`` inside ``exec_agent_address``'s workspace and read its answer.

    The script's last stderr line is its own reason when it ran; when it never did, the
    reason is mngr's ``exec_error`` event or the in-workspace refusal behind the outer
    mngr's chatter (:func:`exec_verdict_detail`).
    """
    result = mngr_caller.call(build_message_chat_args(exec_agent_address, script_args), timeout=timeout)
    exit_code = inner_exit_code(inner_stdout_from_exec_result(result.stdout))
    detail = _script_failure_detail(inner_stderr_from_exec_result(result.stdout)) or exec_verdict_detail(result)
    verdict = (
        ChatAppVerdict.UNANSWERED
        if exit_code is None
        else _VERDICT_BY_EXIT_CODE.get(exit_code, ChatAppVerdict.REFUSED)
    )
    return ChatAppAnswer(
        verdict=verdict,
        detail=detail,
        log_detail=detail or exec_log_detail(result),
        script_exit_code=exit_code,
        exec_returncode=result.returncode,
    )
