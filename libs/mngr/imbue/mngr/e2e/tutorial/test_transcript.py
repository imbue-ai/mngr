"""Tests for ``mngr transcript`` variants from the tutorial."""

import json
from pathlib import Path

import pytest

from imbue.mngr.cli.testing import SAMPLE_TRANSCRIPT_EVENTS
from imbue.mngr.cli.testing import write_common_transcript_events
from imbue.mngr.cli.testing import create_agent_with_sample_transcript
from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


def _find_agent_dir(host_dir: Path, agent_name: str) -> Path:
    """Locate the on-disk state directory of the agent named ``agent_name``."""
    for data_path in host_dir.rglob("data.json"):
        try:
            data = json.loads(data_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("name") == agent_name:
            return data_path.parent
    raise AssertionError(f"Could not find on-disk directory for agent {agent_name!r} under {host_dir}")


def _seed_claude_transcript(host_dir: Path, agent_name: str) -> None:
    """Make ``agent_name`` look like a claude agent with a recorded transcript.

    ``mngr transcript`` only reads an agent's recorded events and requires the
    agent type to be one that produces a common transcript (e.g. ``claude``);
    it never launches the agent. Running a real claude agent in the test would
    be slow and flaky, so we instead relabel the cheap local command agent as a
    ``claude`` agent on disk and seed a deterministic common-transcript file.
    """
    agent_dir = _find_agent_dir(host_dir, agent_name)
    data_path = agent_dir / "data.json"
    data = json.loads(data_path.read_text())
    data["type"] = "claude"
    data_path.write_text(json.dumps(data, indent=2))
    events_dir = agent_dir / "events" / "claude" / "common_transcript"
    events_dir.mkdir(parents=True, exist_ok=True)
    write_common_transcript_events(events_dir, SAMPLE_TRANSCRIPT_EVENTS)


@pytest.mark.rsync
# `mngr transcript` only works for agent types that produce a common transcript
# (e.g. claude); a plain `--type command` agent has no conversation to show, so
# seed a claude agent with a sample transcript directly on the (local) host. The
# sample contains a "Hello" user message, a "World" assistant reply, and a tool
# result, matching imbue.mngr.cli.testing.SAMPLE_TRANSCRIPT_EVENTS. Only the
# `release` mark is used: the agent is local and `mngr transcript` resolves it
# without contacting modal/tmux/rsync, so the resource guards would reject those
# marks as never-invoked.
@pytest.mark.release
def test_transcript_default(e2e: E2eSession, temp_host_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # view the transcript of an agent's conversation
        mngr transcript my-task
    """)
    create_agent_with_sample_transcript(temp_host_dir, agent_name="my-task")
    result = e2e.run("mngr transcript my-task", comment="view the transcript")
    expect(result).to_succeed()
    # Verify the seeded conversation is actually rendered, not just that the
    # command exited 0: the human-readable output must show the user prompt,
    # the assistant reply, and the tool result.
    expect(result.stdout).to_contain("Hello")
    expect(result.stdout).to_contain("World")
    expect(result.stdout).to_contain("user:")
    expect(result.stdout).to_contain("assistant:")


# Note: no @pytest.mark.modal -- this test creates a local command agent with
# --no-connect and reads its (seeded) transcript locally, so it never invokes
# modal (unlike the connect-based create tests). The resource guard fails if a
# modal mark is present but modal is never exercised.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_transcript_assistant_only(e2e: E2eSession, temp_host_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # view only assistant messages
        mngr transcript my-task --role assistant
    """)
    _create_my_task(e2e, 100801)
    # mngr transcript only works for agent types that produce a common transcript
    # (e.g. claude), so relabel the agent and seed a deterministic transcript.
    _seed_claude_transcript(temp_host_dir, "my-task")
    result = e2e.run("mngr transcript my-task --role assistant", comment="view only assistant messages")
    expect(result).to_succeed()
    # The sample transcript has one user, one assistant, and one tool event;
    # filtering by --role assistant must show only the assistant message.
    assert "World" in result.stdout, f"expected the assistant message in output, got: {result.stdout!r}"
    assert "Hello" not in result.stdout, f"user message should be filtered out, got: {result.stdout!r}"
    assert "assistant:" in result.stdout, f"expected an assistant role label, got: {result.stdout!r}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_transcript_tail(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # view the last 5 messages
        mngr transcript my-task --tail 5
    """)
    _create_my_task(e2e, 100802)
    expect(e2e.run("mngr transcript my-task --tail 5", comment="view the last 5 messages")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_transcript_tail_one(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # quickly peek at an agent's most recent message without connecting (handy for sanity-checking many agents)
        mngr transcript my-task --tail 1
    """)
    _create_my_task(e2e, 100803)
    expect(e2e.run("mngr transcript my-task --tail 1", comment="quickly peek at most recent message")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_transcript_format_jsonl(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output transcript as JSONL for programmatic use
        mngr transcript my-task --format jsonl
    """)
    _create_my_task(e2e, 100804)
    expect(e2e.run("mngr transcript my-task --format jsonl", comment="output transcript as JSONL")).to_succeed()
