"""Tests for ``mngr transcript`` variants from the tutorial."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_transcript_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # view the transcript of an agent's conversation
        mngr transcript my-task
    """)
    _create_my_task(e2e, 100800)
    expect(e2e.run("mngr transcript my-task", comment="view the transcript")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_transcript_assistant_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # view only assistant messages
        mngr transcript my-task --role assistant
    """)
    _create_my_task(e2e, 100801)
    expect(e2e.run("mngr transcript my-task --role assistant", comment="view only assistant messages")).to_succeed()


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
