from __future__ import annotations

import re
import shutil
import warnings
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.installation import is_pinned_version_present
from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.primitives import BackgroundTask
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import read_background_task_records_in
from imbue.mngr_claude.dialogs import CLAUDE_CODE_VERSION
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import BACKGROUND_SLEEP_PROMPT
from imbue.mngr_claude.testing import BACKGROUND_SLEEP_SECONDS
from imbue.mngr_claude.testing import BACKGROUND_TASK_KIND_BY_CLAUDE_TASK_LABEL
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import held_tool_command
from imbue.mngr_claude.testing import read_claude_version_output
from imbue.mngr_claude.testing import wait_for_turn_to_end

_RECORD_CONSUMER: Final[str] = (
    "wait_for_stop_hook.sh's write_background_task_records, which records each entry of the Stop payload's "
    "background_tasks and normalises its `type` label to a BackgroundTaskKind"
)

_JS_NAME: Final[str] = r"[A-Za-z_$][\w$]*"
_JS_STRING_ENTRY: Final[str] = rf'{_JS_NAME}:"[^"\\]*"'
# A minified object literal of string values, e.g. `{local_bash:"shell",dream:"dream"}`.
_STRING_TABLE_PATTERN: Final[re.Pattern[bytes]] = re.compile(
    rf"\{{({_JS_STRING_ENTRY}(?:,{_JS_STRING_ENTRY})*)\}}".encode()
)
_STRING_ENTRY_PATTERN: Final[re.Pattern[bytes]] = re.compile(rf'({_JS_NAME}):"([^"\\]*)"'.encode())
# The name a table is assigned to, e.g. `Uuo=`, matched where the table starts.
_TABLE_NAME_PATTERN: Final[re.Pattern[bytes]] = re.compile(rf"({_JS_NAME})=\Z".encode())
_TABLE_NAME_WINDOW_BYTES: Final[int] = 64
# How far past a table its lookup is searched for.
_LOOKUP_WINDOW_BYTES: Final[int] = 512
_RETURNED_STRING_PATTERN: Final[re.Pattern[bytes]] = re.compile(rb'return"([^"\\]*)"')
# The task type claude registers a backgrounded command under.
_SHELL_TASK_TYPE: Final[bytes] = b"local_bash"


def _read_records_a_turn_leaves(tmp_path: Path, prompt: str) -> tuple[tuple[BackgroundTask, ...], str]:
    """Run ``prompt`` as a fresh agent's turn; return the records standing once it ends and a drift note for them."""
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-task-kind-{get_short_random_string()}"
    drift = drift_note(ctx, "the background_tasks entries of claude's Stop payload")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, prompt)
        wait_for_turn_to_end(profile, ctx, prompt, drift)
        records = read_background_task_records_in(agent_state_dir(ctx.host_dir) / BACKGROUND_TASK_DIR_NAME)
    return records, drift


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_records_a_backgrounded_command_as_shell_work(tmp_path: Path) -> None:
    """A turn that leaves a backgrounded Bash command running ends with one SHELL record of native type `shell`."""
    records, drift = _read_records_a_turn_leaves(tmp_path, BACKGROUND_SLEEP_PROMPT)

    assert [(record.kind, record.native_type, record.last_seen_at is not None) for record in records] == [
        (BackgroundTaskKind.SHELL, "shell", True)
    ], (
        f"the turn that backgrounded `sleep {BACKGROUND_SLEEP_SECONDS}` left the records {records} {drift}; "
        f"consumer: {_RECORD_CONSUMER}"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_records_a_backgrounded_subagent_as_subagent_work(tmp_path: Path) -> None:
    """A turn that leaves a backgrounded subagent running ends with one SUBAGENT record of native type `subagent`."""
    # Nothing creates the release path, so the command holds the subagent until the Bash tool times it out.
    held_command = held_tool_command(tmp_path / "subagent-started", tmp_path / "subagent-release")
    records, drift = _read_records_a_turn_leaves(
        tmp_path,
        "Use the Agent tool once, with subagent_type general-purpose, run_in_background set to true, and this "
        f"prompt: 'Use the Bash tool to run exactly: {held_command}. Then reply with just DONE.' "
        "Do not wait for the subagent or check on it, and then reply with just OK.",
    )

    assert [(record.kind, record.native_type, record.last_seen_at is not None) for record in records] == [
        (BackgroundTaskKind.SUBAGENT, "subagent", True)
    ], f"the turn that backgrounded a subagent left the records {records} {drift}; consumer: {_RECORD_CONSUMER}"


def _read_task_label_sets(program: Path) -> set[frozenset[str]]:
    """The labels of each task-type table ``program`` reads as ``Uuo[e]??e``, with the strings that lookup returns first."""
    code = program.read_bytes()
    label_sets: set[frozenset[str]] = set()
    for table in _STRING_TABLE_PATTERN.finditer(code):
        label_by_task_type = dict(_STRING_ENTRY_PATTERN.findall(table.group(1)))
        if _SHELL_TASK_TYPE not in label_by_task_type:
            continue
        table_name = _TABLE_NAME_PATTERN.search(code, max(0, table.start() - _TABLE_NAME_WINDOW_BYTES), table.start())
        if table_name is None:
            continue
        lookup = re.compile(
            rb"(?<![\w$.])" + re.escape(table_name.group(1)) + rf"\[({_JS_NAME})\]\?\?\1(?![\w$])".encode()
        ).search(code, table.end(), table.end() + _LOOKUP_WINDOW_BYTES)
        if lookup is None:
            continue
        labels = [*label_by_task_type.values(), *_RETURNED_STRING_PATTERN.findall(code, table.end(), lookup.start())]
        label_sets.add(frozenset(label.decode() for label in labels))
    return label_sets


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
def test_installed_claude_labels_background_tasks_only_as_mngr_knows(tmp_path: Path) -> None:
    """The installed claude gives a background task's `type` no label mngr's table lacks.

    The pinned claude gives every label in the table, and any other claude every label the Stop hook names.
    """
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Release test requires `claude` on PATH.")
    program = Path(claude).resolve()
    ctx = ClaudeReleaseProfile().setup(tmp_path)
    claude_version_output = read_claude_version_output(ctx)
    assert claude_version_output, (
        f"`claude --version` printed nothing for {program}, so the test cannot tell whether it is the pinned claude "
        f"({CLAUDE_CODE_VERSION})"
    )
    drift = drift_note(ctx, "claude's background task labels")

    label_sets = _read_task_label_sets(program)
    assert len(label_sets) == 1, (
        f"found {len(label_sets)} tables of background task labels in {program}, not one: "
        f"{sorted(sorted(labels) for labels in label_sets)} {drift}; the table is the object literal keyed by task "
        f"type ({_SHELL_TASK_TYPE.decode()} and its siblings) that its lookup reads with a fallback to the raw type; "
        f"consumer: {_RECORD_CONSUMER}"
    )
    (installed_labels,) = label_sets
    kind_by_label = BACKGROUND_TASK_KIND_BY_CLAUDE_TASK_LABEL
    known_labels = frozenset(kind_by_label)

    unlisted_labels = sorted(installed_labels - known_labels)
    absent_labels = sorted(known_labels - installed_labels)
    assert not unlisted_labels, (
        f"the installed claude gives background tasks the labels {unlisted_labels}, which mngr does not list, and "
        f"does not give {absent_labels}, which mngr lists {drift}; decide the BackgroundTaskKind of each new label, "
        "then add it to BACKGROUND_TASK_KIND_BY_CLAUDE_TASK_LABEL in imbue/mngr_claude/testing.py, to "
        "specs/agent-running-background/spec.md and, unless that kind is OTHER, to the jq function record_kind in "
        f"wait_for_stop_hook.sh; a label the pinned claude ({CLAUDE_CODE_VERSION}) does not give can be listed only "
        "once the pin reaches a claude that gives it"
    )

    if is_pinned_version_present(claude_version_output, CLAUDE_CODE_VERSION):
        assert not absent_labels, (
            f"mngr lists the background task labels {absent_labels}, which the claude it pins does not give {drift}; "
            "remove them from BACKGROUND_TASK_KIND_BY_CLAUDE_TASK_LABEL in imbue/mngr_claude/testing.py, from the jq "
            "function record_kind in wait_for_stop_hook.sh where it names them, and from "
            "specs/agent-running-background/spec.md"
        )
    else:
        absent_named_labels = [
            label for label in absent_labels if kind_by_label[label] is not BackgroundTaskKind.OTHER
        ]
        assert not absent_named_labels, (
            f"the installed claude does not give the background task labels {absent_named_labels}, which the jq "
            f"function record_kind in wait_for_stop_hook.sh names {drift}; a task claude reports under any other "
            "label is recorded as OTHER"
        )
        if absent_labels:
            warnings.warn(
                f"claude {claude_version_output} does not give the background task labels {absent_labels} that mngr "
                "lists; the Stop hook records each as OTHER whether or not mngr lists it, so nothing is due until "
                f"mngr pins a claude that does not give them (it pins {CLAUDE_CODE_VERSION}), when this test fails "
                "on that claude and says where to remove them",
                stacklevel=1,
            )
