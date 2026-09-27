"""Ask a workspace's chat app for something through the template's ``system/scripts/message_chat.py``.

Every chat the app reaches goes through this one script, run inside the workspace by
``mngr exec``: a message to a chat by id (``message_chat.py <chat-id> -m ...``) and a new
chat (``message_chat.py --create ...``) are the same run with different arguments, so the
chat app mints, binds, and delivers exactly as it does for a chat started inside the
workspace. What differs between callers is only what they do with the answer: retry it,
show it, or fall back to their own ``mngr`` command when the script gave none. That fallback
runs in the same shell, right behind the script, so a template from before the script (or
before a mode of it) costs no second ``mngr exec``.

``mngr exec --format jsonl`` reports one ``exec_result`` event with the inner command's
stdout and stderr and only a boolean ``success``, so the script's exit status, which is its
verdict, is echoed behind :data:`EXIT_SENTINEL` and read back from the inner stdout; the
fallback's output follows :data:`FALLBACK_BEGIN_SENTINEL` on both streams.
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
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller

# Relative to the workspace repo root, which is the cwd ``mngr exec`` gives every command
# there. Its path is the contract; see its docstring in the template for the exit codes.
MESSAGE_CHAT_SCRIPT: Final[str] = "system/scripts/message_chat.py"

EXIT_SENTINEL: Final[str] = "MNGR_INNER_EXIT="

# Opens the fallback's output on both streams, and closes it with the fallback's own exit status.
FALLBACK_BEGIN_SENTINEL: Final[str] = "MNGR_CHAT_APP_FALLBACK_BEGIN"
FALLBACK_EXIT_SENTINEL: Final[str] = "MNGR_CHAT_APP_FALLBACK_EXIT="


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


class ChatAppFallbackRun(FrozenModel):
    """What the caller's own command did behind a script that gave no verdict, in the same exec."""

    stdout: str = Field(description="The fallback's own stdout, without the sentinels around it")
    stderr: str = Field(description="The fallback's own stderr")
    exit_code: int | None = Field(description="The fallback's exit status; None when it never ran to its echo")


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
    fallback: ChatAppFallbackRun | None = Field(
        description="The caller's fallback command, run because the script gave no verdict; None when it did not run"
    )

    @property
    def is_delivered(self) -> bool:
        return self.verdict in (ChatAppVerdict.DELIVERED, ChatAppVerdict.DELIVERED_BEHIND_DIALOG)


# The script speaks ``mngr message``'s exit codes; any status not listed is a refusal. 2 is
# what python exits with when it cannot open the script (a template from before it) and
# what argparse exits with on an argument the script does not know (one from before a mode).
_NO_VERDICT_EXIT_CODE: Final[int] = 2
_VERDICT_BY_EXIT_CODE: Final[dict[int, ChatAppVerdict]] = {
    0: ChatAppVerdict.DELIVERED,
    7: ChatAppVerdict.DELIVERED_BEHIND_DIALOG,
    _NO_VERDICT_EXIT_CODE: ChatAppVerdict.NO_VERDICT,
}


def build_message_chat_command(argv: Sequence[str], fallback_command: str) -> str:
    """The shell string that runs the messaging script with ``argv``, and ``fallback_command`` if it gave no verdict.

    The script's status is echoed whatever it is, so the outer ``mngr exec`` succeeds and the
    status is read from the inner stdout (:func:`inner_exit_code`), where the script's own
    JSON line, if any, sits above it. The environment prefix reaches the script's own
    ``mngr`` backoff too. ``fallback_command`` is a complete shell command, run in a subshell
    so an ``exit`` in it cannot skip the echo; its output is fenced by
    :data:`FALLBACK_BEGIN_SENTINEL` and its status echoed behind :data:`FALLBACK_EXIT_SENTINEL`.
    """
    script = build_in_workspace_command(["python3", MESSAGE_CHAT_SCRIPT, *argv])
    return (
        f"{script}; mngr_chat_app_exit=$?; echo {EXIT_SENTINEL}$mngr_chat_app_exit; "
        f'if [ "$mngr_chat_app_exit" -eq {_NO_VERDICT_EXIT_CODE} ]; then '
        f"echo {FALLBACK_BEGIN_SENTINEL}; echo {FALLBACK_BEGIN_SENTINEL} >&2; "
        f"({fallback_command}); echo {FALLBACK_EXIT_SENTINEL}$?; fi"
    )


@pure
def build_chat_app_exec_args(exec_agent_address: str, command: str) -> list[str]:
    """The ``mngr`` args that run ``command``, a shell string built around the messaging script, on ``exec_agent_address``.

    ``exec_agent_address`` is any agent of the workspace; the script finds the chat app from the
    workspace root. ``mngr exec`` takes ONE shell command string after its agents.
    ``--no-start``: asking a chat app must never boot a stopped workspace.
    """
    return ["exec", "--agent", exec_agent_address, command, "--no-start", "--format", "jsonl"]


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


@pure
def _split_off_fallback_output(inner_output: str) -> tuple[str, str | None]:
    """The script's part of one inner stream, and the fallback's part; None when the fallback never started.

    ``mngr exec`` reports a stream as its lines joined, with no trailing newline, so the sentinel
    is the stream's last line when the fallback wrote nothing to it.
    """
    lines = inner_output.split("\n")
    if FALLBACK_BEGIN_SENTINEL not in lines:
        return inner_output, None
    begin_index = lines.index(FALLBACK_BEGIN_SENTINEL)
    return "\n".join(lines[:begin_index]), "\n".join(lines[begin_index + 1 :])


@pure
def _read_fallback_run(fallback_stdout: str, fallback_stderr: str) -> ChatAppFallbackRun:
    output_lines = [line for line in fallback_stdout.splitlines() if not line.startswith(FALLBACK_EXIT_SENTINEL)]
    exit_lines = [line for line in fallback_stdout.splitlines() if line.startswith(FALLBACK_EXIT_SENTINEL)]
    exit_text = exit_lines[-1].removeprefix(FALLBACK_EXIT_SENTINEL).strip() if exit_lines else ""
    return ChatAppFallbackRun(
        stdout="\n".join(output_lines),
        stderr=fallback_stderr,
        exit_code=int(exit_text) if exit_text.isdigit() else None,
    )


@pure
def read_chat_app_answer(result: MngrCallResult) -> ChatAppAnswer:
    """What one ``mngr exec`` of a :func:`build_message_chat_command` shell answered.

    The script's last stderr line is its own reason when it ran; when it never did, the
    reason is mngr's ``exec_error`` event or the in-workspace refusal behind the outer
    mngr's chatter (:func:`exec_verdict_detail`).
    """
    script_stdout, fallback_stdout = _split_off_fallback_output(inner_stdout_from_exec_result(result.stdout))
    script_stderr, fallback_stderr = _split_off_fallback_output(inner_stderr_from_exec_result(result.stdout))
    exit_code = inner_exit_code(script_stdout)
    detail = _script_failure_detail(script_stderr) or exec_verdict_detail(result)
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
        fallback=None if fallback_stdout is None else _read_fallback_run(fallback_stdout, fallback_stderr or ""),
    )


def ask_chat_app(
    mngr_caller: MngrCaller,
    exec_agent_address: str,
    script_args: Sequence[str],
    *,
    fallback_command: str,
    timeout: float,
) -> ChatAppAnswer:
    """Run the messaging script with ``script_args`` inside ``exec_agent_address``'s workspace and read its answer.

    ``fallback_command`` runs in the same exec when the script gives no verdict.
    """
    command = build_message_chat_command(script_args, fallback_command)
    result = mngr_caller.call(build_chat_app_exec_args(exec_agent_address, command), timeout=timeout)
    return read_chat_app_answer(result)
