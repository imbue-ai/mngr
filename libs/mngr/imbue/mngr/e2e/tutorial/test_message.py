"""Tests for ``mngr message`` variants from the tutorial.

Each test corresponds 1:1 to a tutorial script block. Where the block addresses
fictional agent names (agent-1, agent-2, ...), the test creates real agents
with those names first so the message command has somewhere to land.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_sleep_agents(
    e2e: E2eSession, names_and_sleeps: list[tuple[str, int]], timeout: float = 30.0
) -> None:
    for name, sleep_seconds in names_and_sleeps:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"create {name}",
                timeout=timeout,
            )
        ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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
# A bare positional name with no provider component cannot be resolved from the
# discovery event stream (the name matches nothing), so discovery falls back to
# a full scan across every enabled provider (local, Docker, Modal, ...) to prove
# the name exists nowhere. That scan is inherently slow -- it contacts remote
# providers -- and reliably exceeds both the default 10s func-only pytest
# timeout and the default 30s e2e.run subprocess timeout. The happy-path message
# tests are fast only because the event stream resolves their (existing) names
# to the local provider and skips the remote scan. Give this no-op path the same
# generous headroom the other multi-provider message tests use, and raise the
# subprocess timeout above the func-only mark so a genuine hang surfaces as the
# cleaner e2e.run timeout rather than a thread kill.
@pytest.mark.timeout(120)
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
        timeout=90.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found to send message to")
    expect(result.stdout).not_to_contain("Message sent to:")


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
    # Guard against the no-op path: a build that failed to resolve the named
    # agent would print this instead while still exiting 0.
    expect(result.stdout).not_to_contain("No agents found to send message to")


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


@pytest.mark.release
@pytest.mark.tmux
# The broadcast targets a single local command agent, so message delivery writes
# to a local mailbox and never crosses a host boundary; rsync (which requires one
# local and one remote endpoint) is therefore not invoked, so this test carries
# no @pytest.mark.rsync -- the resource guard would flag it as a superfluous mark.
#
# The broadcast command chains two mngr CLI invocations (`mngr list` piped into
# `mngr msg`). Each invocation pays the full CLI startup cost, so the combined
# wall-clock time can exceed the default 30s per-command timeout on slower
# filesystems (the same reasoning as test_message_filtered_via_stdin). Give the
# create step and the pipeline generous per-command headroom, and raise the
# overall test timeout to cover both.
@pytest.mark.timeout(240)
def test_message_all(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # send a message to every agent by piping their ids from `mngr list`
        mngr list --ids | mngr msg - -m "Stop what you are doing and commit your current progress"
    """)
    _create_sleep_agents(e2e, [("my-task", 100305)], timeout=90.0)
    result = e2e.run(
        'mngr list --ids | mngr msg - -m "Stop what you are doing and commit your current progress"',
        comment="send a message to every agent",
        timeout=90.0,
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
    #
    # `--provider local` scopes discovery to the local backend so this standalone
    # verification does not fan out to remote backends (e.g. docker/modal) that
    # may be unreachable in the test environment -- an unreachable enabled
    # provider makes `mngr list` exit non-zero even when the reachable providers
    # listed fine, which would fail `to_succeed()` here for reasons unrelated to
    # the filter under test. The `--include` filter is kept so this mirrors the
    # exact CEL filter the piped command below feeds into `mngr msg`.
    list_result = e2e.run(
        "mngr list --provider local --include 'host.provider == \"local\"' --ids",
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


@pytest.mark.release
@pytest.mark.tmux
# Creates sleep agents, then chains `mngr list --ids` into `mngr msg -`. Each
# step pays the full CLI startup cost, so the combined wall-clock time exceeds
# the default 10s func-only timeout. Give it the same headroom as the other
# agent-creating message tests.
#
# Only `tmux` is marked: message delivery injects into each agent's tmux
# session (the sole guarded resource invoked here). There is no `rsync` mark
# because delivery never rsyncs, and no `modal` mark because the test env has
# no Modal agents, so an unfiltered `mngr list` never contacts Modal. A mark
# whose resource is never invoked trips the resource guard and fails the test.
@pytest.mark.timeout(180)
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
    # The tutorial block is about messaging *multiple* agents, so create two of
    # them: `--on-error continue` only has observable meaning when more than one
    # recipient is in play.
    _create_sleep_agents(e2e, [("error-continue-1", 100306), ("error-continue-2", 100309)])
    result = e2e.run(
        'mngr list --ids | mngr msg - -m "Status update please" --on-error continue',
        comment="control error handling when messaging multiple agents",
    )
    expect(result).to_succeed()
    # With `--on-error continue` and two healthy agents, the message must land on
    # both of them and the command must report a clean delivery.
    expect(result.stdout).to_contain("Message sent to: error-continue-1")
    expect(result.stdout).to_contain("Message sent to: error-continue-2")
    expect(result.stdout).to_contain("Successfully sent message")
    expect(result.stdout).not_to_contain("No agents found to send message to")
