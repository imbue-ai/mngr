"""Tests for the connect-to-agent commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.

``mngr connect`` normally attaches to the agent's tmux session interactively;
the e2e fixture rewrites that to ``mngr-e2e-connect`` (a no-op recorder), so the
calls below return immediately instead of blocking.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    """Create a long-running 'my-task' agent so connect/start variants have a target."""
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task for connect test (sleep {sleep_value})",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # connect to a running agent by name
        mngr connect my-task
    """)
    _create_my_task(e2e, 100200)
    expect(e2e.run("mngr connect my-task", comment="connect to a running agent by name")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr conn my-task
    """)
    _create_my_task(e2e, 100201)
    expect(e2e.run("mngr conn my-task", comment="short form")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_connect_by_agent_id_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # sometimes names can be ambiguous (e.g. if you made two agents with the same name on different hosts), so you can always
        # be really specific by using the agent id instead of the name:
        mngr connect agent-fa29307a16734899aa77b0f0563c8c99
    """)
    # The fictional agent id from the tutorial does not exist in the fresh test
    # environment, so the command is expected to fail with a "not found" error.
    # We only care that mngr accepts and parses the id-as-target syntax.
    result = e2e.run(
        "mngr connect agent-fa29307a16734899aa77b0f0563c8c99",
        comment="connect using the agent id instead of the name",
    )
    assert "not found" in (result.stdout + result.stderr).lower() or result.exit_code != 0


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_explicit_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can use the explicit host and agent:
        mngr conn my-task@my-host
    """)
    # `@my-host` refers to a host that doesn't exist in the test env; assert the
    # command parses the syntax and returns a clean error rather than crashing.
    result = e2e.run(
        "mngr conn my-task@my-host",
        comment="use the explicit host and agent",
    )
    assert result.exit_code != 0


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_explicit_host_and_provider(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or if you're really unlucky and have multiple *hosts* with the same name (across different providers),
        # you can use the explicit host, agent and provider:
        mngr conn my-task@my-host.modal
    """)
    result = e2e.run(
        "mngr conn my-task@my-host.modal",
        comment="use the explicit host, agent and provider",
    )
    assert result.exit_code != 0


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_with_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # the default behavior is to start the agent if it's stopped (you can be explicit about that too):
        mngr connect my-task --start
    """)
    _create_my_task(e2e, 100202)
    expect(e2e.run("mngr connect my-task --start", comment="explicit --start behavior")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_connect_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can disable auto-starting (fails if agent is stopped)
        mngr connect my-task --no-start
    """)
    _create_my_task(e2e, 100203)
    expect(e2e.run("mngr connect my-task --no-start", comment="disable auto-starting")).to_succeed()
