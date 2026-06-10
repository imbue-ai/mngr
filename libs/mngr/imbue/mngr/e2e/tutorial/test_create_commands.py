"""Tests for mngr create agent-type and option combinations from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Override the default 10s function timeout: a real create (tmux session +
# asciinema connect, plus a one-time ttyd install on hosts that lack it)
# followed by `mngr exec` and `mngr list` routinely exceeds 10s.
@pytest.mark.timeout(120)
def test_create_command_agent_runs_post_dash_command_in_agent(e2e: E2eSession) -> None:
    # Use a locally-bound name since we assert on the exact command string below.
    expected_command = "sleep 123456789"
    e2e.write_tutorial_block("""
    # to run an arbitrary shell command, use the built-in `command` agent type
    # and put the command (and its args) after `--`:
    mngr create my-task --type command -- python my_script.py
    # remember that the arguments to the "agent" (or command) come after the `--` separator
    """)
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean -- {expected_command}",
            comment="run a shell command as the agent body via --type command",
        )
    ).to_succeed()

    # Verify the agent's configured command (sleep) is actually running inside the agent
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the agent's sleep command is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)

    # Verify the agent was created with the custom command via JSON metadata
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify the agent's command field reflects the custom command",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["command"] == expected_command


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_with_idle_mode_and_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
    # this makes debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
    mngr create my-task --type command --idle-mode run --idle-timeout 60 -- python my_long_running_script.py extra-args
    # see "RUNNING NON-AGENT PROCESSES" below for more details
    """)
    # Idle timeout requires a remote provider (local provider rejects it).
    # Use Modal to exercise the real idle timeout path. The `--idle-*` and
    # `--no-connect` options must precede `--`, otherwise they are consumed
    # as agent_args and never reach mngr create.
    result = e2e.run(
        "mngr create my-task --provider modal --type command --no-ensure-clean"
        " --idle-mode run --idle-timeout 60 --no-connect -- sleep 100077",
        comment="idle timeout requires a remote provider",
        timeout=120.0,
    )
    expect(result).to_succeed()

    # Verify the idle-mode/idle-timeout actually took effect on the created agent
    # (not just that the command exited 0). The list JSON surfaces the host's
    # activity config per agent, so we can assert the concrete settings.
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify the idle settings took effect on the modal host",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    agent = matching[0]
    # The command agent runs the post-`--` command on a modal host with idle-mode
    # "run" (host stops when the process finishes) and a 60s idle timeout.
    assert agent["command"] == "sleep 100077", f"Unexpected command: {agent['command']}"
    assert agent["type"] == "command", f"Unexpected agent type: {agent['type']}"
    assert agent["idle_mode"] == "RUN", f"Unexpected idle_mode: {agent['idle_mode']}"
    assert agent["idle_timeout_seconds"] == 60, f"Unexpected idle_timeout: {agent['idle_timeout_seconds']}"
    assert agent["host"]["provider_name"] == "modal", f"Unexpected provider: {agent['host']['provider_name']}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This is a purely local-provider test: it creates a local agent and inspects
# its tmux windows. It deliberately carries no @pytest.mark.modal -- since
# `mngr list` no longer auto-creates the per-user Modal environment for
# read-only commands, a local agent never invokes Modal, and the resource
# guard would flag the mark as never-invoked.
# Override the default 10s function timeout: a real create (tmux session +
# asciinema connect, plus a one-time ttyd install on hosts that lack it)
# followed by `mngr list` routinely exceeds 10s.
@pytest.mark.timeout(120)
# Flaky: collateral damage from a leaked `mngr observe` process that the
# system_interface's AgentManager spawns and doesn't always clean up (lives
# in forever-claude-template/apps/system_interface). session_cleanup
# attributes the leak to whichever test runs last in the offload sandbox;
# this one happens to draw the short straw. Real fix lives in
# system_interface's observe lifecycle, not here.
@pytest.mark.flaky
def test_create_with_extra_tmux_windows(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # alternatively, you can simply add extra tmux windows that run alongside your agent:
    mngr create my-task -w server="npm run dev" -w logs="tail -f app.log"
    # that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)
    """)
    # Use the tutorial's exact window names ("server" and "logs"). The agent
    # bodies are `sleep` stand-ins for `npm run dev` / `tail -f app.log` so the
    # commands don't depend on tools that aren't present in the test sandbox.
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean"
            ' -w server="sleep 99999" -w logs="sleep 99998" -- sleep 100078',
            comment="you can simply add extra tmux windows that run alongside your agent",
        )
    ).to_succeed()

    # Verify the agent was created
    list_result = e2e.run("mngr list --format json", comment="Verify agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1

    # Verify both extra tmux windows exist, and that they run *in addition to*
    # the agent's main window (so the count must exceed the two extras).
    session_name = "mngr_test-my-task"
    windows_result = e2e.run(
        f"tmux list-windows -t {session_name} -F '#{{window_name}}'",
        comment="that command automatically starts two tmux windows named server and logs",
    )
    expect(windows_result).to_succeed()
    window_names = windows_result.stdout.strip().split("\n")
    assert "server" in window_names, f"Expected 'server' window, got: {window_names}"
    assert "logs" in window_names, f"Expected 'logs' window, got: {window_names}"
    assert len(window_names) > 2, f"Expected a main agent window in addition to the extras, got: {window_names}"

    # Verify the windows are not just present by name but actually *running* their
    # configured commands -- that is the whole point of `-w name="cmd"`. Both the
    # "server" (sleep 99999) and "logs" (sleep 99998) stand-ins should be live
    # processes on the host, alongside the agent's own main command (sleep 100078).
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux'",
        comment="the extra windows run those commands alongside the agent",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 99999")
    expect(ps_result.stdout).to_contain("sleep 99998")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Override the default 10s function timeout: a real create (tmux session +
# asciinema connect, plus a one-time ttyd install on hosts that lack it)
# followed by `mngr list` routinely exceeds 10s.
@pytest.mark.timeout(120)
def test_create_with_no_ensure_clean(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, mngr aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mngr create my-task --no-ensure-clean
    # this is particularly useful when, for example, you are in the middle of a merge conflict and you just want the agent to finish it off
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """)
    # Make the working tree dirty so --no-ensure-clean is actually needed
    e2e.run("touch untracked-file.txt && git add untracked-file.txt", comment="Dirty the working tree")

    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100079",
            comment="by default, mngr aborts the create command if the working tree has uncommitted changes",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify agent created despite dirty working tree")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "my-task" in agent_names


# Unhappy-path counterpart to test_create_with_no_ensure_clean, sharing the same
# tutorial block. It verifies the *default* behavior the block describes: without
# --no-ensure-clean, `mngr create` aborts when the working tree is dirty. The
# abort fires before any host/tmux/rsync work, so this test carries neither
# @pytest.mark.tmux nor @pytest.mark.rsync (the resource guard would flag them as
# never-invoked).
@pytest.mark.release
@pytest.mark.timeout(120)
def test_create_aborts_on_dirty_tree_by_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, mngr aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mngr create my-task --no-ensure-clean
    # this is particularly useful when, for example, you are in the middle of a merge conflict and you just want the agent to finish it off
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """)
    # Dirty the working tree so the default ensure-clean check has something to trip on.
    e2e.run("touch untracked-file.txt && git add untracked-file.txt", comment="Dirty the working tree")

    # Without --no-ensure-clean, the create must abort because the tree is dirty.
    result = e2e.run(
        "mngr create my-task --type command -- sleep 100082",
        comment="by default, mngr aborts the create command if the working tree has uncommitted changes",
    )
    expect(result).to_fail()
    # The abort message should explain the cause and point at the escape hatch.
    expect(result.stderr).to_contain("uncommitted changes")
    expect(result.stderr).to_contain("--no-ensure-clean")

    # The agent must not have been created.
    list_result = e2e.run("mngr list --format json", comment="Verify no agent was created after the abort")
    expect(list_result).to_succeed()
    agent_names = [a["name"] for a in json.loads(list_result.stdout)["agents"]]
    assert "my-task" not in agent_names, f"Agent should not exist after abort, got: {agent_names}"


# No @pytest.mark.modal: this test uses the default local provider. The
# `mngr list` discovery call against a not-yet-existent Modal environment
# short-circuits inside the in-subprocess Modal SDK (it never shells out to the
# guarded `modal` CLI binary, the only path the resource guard sees across the
# subprocess boundary), so the modal mark would fail the never-invoked guard.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_connect_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mngr create my-task --connect-command "my_script.sh"
    """)
    # Create with a custom connect command that echoes env vars set by mngr.
    # Single quotes around the connect command prevent the outer shell from
    # expanding $MNGR_AGENT_NAME; it is expanded by the inner shell that mngr
    # exec's into via run_connect_command.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean"
        " --connect-command 'echo agent=$MNGR_AGENT_NAME' -- sleep 100080",
        comment="you can use a custom connect command instead of the default",
    )
    expect(result).to_succeed()
    # Verify the custom connect command actually ran and received the agent name
    expect(result.stdout).to_contain("agent=my-task")

    # Verify the agent was created and is running
    list_result = e2e.run("mngr list --format json", comment="Verify agent created with custom connect command")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# No @pytest.mark.modal here (unlike the sibling tests): this test creates a
# local command agent and only contacts Modal through `mngr list`'s discovery
# path, which runs the Modal Python SDK inside the mngr subprocess. The resource
# guard only observes Modal usage via the `modal` CLI binary (PATH wrapper) or
# the in-process SDK monkeypatch -- neither of which a subprocess SDK call
# trips -- so the mark would fail the guard's NEVER_INVOKED check.
# The --message path starts the agent, waits for its ready signal, and only
# then sends the message (see api/create.py). That ready-signal dance makes
# create slower than the sibling tests, so the default 10s function timeout is
# too tight; give it the same headroom as test_create_with_idle_mode_and_timeout.
@pytest.mark.timeout(120)
def test_create_with_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can send a message when starting the agent (great for scripting):
    mngr create my-task --no-connect --message "Do the thing"
    """)
    create_result = e2e.run(
        'mngr create my-task --type command --no-ensure-clean --no-connect --message "Do the thing" -- sleep 100081',
        comment="you can send a message when starting the agent (great for scripting)",
    )
    expect(create_result).to_succeed()
    # Verify the create output confirms the message was sent
    expect(create_result.stderr).to_contain("Sending initial message")

    # Verify the agent was created
    list_result = e2e.run("mngr list --format json", comment="Verify agent created with initial message")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1

    # Verify the message was actually delivered into the agent's tmux pane, not
    # just that mngr logged "Sending initial message". For a command agent the
    # message is typed into the main pane (send_message -> tmux literal keys), so
    # the message text must be visible when we capture that pane.
    session_name = "mngr_test-my-task"
    capture_result = e2e.run(
        f"tmux capture-pane -t {session_name} -p",
        comment="Verify the initial message was delivered into the agent's pane",
    )
    expect(capture_result).to_succeed()
    expect(capture_result.stdout).to_contain("Do the thing")
