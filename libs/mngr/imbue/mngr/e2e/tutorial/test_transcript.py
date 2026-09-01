"""Tests for ``mngr transcript`` variants from the tutorial.

``mngr transcript`` only works for agent types that produce a *common
transcript* (e.g. claude); it fails fast on a ``command`` agent, which has
none. Each test therefore creates a real (local) claude agent with an initial
message and waits until the agent has produced at least one agent reply
before exercising the transcript command.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.mngr.utils.polling import poll_until
from imbue.skitwright.expect import expect

# The initial message is intentionally trivial so the agent completes its first
# turn quickly; the exact reply does not matter, only that a transcript exists.
_INITIAL_MESSAGE = "Reply with just the single word: hello"

# Creating a real claude agent and having it process the initial message takes
# noticeably longer than spinning up a sleep command agent, so give create a
# generous budget.
_AGENT_READY_TIMEOUT = 300
_CREATE_TIMEOUT = 300.0
# Once `mngr create --message` returns, the message has been delivered but the
# agent may still be mid-turn; poll until the first agent reply lands.
_TRANSCRIPT_POLL_TIMEOUT = 180.0
_TRANSCRIPT_POLL_INTERVAL = 5.0


def _create_my_task(e2e: E2eSession) -> None:
    """Create a local claude agent and wait until it has produced a transcript.

    Returns once the agent has emitted at least one agent step into its common
    transcript, so that every ``mngr transcript`` variant (including
    ``--role agent``) has real content to display.
    """
    result = e2e.run(
        f"MNGR__AGENT_READY_TIMEOUT={_AGENT_READY_TIMEOUT} mngr create my-task --type claude "
        "--no-connect --yes --pass-env ANTHROPIC_API_KEY "
        f'--message "{_INITIAL_MESSAGE}" --no-ensure-clean',
        comment="create a claude agent with an initial message",
        timeout=_CREATE_TIMEOUT,
    )
    if result.exit_code != 0:
        diagnostics = e2e.collect_remote_diagnostics("my-task")
        raise AssertionError(
            f"Expected agent creation to succeed but got exit code {result.exit_code}\n"
            f"  Command: {result.command}\n"
            f"  Stderr:\n    {result.stderr}\n"
            f"{diagnostics}"
        )

    def _transcript_has_agent_reply() -> bool:
        probe = e2e.run(
            "mngr transcript my-task --role agent --format jsonl",
            comment="wait for the agent's first reply",
            timeout=60.0,
        )
        return probe.exit_code == 0 and probe.stdout.strip() != ""

    if not poll_until(
        condition=_transcript_has_agent_reply,
        timeout=_TRANSCRIPT_POLL_TIMEOUT,
        poll_interval=_TRANSCRIPT_POLL_INTERVAL,
    ):
        diagnostics = e2e.collect_remote_diagnostics("my-task")
        raise AssertionError(
            f"Agent 'my-task' did not produce an agent transcript step "
            f"within {_TRANSCRIPT_POLL_TIMEOUT}s.\n{diagnostics}"
        )


# A realistic common transcript: the stream header, one user step, one agent step,
# and its observation. Mirrors the ATIF-shaped records the claude common_transcript
# converter emits (see imbue/mngr/cli/testing.py::SAMPLE_ATIF_STREAM_EVENTS). The
# text fields are distinctive so role filtering can be verified by substring matching.
_SAMPLE_TRANSCRIPT_EVENTS: list[dict[str, Any]] = [
    {
        "type": "header",
        "event_id": "header-" + "0" * 32,
        "emitter": "claude/common_transcript",
        "schema_version": "ATIF-v1.7",
    },
    {
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "step",
        "event_id": "evt-user-1",
        "emitter": "claude/common_transcript",
        "source": "user",
        "message": "USER_MESSAGE_MARKER please help",
    },
    {
        "timestamp": "2026-01-01T00:00:01Z",
        "type": "step",
        "event_id": "evt-agent-1",
        "emitter": "claude/common_transcript",
        "source": "agent",
        "message": "AGENT_MESSAGE_MARKER on it",
        "model_name": "test-model",
    },
    {
        "timestamp": "2026-01-01T00:00:02Z",
        "type": "observation",
        "event_id": "evt-tool-1",
        "emitter": "claude/common_transcript",
        "results": [
            {
                "source_call_id": "call_1",
                "content": "TOOL_RESULT_MARKER ok",
                "extra": {"is_error": False, "tool_name": "Bash"},
            }
        ],
    },
]


def _create_claude_task_with_transcript(e2e: E2eSession, host_dir: Path, sleep_value: int) -> None:
    """Create a transcript-capable ``my-task`` agent and seed a common transcript.

    ``mngr transcript`` only supports agent types that emit a common transcript
    (i.e. implement ``HasCommonTranscriptMixin`` -- e.g. ``claude``), so a plain
    ``command`` agent is rejected by the command's up-front type check. Creating
    a real ``claude`` agent in the e2e environment is not viable here: it
    requires the Claude Code trust dialog to have been accepted for the source
    directory (and, for a live run, the claude binary). Instead we create a
    reliable ``command`` agent and relabel its recorded ``type`` to ``claude``.
    The transcript code path only reads ``type`` from ``data.json`` during
    discovery (it never instantiates the claude agent class), so this faithfully
    exercises ``mngr transcript`` while avoiding the trust/binary dependencies.

    A representative common_transcript events file (header, user step, agent
    step, observation) is then written into the agent's events directory so the
    role-filtering behavior can be verified.
    """
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()

    # The agent's state lives at $MNGR_HOST_DIR/agents/<agent_id>/. Exactly one
    # agent exists in this isolated host dir, so glob for it.
    agent_dirs = [d for d in (host_dir / "agents").glob("*") if (d / "data.json").exists()]
    assert len(agent_dirs) == 1, f"expected exactly one agent directory, found: {agent_dirs}"
    agent_dir = agent_dirs[0]

    # Relabel the agent type to ``claude`` so it advertises common-transcript
    # support; all other (real) data.json fields are left untouched.
    data_path = agent_dir / "data.json"
    data = json.loads(data_path.read_text())
    data["type"] = "claude"
    data_path.write_text(json.dumps(data))

    # Seed a common_transcript events file at the path the converter would use:
    # events/<agent_type>/common_transcript/events.jsonl.
    events_dir = agent_dir / "events" / "claude" / "common_transcript"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in _SAMPLE_TRANSCRIPT_EVENTS) + "\n"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(600)
def test_transcript_default(e2e: E2eSession) -> None:
    """Tutorial block:
        # view the transcript of an agent's conversation
        mngr transcript my-task

    Scope: `mngr transcript my-task` renders the agent's conversation as
    human-readable text, showing both the user message we sent (`user:` plus the
    initial message text) and at least one agent reply (`agent:`).
    """
    _create_my_task(e2e)
    result = e2e.run("mngr transcript my-task", comment="view the transcript")
    expect(result).to_succeed()
    # The human-readable transcript must show the conversation we started: both
    # the user message we sent and at least one agent reply.
    assert result.stdout.strip() != "", "Expected a non-empty transcript"
    assert "user:" in result.stdout, f"Expected a user message in the transcript, got:\n{result.stdout}"
    assert "agent:" in result.stdout, f"Expected an agent message in the transcript, got:\n{result.stdout}"
    assert _INITIAL_MESSAGE in result.stdout, (
        f"Expected the initial message text in the transcript, got:\n{result.stdout}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(600)
def test_transcript_agent_only(e2e: E2eSession, temp_host_dir: Path) -> None:
    """Tutorial block:
        # view only the agent's own messages
        mngr transcript my-task --role agent

    Scope: `--role agent` filters the transcript to the agent's own steps only,
    showing the agent message while dropping both the user message and the tool
    observation.
    """
    _create_claude_task_with_transcript(e2e, temp_host_dir, 100801)
    result = e2e.run(
        "mngr transcript my-task --role agent",
        comment="view only the agent's own messages",
    )
    expect(result).to_succeed()
    # --role agent must show the agent message and filter out the user message
    # and the tool observation.
    assert "AGENT_MESSAGE_MARKER on it" in result.stdout, result.stdout
    assert "USER_MESSAGE_MARKER" not in result.stdout, result.stdout
    assert "TOOL_RESULT_MARKER" not in result.stdout, result.stdout


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(600)
def test_transcript_tail(e2e: E2eSession) -> None:
    """Tutorial block:
        # view the last 5 messages
        mngr transcript my-task --tail 5

    Scope: `--tail 5` caps the transcript to at most the last 5 events (verified
    against the JSONL output, which yields between 1 and 5 events).
    """
    _create_my_task(e2e)
    result = e2e.run("mngr transcript my-task --tail 5", comment="view the last 5 messages")
    expect(result).to_succeed()
    # --tail shows at most N events; verify the cap holds against the full JSONL.
    tail_events = e2e.run("mngr transcript my-task --tail 5 --format jsonl", comment="tail as JSONL")
    expect(tail_events).to_succeed()
    tail_lines = [line for line in tail_events.stdout.splitlines() if line.strip()]
    assert 0 < len(tail_lines) <= 5, f"Expected between 1 and 5 tailed events, got {len(tail_lines)}"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(600)
def test_transcript_tail_one(e2e: E2eSession) -> None:
    """Tutorial block:
        # quickly peek at an agent's most recent message without connecting (handy for sanity-checking many agents)
        mngr transcript my-task --tail 1

    Scope: `--tail 1` shows exactly the single most recent event (verified
    against the JSONL output, which yields exactly 1 event).
    """
    _create_my_task(e2e)
    result = e2e.run("mngr transcript my-task --tail 1", comment="quickly peek at most recent message")
    expect(result).to_succeed()
    # --tail 1 shows exactly the single most recent event.
    tail_events = e2e.run("mngr transcript my-task --tail 1 --format jsonl", comment="tail one as JSONL")
    expect(tail_events).to_succeed()
    tail_lines = [line for line in tail_events.stdout.splitlines() if line.strip()]
    assert len(tail_lines) == 1, f"Expected exactly 1 tailed event, got {len(tail_lines)}"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(600)
def test_transcript_format_jsonl(e2e: E2eSession) -> None:
    """Tutorial block:
        # output transcript as JSONL for programmatic use
        mngr transcript my-task --format jsonl

    Scope: `--format jsonl` emits the transcript as JSONL, where every non-blank
    line is a standalone JSON object (a dict) suitable for programmatic use.
    """
    _create_my_task(e2e)
    result = e2e.run("mngr transcript my-task --format jsonl", comment="output transcript as JSONL")
    expect(result).to_succeed()
    # Every non-blank line must be a standalone JSON object (true JSONL).
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "Expected at least one JSONL event"
    for line in lines:
        event = json.loads(line)
        assert isinstance(event, dict), f"Expected each JSONL line to be a JSON object, got: {line!r}"
