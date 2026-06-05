"""Tests for the connect-to-agent commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.

``mngr connect`` execs ``tmux attach`` for a local agent, so it requires a real
terminal and blocks until the client detaches. Unlike ``create``/``start`` (whose
``connect_command`` the fixture rewrites to a no-op recorder), the standalone
``connect`` command does the real attach and cannot run under the plain
pipe-based ``e2e.run`` -- it would abort with "open terminal failed: not a
terminal". The happy-path tests therefore use ``e2e.run_connect_interactively``,
which wires the command to a PTY, waits for the client to attach, and detaches it
from outside so the command exits cleanly. The unhappy-path tests (bad id/host)
fail before reaching the attach, so they use the plain ``e2e.run``.
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


# No @pytest.mark.modal: connecting to a freshly-created *local* agent by name
# resolves via the discovery event-stream optimization to the local provider
# only, so modal is never queried (the resource guard enforces this).
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_connect_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # connect to a running agent by name
        mngr connect my-task
    """)
    _create_my_task(e2e, 100200)
    result = e2e.run_connect_interactively(
        "mngr connect my-task",
        agent_name="my-task",
        comment="connect to a running agent by name",
    )
    expect(result).to_succeed()
    # The connect command resolves the name and attaches to *that* agent's
    # session before the helper detaches it; verify it targeted my-task.
    expect(result.stdout).to_contain("Connecting to agent: my-task")


# No @pytest.mark.modal: see test_connect_by_name (local-only resolution).
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_connect_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr conn my-task
    """)
    _create_my_task(e2e, 100201)
    result = e2e.run_connect_interactively("mngr conn my-task", agent_name="my-task", comment="short form")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Connecting to agent: my-task")


@pytest.mark.release
def test_connect_by_agent_id_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # sometimes names can be ambiguous (e.g. if you made two agents with the same name on different hosts), so you can always
        # be really specific by using the agent id instead of the name:
        mngr connect agent-fa29307a16734899aa77b0f0563c8c99
    """)
    # The fictional agent id from the tutorial does not exist in the fresh test
    # environment, so the command is expected to fail with a "not found" error.
    # We only care that mngr accepts and parses the id-as-target syntax.
    agent_id = "agent-fa29307a16734899aa77b0f0563c8c99"
    result = e2e.run(
        f"mngr connect {agent_id}",
        comment="connect using the agent id instead of the name",
    )
    # mngr must reject this as a missing agent, not as malformed input: a
    # non-zero exit plus a "not found" error that names the exact id we passed
    # proves the id was parsed and used as the lookup target (rather than, e.g.,
    # being treated as a host or a syntax error).
    combined_output = (result.stdout + result.stderr).lower()
    assert result.exit_code != 0, f"expected non-zero exit, got {result.exit_code}"
    assert "not found" in combined_output, combined_output
    assert agent_id in combined_output, combined_output


@pytest.mark.release
def test_connect_explicit_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can use the explicit host and agent:
        mngr conn my-task@my-host
    """)
    # `@my-host` refers to a host that doesn't exist in the test env; assert the
    # command parses the syntax and returns a clean error rather than crashing.
    # Resolution looks for a host named "my-host", finds none, and exits before
    # ever attaching a tmux session or running rsync. No Modal hosts exist in the
    # fresh test env either, so the Modal provider short-circuits without a real
    # Modal call -- hence this unhappy path exercises no guarded resource and
    # carries only the `release` mark.
    result = e2e.run(
        "mngr conn my-task@my-host",
        comment="use the explicit host and agent",
    )
    assert result.exit_code != 0
    # The failure must be a clean, host-scoped resolution error that names the
    # bogus host -- not a crash or a misleading "agent not found". This proves
    # the `agent@host` syntax was parsed and the host component drove the lookup.
    combined_output = (result.stdout + result.stderr).lower()
    assert "my-host" in combined_output, combined_output
    assert "no hosts found" in combined_output, combined_output
    # A Python traceback would mean the error escaped rather than being reported
    # as a clean user-facing message.
    assert "traceback (most recent call last)" not in combined_output, combined_output


@pytest.mark.release
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
    # The provider-qualified `host.provider` syntax is accepted and resolved;
    # `my-host.modal` doesn't exist in the test env, so mngr exits with a clean
    # controlled error (exit 1) that names the full host spec -- not a crash or
    # an unhandled traceback.
    assert result.exit_code == 1
    output = (result.stdout + result.stderr).lower()
    assert "no hosts found matching my-host.modal" in output
    assert "traceback (most recent call last)" not in output


# No @pytest.mark.modal: see test_connect_by_name (local-only resolution).
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_connect_with_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # the default behavior is to start the agent if it's stopped (you can be explicit about that too):
        mngr connect my-task --start
    """)
    _create_my_task(e2e, 100202)
    result = e2e.run_connect_interactively(
        "mngr connect my-task --start",
        agent_name="my-task",
        comment="explicit --start behavior",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Connecting to agent: my-task")


# No @pytest.mark.modal: see test_connect_by_name (local-only resolution).
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_connect_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can disable auto-starting (fails if agent is stopped)
        mngr connect my-task --no-start
    """)
    _create_my_task(e2e, 100203)
    result = e2e.run_connect_interactively(
        "mngr connect my-task --no-start",
        agent_name="my-task",
        comment="disable auto-starting",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Connecting to agent: my-task")
