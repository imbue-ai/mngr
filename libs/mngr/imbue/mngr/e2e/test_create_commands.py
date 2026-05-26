"""Tests for mngr create agent-type and option combinations from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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
    expected_command = "sleep 100077"
    result = e2e.run(
        "mngr create my-task --provider modal --type command --no-ensure-clean"
        f" --idle-mode run --idle-timeout 60 --no-connect -- {expected_command}",
        comment="idle timeout requires a remote provider",
        timeout=120.0,
    )
    expect(result).to_succeed()

    # Verify the idle settings were actually applied to the created agent.
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify idle-mode and idle-timeout were applied to the agent",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected one agent named my-task, got: {[a['name'] for a in agents]}"
    agent = matching[0]
    assert agent["idle_mode"] == "RUN", f"Expected idle_mode=RUN, got: {agent['idle_mode']}"
    assert agent["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got: {agent['idle_timeout_seconds']}"
    )
    assert agent["command"] == expected_command, f"Expected command={expected_command!r}, got: {agent['command']!r}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
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
    # Use two -w flags (mirroring the tutorial's "server" + "logs" pattern)
    # with distinct sleep durations so each window's command can be matched
    # uniquely against the host's process list.
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

    # Verify both extra tmux windows ("server" and "logs") exist alongside
    # the agent's main window.
    session_name = "mngr_test-my-task"
    windows_result = e2e.run(
        f"tmux list-windows -t {session_name} -F '#{{window_name}}'",
        comment="Verify the extra tmux windows exist",
    )
    expect(windows_result).to_succeed()
    window_names = windows_result.stdout.strip().split("\n")
    assert "server" in window_names, f"Expected 'server' window, got: {window_names}"
    assert "logs" in window_names, f"Expected 'logs' window, got: {window_names}"

    # Verify the per-window commands are actually running, not just that the
    # windows exist. mngr exec inherits the agent's working dir so ps inside
    # it sees the same processes as the host.
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux'",
        comment="Verify each -w window's command is actually running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 99999")
    expect(ps_result.stdout).to_contain("sleep 99998")
    expect(ps_result.stdout).to_contain("sleep 100078")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_connect_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mngr create my-task --connect-command "my_script.sh"
    """)
    # Create with a custom connect command that echoes env vars set by mngr.
    # Single quotes around the connect command prevent the outer shell from
    # expanding the vars; they are expanded by the inner shell that mngr
    # exec's into via run_connect_command. The contract from
    # run_connect_command is that MNGR_AGENT_NAME, MNGR_SESSION_NAME, and
    # MNGR_HOST_IS_LOCAL are all set; verify all three reach the command.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean"
        " --connect-command 'echo agent=$MNGR_AGENT_NAME session=$MNGR_SESSION_NAME is_local=$MNGR_HOST_IS_LOCAL'"
        " -- sleep 100080",
        comment="you can use a custom connect command instead of the default",
    )
    expect(result).to_succeed()
    # Verify the custom connect command actually ran and received the documented env vars
    expect(result.stdout).to_contain("agent=my-task")
    expect(result.stdout).to_contain("session=mngr_test-my-task")
    expect(result.stdout).to_contain("is_local=true")

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

    # Verify the message text was actually delivered to the agent's tmux pane.
    # send_message types the text via `tmux send-keys -l`, which the pty echoes
    # to the screen even though `sleep` itself discards the input.
    session_name = "mngr_test-my-task"
    pane_result = e2e.run(
        f"tmux capture-pane -p -t {session_name}",
        comment="Verify the initial message was typed into the agent's tmux pane",
    )
    expect(pane_result).to_succeed()
    assert "Do the thing" in pane_result.stdout, (
        f"Expected initial message in pane, got: {pane_result.stdout!r}"
    )
