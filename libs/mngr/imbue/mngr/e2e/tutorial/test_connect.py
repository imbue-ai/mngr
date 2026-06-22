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

import json

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
#
# No @pytest.mark.rsync either: the source is a clean local git repo, so create
# defaults to a git-worktree transfer (not rsync), and `_transfer_extra_files`
# only rsyncs untracked/modified files -- of which there are none here. The
# connect command itself is a local `tmux attach` and never invokes rsync.
@pytest.mark.release
@pytest.mark.tmux
# Creating the agent, attaching a real tmux client, and detaching it takes
# longer than the default 10s per-test timeout, so give the interactive flow
# room. The helper itself caps each wait at 30s.
@pytest.mark.timeout(120)
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
# No @pytest.mark.rsync: a local command agent lives in a git worktree (no rsync
# on create) and connecting to it just attaches tmux, so rsync is never invoked.
@pytest.mark.release
@pytest.mark.tmux
# See test_connect_by_name: the interactive attach/detach flow exceeds the
# default 10s per-test timeout.
@pytest.mark.timeout(120)
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
# A fictional (non-existent) agent id cannot be resolved by the discovery
# event-stream optimization, so mngr must run a *full* multi-provider discovery
# scan (local + Docker + Modal + Vultr) to prove the id exists nowhere before it
# can report "not found". That scan reaches over the network and easily exceeds
# the default 10s per-test timeout (it takes ~25s here), so give it room. Unlike
# test_connect_by_name -- which connects to an agent that exists locally and thus
# resolves to the local provider only -- this path legitimately queries the
# remote providers, but only from the unguarded e2e subprocess, so no
# @pytest.mark.modal is required (and adding it would trip the superfluous-mark
# guard, since the in-process test body never touches Modal).
@pytest.mark.timeout(120)
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
    # A Python traceback would mean the missing-agent case crashed rather than
    # being reported as a clean, user-facing error.
    assert "traceback (most recent call last)" not in combined_output, combined_output


# No @pytest.mark.modal: my-task is created locally first, so the discovery
# event-stream optimization resolves the "my-task" identifier to the local
# provider only. The host component "my-host" carries no provider, so without
# that local scoping mngr would have to scan *every* provider (constructing the
# Modal provider, which makes a real `app_lookup` network call before
# disabling itself) just to prove no host is named my-host. Creating the local
# agent keeps resolution local-only -- the Modal provider is never constructed,
# so this unhappy path exercises no guarded resource and is fast.
#
# No @pytest.mark.rsync: connect fails at host resolution before any rsync, and
# creating a local command agent never rsyncs either.
@pytest.mark.release
@pytest.mark.tmux
# Creating the local agent plus the connect attempt exceeds the default 10s
# per-test timeout, so give the flow room (mirrors the by-name tests).
@pytest.mark.timeout(120)
def test_connect_explicit_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can use the explicit host and agent:
        mngr conn my-task@my-host
    """)
    # `@my-host` refers to a host that doesn't exist in the test env; assert the
    # command parses the syntax and returns a clean error rather than crashing.
    # my-task itself exists (locally), so resolution finds the agent but then
    # filters by the host constraint "my-host", finds no such host, and exits
    # before ever attaching a tmux session or running rsync.
    _create_my_task(e2e, 100206)
    result = e2e.run(
        "mngr conn my-task@my-host",
        comment="use the explicit host and agent",
    )
    assert result.exit_code != 0
    # The failure must be a clean, host-scoped resolution error that names the
    # bogus host -- not a crash or a misleading "agent not found". Asserting on
    # the combined "no hosts found matching my-host" phrase (rather than the two
    # fragments separately) proves the `agent@host` syntax was parsed and the
    # host component -- not the agent name -- drove the lookup.
    combined_output = (result.stdout + result.stderr).lower()
    assert "no hosts found matching my-host" in combined_output, combined_output
    # A Python traceback would mean the error escaped rather than being reported
    # as a clean user-facing message.
    assert "traceback (most recent call last)" not in combined_output, combined_output


# Unlike test_connect_explicit_host (bare `@my-host`, which resolves without a
# provider and is short-circuited away from Modal), the explicit `.modal`
# qualifier narrows discovery to the Modal provider, so resolution loads the
# Modal SDK and queries it for matching hosts before concluding none exist. That
# SDK work pushes the command just past the default 10s per-test timeout, so give
# the resolution room.
#
# No @pytest.mark.modal: in a fresh environment the Modal environment does not
# exist, so discovery never invokes the `modal` CLI -- the only Modal usage the
# resource guard can observe across the e2e subprocess boundary (the in-process
# SDK guard cannot see the subprocess). Marking the test @pytest.mark.modal would
# therefore trip the guard's "marked but never invoked" check (see
# test_list_active_filter for the same reasoning).
@pytest.mark.release
@pytest.mark.timeout(120)
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
# No @pytest.mark.rsync: connecting to an already-running *local* agent with
# --start is a no-op start followed by a plain tmux attach -- no file sync
# happens, so rsync is never invoked. (The resource guard fails any passing
# test that carries a mark it never exercises.)
@pytest.mark.release
@pytest.mark.tmux
# See test_connect_by_name: the interactive attach/detach flow exceeds the
# default 10s per-test timeout.
@pytest.mark.timeout(120)
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
# No @pytest.mark.rsync: the agent is a *local* agent in a git repo, so its
# workspace is synced via a git worktree, not rsync (rsync is only used for
# local<->remote, non-git transfers). The interactive connect attaches a real
# tmux client (hence @pytest.mark.tmux), but rsync is never invoked, so carrying
# the mark would trip the resource guard for an unused resource.
@pytest.mark.release
@pytest.mark.tmux
# See test_connect_by_name: the interactive attach/detach flow exceeds the
# default 10s per-test timeout.
@pytest.mark.timeout(120)
def test_connect_with_start_restarts_stopped_agent(e2e: E2eSession) -> None:
    # Shares the tutorial block with test_connect_with_start, but exercises the
    # *distinguishing* behavior of --start: test_connect_with_start connects to an
    # already-running agent (where --start is a no-op), so it never proves that
    # --start restarts a stopped agent. Here the agent is stopped first, so a plain
    # connect would fail; --start must transition it back to running before attaching.
    e2e.write_tutorial_block("""
        # the default behavior is to start the agent if it's stopped (you can be explicit about that too):
        mngr connect my-task --start
    """)
    _create_my_task(e2e, 100205)
    # Stop the freshly-created (running) agent so --start has real work to do.
    # Address the agent as my-task@localhost.local so discovery pins the local
    # provider (see _collect_required_provider_names): without it, `mngr stop`
    # fans out to every registered provider, including the unconfigured/unreachable
    # remote ones (AWS/Vultr/...), which intermittently make this setup command
    # exceed the per-command timeout. `mngr connect` itself stays local-only via
    # the discovery event-stream optimization (this test carries no modal mark).
    # Each mngr invocation pays a multi-second startup cost, so this test keeps
    # the command count low (create, stop, connect, one list) to stay within the
    # per-test timeout; the stop output below is sufficient proof of the
    # stopped precondition without an extra `mngr list` round-trip.
    stop_result = e2e.run("mngr stop my-task@localhost.local", comment="stop my-task so --start must restart it")
    expect(stop_result).to_succeed()
    expect(stop_result.stdout).to_contain("Stopped agent: my-task")

    result = e2e.run_connect_interactively(
        "mngr connect my-task --start",
        agent_name="my-task",
        comment="explicit --start behavior (restarts the stopped agent)",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Connecting to agent: my-task")
    # The connect output itself proves the distinguishing behavior of --start: it
    # detected the stopped agent and restarted it before attaching (a plain connect
    # or --no-start would have refused). This is exactly what differentiates this
    # test from test_connect_with_start, which connects to an already-running agent.
    expect(result.stdout).to_contain("Agent my-task is stopped, starting it")

    # The persisted effect of --start: the previously-stopped agent is alive again.
    # The helper only detaches the tmux client, so the restarted agent survives.
    # Scope discovery to --provider local (fast, no remote fan-out) and read the
    # state from JSON: the agent must be present and have left the STOPPED state
    # (a restarted command agent settles in an active state such as WAITING).
    listing = e2e.run(
        "mngr list --provider local --format json", comment="confirm --start brought my-task back to life"
    )
    expect(listing).to_succeed()
    agents = json.loads(listing.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["state"] != "STOPPED", f"expected my-task to be restarted (not STOPPED), got: {matching[0]}"


# No @pytest.mark.modal: see test_connect_by_name (local-only resolution).
# No @pytest.mark.rsync: a local agent is created via a git worktree (the default
# transfer mode for a same-host git repo) and connect just execs `tmux attach`, so
# rsync is never invoked. Declaring the mark would trip the resource guard, which
# fails any passing test that declares a resource it never uses.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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


# Shares the tutorial block of test_connect_no_start, covering the "unhappy"
# path the tutorial comment explicitly calls out: "fails if agent is stopped".
# Connecting with --no-start fails before any tmux attach, so the plain
# pipe-based e2e.run is sufficient (no PTY needed). No @pytest.mark.rsync:
# unlike the happy path, the connect refuses before the rsync-backed attach is
# ever reached, so rsync is never invoked (only create/stop exercise tmux).
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_connect_no_start_fails_when_stopped(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or you can disable auto-starting (fails if agent is stopped)
        mngr connect my-task --no-start
    """)
    _create_my_task(e2e, 100204)
    # Stop the agent so --no-start has nothing to attach to and must refuse.
    expect(e2e.run("mngr stop my-task", comment="stop the agent so --no-start fails")).to_succeed()
    result = e2e.run(
        "mngr connect my-task --no-start",
        comment="disable auto-starting (fails if agent is stopped)",
    )
    # mngr must refuse with a clean, controlled error -- not auto-start the agent
    # and not crash. The message names the agent and explains that auto-start is
    # disabled, proving --no-start was honored rather than silently ignored.
    assert result.exit_code != 0, f"expected non-zero exit, got {result.exit_code}"
    combined_output = (result.stdout + result.stderr).lower()
    assert "my-task" in combined_output, combined_output
    assert "stopped and automatic starting is disabled" in combined_output, combined_output
    # A successful connect would have logged this line; it must NOT appear.
    assert "connecting to agent: my-task" not in combined_output, combined_output
    # A Python traceback would mean the error escaped instead of being reported
    # as a clean user-facing message.
    assert "traceback (most recent call last)" not in combined_output, combined_output
    # The observable effect of --no-start refusing: the agent was NOT auto-started
    # behind the scenes. It must still be stopped, proving --no-start was honored at
    # the state level, not merely in the message (a started agent would have left
    # the stopped set). Scope the query to the local provider so discovery does not
    # fan out to the (unreachable) remote providers configured in the test env.
    still_stopped = e2e.run(
        "mngr list --stopped --provider local",
        comment="confirm --no-start left my-task stopped",
    )
    expect(still_stopped).to_succeed()
    expect(still_stopped.stdout).to_contain("my-task")
