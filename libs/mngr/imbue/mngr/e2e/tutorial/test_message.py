"""Tests for ``mngr message`` variants from the tutorial.

Each test corresponds 1:1 to a tutorial script block. Where the block addresses
fictional agent names (agent-1, agent-2, ...), the test creates real agents
with those names first so the message command has somewhere to land.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_sleep_agents(e2e: E2eSession, names_and_sleeps: list[tuple[str, int]]) -> None:
    for name, sleep_seconds in names_and_sleeps:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"create {name}",
            )
        ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_one_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to a specific agent
        mngr message my-task -m "Please also add unit tests for the new function"
    """)
    _create_sleep_agents(e2e, [("my-task", 100300)])
    expect(
        e2e.run(
            'mngr message my-task -m "Please also add unit tests for the new function"',
            comment="send a message to a specific agent",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr msg my-task -m "Check the CI results and fix any failures"
    """)
    _create_sleep_agents(e2e, [("my-task", 100301)])
    expect(
        e2e.run(
            'mngr msg my-task -m "Check the CI results and fix any failures"',
            comment="short form",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_multiple_agents_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send the same message to multiple agents by name
        mngr msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"
    """)
    _create_sleep_agents(e2e, [("agent-1", 100302), ("agent-2", 100303), ("agent-3", 100304)])
    expect(
        e2e.run(
            'mngr msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"',
            comment="send the same message to multiple agents by name",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_all(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to all agents
        mngr msg -a -m "Stop what you are doing and commit your current progress"
    """)
    _create_sleep_agents(e2e, [("my-task", 100305)])
    expect(
        e2e.run(
            'mngr msg -a -m "Stop what you are doing and commit your current progress"',
            comment="send a message to all agents",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_filtered_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to agents matching a filter
        mngr list --include 'host.provider == "modal"' --ids | mngr msg - -m "Almost out of budget, please finish up"
    """)
    # No modal agents exist in the test env, so the filtered id list is empty
    # and the message becomes a no-op. We assert the pipeline succeeds end to
    # end -- that's the contract the tutorial is illustrating.
    expect(
        e2e.run(
            'mngr list --include \'host.provider == "modal"\' --ids | mngr msg - -m "Almost out of budget, please finish up"',
            comment="send a message to agents matching a filter",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_on_error_continue(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control error handling when messaging multiple agents
        # your choices are:
        #   "continue", which means try all agents once, or
        #   "abort", which means stop if any agent fails to receive the message
        # note that "abort" is kind of dangerous--you could easily have agents left in a strange state
        # thus the default is "continue"
        mngr msg -a -m "Status update please" --on-error continue
    """)
    _create_sleep_agents(e2e, [("my-task", 100306)])
    expect(
        e2e.run(
            'mngr msg -a -m "Status update please" --on-error continue',
            comment="control error handling when messaging multiple agents",
        )
    ).to_succeed()
