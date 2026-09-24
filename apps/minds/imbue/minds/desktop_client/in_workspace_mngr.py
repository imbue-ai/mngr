"""Build and read the commands the app runs *inside* a workspace through ``mngr exec``.

``mngr exec`` runs its COMMAND through a shell in the container, so each of
these is a single shell string, and each needs the same two things, whether the
program is the container's ``mngr`` or a template script that runs ``mngr``
itself (:mod:`.chat_app`).

**Tolerance.** A workspace's ``.mngr/settings.toml`` is versioned with its
template, but the mngr that parses it is installed separately, so the file can
name config that mngr does not know -- an ``[agent_types.<name>]`` section whose
fields belong to a plugin a release adds, say. Strict parsing (mngr's default)
turns that into a hard exit on *every* inner command, including the update that
would install the missing plugin, so the workspace wedges with no way out from
the app. The workspace's own system interface refuses to be strict for the same
reason (``agent_discovery._get_mngr_context``); the app must not be stricter
than the workspace it is trying to repair.

**A readable failure.** An inner command's verdict reaches the app wrapped in
the outer ``mngr exec``'s own ``Command failed on agent ...``, which says
nothing about why, so the diagnosis is the *first* verdict in the stream rather
than the last one :func:`mngr_failure_verdict` reads for un-nested commands.

**The inner output.** ``mngr exec --format jsonl`` reports one ``exec_result``
event per agent with the inner command's stdout and stderr as fields and only a
boolean ``success``. That format also moves the exec's own failures onto stdout, as
``exec_error`` events (:func:`exec_failure_reason`) -- only the human format writes
them to stderr, so a reader of a jsonl run that reaches for stderr finds discovery
chatter and no verdict.
"""

import json
import shlex
from collections.abc import Iterator
from collections.abc import Sequence
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.mngr_command import mngr_verdict_block
from imbue.minds.utils.mngr_caller import MngrCallResult

# Bare ``mngr`` resolves on the container's PATH (set up by ``mngr exec``'s
# source-env prefix); the desktop app's outer binary path does not exist there.
_CONTAINER_MNGR_BINARY: Final[str] = "mngr"

# The image's tool bin dir, ahead of whatever the login shell prepended: a
# workspace can carry a second, stale mngr under ``$HOME/.local/bin`` that no
# update refreshes, while every update refreshes the copy under /root.
_IMAGE_TOOL_BIN_DIR: Final[str] = "/root/.local/bin"
_PREFER_IMAGE_TOOLS: Final[str] = f"PATH={_IMAGE_TOOL_BIN_DIR}:$PATH"

# The outer ``mngr exec``'s own closing line, which names the agent and nothing else.
_OUTER_EXEC_VERDICT_PREFIX: Final[str] = "ERROR: Command failed on agent"

# An env-assignment prefix rather than a ``--setting``: it is read before any
# config is parsed, which is the failure being tolerated.
_TOLERATE_UNKNOWN_CONFIG: Final[str] = "MNGR_ALLOW_UNKNOWN_CONFIG=1"

# Room for a verdict and the hint mngr appends to it, in something the SPA
# renders -- not for a log dump.
FAILURE_DETAIL_MAX_CHARS: Final[int] = 1000


def build_in_workspace_command(argv: Sequence[str]) -> str:
    """The shell string that runs ``argv`` inside a workspace with the image's tools first and mngr's config tolerated.

    Shell-quoted, so a seed message or a format string cannot break out of its own
    argument. The tolerance rides as an environment assignment, so a script's own ``mngr``
    subprocess inherits it.
    """
    return f"{_PREFER_IMAGE_TOOLS} {_TOLERATE_UNKNOWN_CONFIG} {shlex.join(list(argv))}"


def build_in_workspace_mngr_command(argv: Sequence[str]) -> str:
    """The shell string that runs ``mngr <argv>`` inside a workspace.

    ``argv`` is the argument vector *after* the program name, which this adds:
    naming the binary is the caller's one way to bypass the tolerance above.
    """
    return build_in_workspace_command([_CONTAINER_MNGR_BINARY, *argv])


@pure
def jsonl_events(stdout: str) -> Iterator[dict[str, Any]]:
    """Every JSONL record on a ``--format jsonl`` stdout, in order, skipping whatever else is on the stream.

    mngr interleaves human-readable warnings on stdout, so only lines that look like a
    JSONL record are parsed (mirrors the ``mngr create`` event sniff in
    ``agent_creator._CreateEventCapture``).
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@pure
def _exec_result_field(stdout: str, field: str) -> str:
    return "".join(
        str(event[field]) for event in jsonl_events(stdout) if event.get("event") == "exec_result" and field in event
    )


@pure
def inner_stdout_from_exec_result(stdout: str) -> str:
    """The inner command's own stdout, unwrapped from ``mngr exec --format jsonl`` output.

    ``mngr exec`` does not relay the inner command's output as it arrives: it emits one
    ``{"event": "exec_result", "agent": ..., "stdout": ..., "stderr": ...}`` line per
    targeted agent, carrying the whole inner stdout as a string field. '' when the exec
    produced no result at all, e.g. it could not reach the workspace.
    """
    return _exec_result_field(stdout, "stdout")


@pure
def inner_stderr_from_exec_result(stdout: str) -> str:
    """The inner command's own stderr, unwrapped the same way; '' when the exec produced no result."""
    return _exec_result_field(stdout, "stderr")


@pure
def exec_failure_reason(stdout: str) -> str:
    """Why ``mngr exec --format jsonl`` never ran the command, in mngr's own words; '' when it ran it.

    A run that did not reach the command at all -- an agent the host does not have, a host
    that would not start -- is reported as a ``{"event": "exec_error", "agent": ...,
    "error": ...}`` line on stdout, and in this format nothing about it reaches stderr the
    way the human format's ``ERROR: Failed on agent ...`` line does. Without reading it the
    only text left is the outer mngr's discovery chatter, which reads like a cause and is
    not.
    """
    reasons = "; ".join(
        str(event["error"])
        for event in jsonl_events(stdout)
        if event.get("event") == "exec_error" and "error" in event
    )
    return reasons[:FAILURE_DETAIL_MAX_CHARS]


def in_workspace_failure_detail(stderr: str) -> str:
    """The inner command's own verdict from a failed in-workspace run's stderr.

    The *first* marker starts the block, unlike the un-nested case
    :func:`mngr_failure_verdict` reads: the last one here is the outer ``mngr
    exec``'s own ``Command failed on agent ...``. What comes before the first is
    the outer mngr's discovery chatter -- an unreachable host it skipped, a
    duplicate host name -- which reads as the cause but is not.

    When the wrapper's line is the only marker, the inner command gave no
    verdict at all, and the last thing it wrote before dying (a traceback's
    final line) is the diagnosis instead.
    """
    detail = mngr_verdict_block(stderr, is_first_verdict=True, max_chars=FAILURE_DETAIL_MAX_CHARS)
    if not detail.startswith(_OUTER_EXEC_VERDICT_PREFIX):
        return detail
    lines = stderr.strip().splitlines()
    wrapper_index = next(index for index, line in enumerate(lines) if line.startswith(_OUTER_EXEC_VERDICT_PREFIX))
    inner_lines = [line.strip() for line in lines[:wrapper_index] if line.strip() and not line.startswith("WARNING:")]
    return inner_lines[-1][:FAILURE_DETAIL_MAX_CHARS] if inner_lines else detail


@pure
def exec_verdict_detail(result: MngrCallResult) -> str:
    """The workspace's own account of an ``mngr exec --format jsonl`` run that gave no verdict; '' when it gave none.

    mngr's own ``exec_error`` event first, then the in-workspace refusal behind the outer
    mngr's chatter. Nothing else is read: when neither is there, the only text left is that
    chatter, or minds' own words about a run it never got an answer from, and both read like
    a cause without being one.
    """
    return exec_failure_reason(result.stdout) or (
        in_workspace_failure_detail(result.stderr) if result.is_mngr_output else ""
    )


@pure
def exec_log_detail(result: MngrCallResult) -> str:
    """Why an ``mngr exec`` run fell short of a verdict, for a log line; '' when nothing said why.

    The workspace's own account first (:func:`exec_verdict_detail`), then minds' own words
    about a run it got no answer from, which is a last resort for logs and no part of what a
    user is shown.
    """
    return exec_verdict_detail(result) or ("" if result.is_mngr_output else result.stderr.strip())
