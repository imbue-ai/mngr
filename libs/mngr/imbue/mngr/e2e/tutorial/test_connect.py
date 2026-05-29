"""Tests for the connect-to-agent commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.

``mngr connect`` normally attaches to the agent's tmux session interactively,
which requires a controlling TTY that the pipe-based e2e runner cannot provide.
The e2e fixture therefore configures a ``connect_command`` for the ``connect``
command (see ``conftest.py``) that echoes the resolved agent/session/locality
and exits, so the calls below return immediately instead of blocking. Tests
assert on that echoed marker to confirm ``connect`` resolved the agent and
reached the connect step.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    """Create a long-running 'my-task' agent so connect/start variants have a target."""
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task for connect test (sleep {sleep_value})",
        )
    ).to_succeed()


def _expect_connected_to(result: CommandResult, agent_name: str) -> None:
    """Assert connect resolved the given agent and reached the connect step.

    The e2e fixture points the ``connect`` command's ``connect_command`` at a
    recorder that echoes ``agent=<name> session=<prefix><name> local=<bool>``.
    A successful exit plus this marker proves that ``connect`` (or its ``conn``
    alias) parsed the target, resolved it to a running agent on a started host,
    and invoked the connect step for the correct agent and session.
    """
    expect(result).to_succeed()
    combined = result.stdout + result.stderr
    assert f"agent={agent_name}" in combined, f"connect did not resolve agent {agent_name!r}:\n{combined}"
    assert f"session=mngr_test-{agent_name}" in combined, f"unexpected session name:\n{combined}"
    assert "local=true" in combined, f"expected a local agent host:\n{combined}"


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


# The agent is created and connected to entirely on the local host: create
# copies the source via rsync and runs the command under tmux, but nothing
# touches modal (the @pytest.mark.modal that the other connect tests carry is
# superfluous here). The default 10s pytest timeout is too short for the
# create step, so it is raised.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_connect_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr conn my-task
    """)
    _create_my_task(e2e, 100201)
    _expect_connected_to(e2e.run("mngr conn my-task", comment="short form"), "my-task")


@pytest.mark.release
# An unknown agent id cannot be resolved from the discovery event stream, so the
# command falls back to a full discovery scan across every enabled provider.
# That scan is read-only: per ModalProviderApp._get_or_create_app it never
# creates a Modal environment and so never shells out to the `modal` CLI -- the
# only Modal usage the resource guard can observe from a subprocess. Hence this
# test deliberately carries no @pytest.mark.modal: it would always fail the
# guard's "marked but never invoked" check. The scan still makes real network
# round-trips, which sit right at the default 10s per-test timeout, so give it
# headroom.
@pytest.mark.timeout(60)
def test_connect_by_agent_id_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # sometimes names can be ambiguous (e.g. if you made two agents with the same name on different hosts), so you can always
        # be really specific by using the agent id instead of the name:
        mngr connect agent-fa29307a16734899aa77b0f0563c8c99
    """)
    # The fictional agent id from the tutorial does not exist in the fresh test
    # environment, so the command is expected to fail. Because the id is well
    # formed but unknown, mngr scans every provider before reporting it missing,
    # which is slow -- give the subprocess room to finish so we observe the real
    # "Agent not found" error rather than a timeout kill.
    result = e2e.run(
        "mngr connect agent-fa29307a16734899aa77b0f0563c8c99",
        comment="connect using the agent id instead of the name",
        timeout=45.0,
    )
    # The command must fail (the agent does not exist) and must specifically
    # report the well-formed id as not found, proving mngr parsed the
    # id-as-target syntax and attempted resolution rather than rejecting it.
    assert result.exit_code != 0, f"expected non-zero exit, got transcript:\n{e2e.transcript}"
    combined_output = (result.stdout + result.stderr).lower()
    assert "not found" in combined_output, f"expected a 'not found' error, got transcript:\n{e2e.transcript}"
    assert "agent-fa29307a16734899aa77b0f0563c8c99" in combined_output, (
        f"expected the fictional id echoed back in the error, got transcript:\n{e2e.transcript}"
    )


@pytest.mark.release
@pytest.mark.timeout(120)
def test_connect_explicit_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can use the explicit host and agent:
        mngr conn my-task@my-host
    """)
    # `@my-host` refers to a host that doesn't exist in the test env. The
    # `agent@host` form pins no provider, so discovery does a full scan, but a
    # fresh env has no remote hosts in local state -- discovery resolves the
    # unknown host without ever invoking tmux, rsync, or the Modal SDK. Hence no
    # resource marks (cf. the error-path test_connect_by_agent_id_fictional). We
    # only assert that mngr parses the syntax and surfaces a clean "no such host"
    # error instead of crashing or hanging. The full provider scan plus mngr's
    # cold start can edge past the default 10s function timeout, so we widen it.
    result = e2e.run(
        "mngr conn my-task@my-host",
        comment="use the explicit host and agent",
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("No hosts found matching my-host")


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
    # `my-host.modal` names a (host, provider) pair that doesn't exist in the
    # fresh test env. The command should parse the agent@host.provider syntax,
    # reach host discovery, and fail cleanly reporting the missing host -- not
    # crash with a traceback.
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "no hosts found" in combined
    assert "my-host.modal" in combined


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


# Unlike the other connect tests, this one never reaches a remote provider: it
# only creates a local agent, stops it, and checks that --no-start refuses to
# auto-start. `mngr list` attempts Modal discovery but the environment was never
# created (no Modal agent exists), so the guarded Modal chokepoint is never hit.
# Hence no @pytest.mark.modal here -- the guard would flag it as never invoked.
#
# Creating an agent (provider init + tmux session) far exceeds the 10s default
# pytest timeout from pyproject.toml. The offload release config overrides that
# with --timeout=900, but a per-test marker is needed so the test also passes
# when run directly (e.g. `pytest ... -m release`). The generous value leaves
# headroom for the slower Modal-in-Modal offload environment.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_connect_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can disable auto-starting (fails if agent is stopped)
        mngr connect my-task --no-start
    """)
    _create_my_task(e2e, 100203)
    # The tutorial comment promises that --no-start "fails if agent is stopped",
    # so stop the agent first to exercise that documented behavior. (Connecting
    # to a *running* agent would block on an interactive tmux attach, which has
    # no terminal under the e2e runner; the stopped-agent path is the meaningful,
    # non-interactive behavior that distinguishes --no-start from --start.)
    expect(e2e.run("mngr stop my-task", comment="stop my-task so --no-start has nothing running")).to_succeed()
    # With the agent stopped, --no-start must refuse to auto-start it and exit
    # non-zero rather than attaching.
    result = e2e.run(
        "mngr connect my-task --no-start",
        comment="or you can disable auto-starting (fails if agent is stopped)",
    )
    assert result.exit_code != 0, (
        f"Expected --no-start to fail for a stopped agent, but it succeeded.\n{result.stdout}\n{result.stderr}"
    )
    combined_output = (result.stdout + result.stderr).lower()
    assert "stopped" in combined_output, (
        f"Expected an error explaining the agent is stopped, got:\n{result.stdout}\n{result.stderr}"
    )
    # --no-start must not have started the agent as a side effect: it is still stopped.
    listing = e2e.run("mngr list --stopped", comment="confirm --no-start did not start the agent")
    expect(listing).to_succeed()
    assert "my-task" in listing.stdout, (
        f"Expected my-task to still be stopped after --no-start, got:\n{listing.stdout}"
    )
