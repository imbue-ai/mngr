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
def test_message_one_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to a specific agent
        mngr message my-task -m "Please also add unit tests for the new function"
    """)
    _create_sleep_agents(e2e, [("my-task", 100300)])
    result = e2e.run(
        'mngr message my-task -m "Please also add unit tests for the new function"',
        comment="send a message to a specific agent",
    )
    expect(result).to_succeed()
    # The message must actually land on the named agent: the command reports the
    # specific recipient and a count of exactly one successful delivery.
    expect(result.stdout).to_contain("Message sent to: my-task")
    expect(result.stdout).to_contain("Successfully sent message to 1 agent(s)")
    expect(result.stdout).not_to_contain("No agents found to send message to")


@pytest.mark.release
def test_message_nonexistent_agent(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: messaging an agent that does not
    # exist is a no-op, not an error. A positional name becomes a name/id filter
    # that matches nothing, so no delivery happens and the command still exits 0.
    e2e.write_tutorial_block("""
        # send a message to a specific agent
        mngr message my-task -m "Please also add unit tests for the new function"
    """)
    result = e2e.run(
        'mngr message no-such-agent -m "Please also add unit tests for the new function"',
        comment="send a message to a specific agent",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found to send message to")
    expect(result.stdout).not_to_contain("Message sent to:")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_message_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr msg my-task -m "Check the CI results and fix any failures"
    """)
    _create_sleep_agents(e2e, [("my-task", 100301)])
    result = e2e.run(
        'mngr msg my-task -m "Check the CI results and fix any failures"',
        comment="short form",
    )
    expect(result).to_succeed()
    # Verify the message was actually delivered to the named agent. Messaging
    # zero agents also exits 0 ("No agents found to send message to"), so the
    # exit code alone does not prove the agent was found and reached.
    expect(result.stdout).to_contain("Message sent to: my-task")
    expect(result.stdout).to_contain("Successfully sent message to 1 agent(s)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_message_multiple_agents_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send the same message to multiple agents by name
        mngr msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"
    """)
    _create_sleep_agents(e2e, [("agent-1", 100302), ("agent-2", 100303), ("agent-3", 100304)])
    result = e2e.run(
        'mngr msg agent-1 agent-2 agent-3 -m "Wrap up and commit your changes"',
        comment="send the same message to multiple agents by name",
    )
    expect(result).to_succeed()
    # Verify all three named agents were actually reached, not just that the
    # command exited 0. A bug that parsed only the first name or silently
    # dropped a target would still exit 0, so assert on the per-agent delivery
    # lines and the aggregate count.
    expect(result.stdout).to_contain("Message sent to: agent-1")
    expect(result.stdout).to_contain("Message sent to: agent-2")
    expect(result.stdout).to_contain("Message sent to: agent-3")
    expect(result.stdout).to_contain("Successfully sent message to 3 agent(s)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_message_all(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to every agent by piping their ids from `mngr list`
        mngr list --ids | mngr msg - -m "Stop what you are doing and commit your current progress"
    """)
    _create_sleep_agents(e2e, [("my-task", 100305)])
    result = e2e.run(
        'mngr list --ids | mngr msg - -m "Stop what you are doing and commit your current progress"',
        comment="send a message to every agent",
    )
    expect(result).to_succeed()
    # The pipe must actually broadcast to the running agent, not silently no-op.
    expect(result.stdout).to_contain("Message sent to: my-task")
    expect(result.stdout).to_contain("Successfully sent message to 1 agent(s)")


@pytest.mark.release
# Unlike the other message tests, this one filters on a provider that has no
# agents in the test env, so the piped id list is empty and `mngr msg -` is a
# pure no-op: it never attaches a tmux session, contacts Modal, or rsyncs. The
# rsync/tmux/modal resource marks would therefore trip the resource guard
# (mark present but resource never invoked), so they are intentionally omitted.
#
# The command chains two mngr CLI invocations (`mngr list` piped into `mngr
# msg`). Each invocation pays the full CLI startup cost, so the combined
# wall-clock time can exceed the default 10s func-only timeout on slower
# filesystems. Give the pipeline generous headroom.
@pytest.mark.timeout(90)
def test_message_filtered_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to agents matching a filter
        mngr list --include 'host.provider == "modal"' --ids | mngr msg - -m "Almost out of budget, please finish up"
    """)
    # No modal agents exist in the test env, so the filtered id list is empty
    # and the message becomes a no-op. First confirm the filter half really does
    # produce an empty id list -- otherwise the no-op path would not be the one
    # under test.
    list_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids",
        comment="the filter matches no agents, so the id list is empty",
        timeout=60.0,
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout.strip()).to_be_empty()

    # Piping the empty list into msg must exit cleanly without claiming that any
    # message was delivered.
    pipe_result = e2e.run(
        'mngr list --include \'host.provider == "modal"\' --ids | mngr msg - -m "Almost out of budget, please finish up"',
        comment="send a message to agents matching a filter",
        timeout=60.0,
    )
    expect(pipe_result).to_succeed()
    expect(pipe_result.stdout).not_to_contain("Successfully sent message")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Happy-path counterpart to test_message_filtered_via_stdin: here the filter
# matches real agents, so their ids are piped into `mngr msg -` and the message
# is actually delivered. The tutorial block uses a modal filter as its example,
# but creating modal agents is slow, so this test filters on the local provider
# instead -- the stdin-piping mechanism being illustrated is identical.
@pytest.mark.timeout(180)
def test_message_filtered_via_stdin_delivers_to_matching_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to agents matching a filter
        mngr list --include 'host.provider == "modal"' --ids | mngr msg - -m "Almost out of budget, please finish up"
    """)
    _create_sleep_agents(e2e, [("filter-target-1", 100307), ("filter-target-2", 100308)])

    # The filter half must list exactly the two agents we just created (they run
    # on the local provider). `--ids` emits internal agent ids (one per line),
    # not the human names, so assert on the count of matched ids here and defer
    # the name-level check to the delivery output below.
    list_result = e2e.run(
        "mngr list --include 'host.provider == \"local\"' --ids",
        comment="list the ids of agents matching the filter",
        timeout=60.0,
    )
    expect(list_result).to_succeed()
    matched_ids = [line for line in list_result.stdout.splitlines() if line.strip()]
    assert len(matched_ids) == 2, f"expected the filter to match the 2 local agents, got: {matched_ids!r}"

    # Piping those ids into msg must actually deliver the message to both agents.
    pipe_result = e2e.run(
        'mngr list --include \'host.provider == "local"\' --ids | mngr msg - -m "Almost out of budget, please finish up"',
        comment="send a message to agents matching a filter",
        timeout=60.0,
    )
    expect(pipe_result).to_succeed()
    expect(pipe_result.stdout).to_contain("Message sent to: filter-target-1")
    expect(pipe_result.stdout).to_contain("Message sent to: filter-target-2")
    expect(pipe_result.stdout).to_contain("Successfully sent message")


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
        mngr list --ids | mngr msg - -m "Status update please" --on-error continue
    """)
    _create_sleep_agents(e2e, [("my-task", 100306)])
    expect(
        e2e.run(
            'mngr list --ids | mngr msg - -m "Status update please" --on-error continue',
            comment="control error handling when messaging multiple agents",
        )
    ).to_succeed()
