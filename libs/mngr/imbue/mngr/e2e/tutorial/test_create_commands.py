"""Tests for mngr create agent-type and option combinations from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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
    # The `command` agent type and the post-`--` command should both be reflected.
    assert matching[0]["type"] == "command"
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

    # Verify the idle settings and command actually landed on the agent. The
    # JSON output reflects the host's resolved ActivityConfig: idle_mode is the
    # uppercased IdleMode value and idle_timeout_seconds is the parsed timeout.
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify the agent's idle mode, idle timeout, and command",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one my-task agent, got: {parsed['agents']}"
    agent = matching[0]
    assert agent["command"] == "sleep 100077", f"Unexpected command: {agent['command']}"
    assert agent["idle_mode"] == "RUN", f"Unexpected idle_mode: {agent['idle_mode']}"
    assert agent["idle_timeout_seconds"] == 60, f"Unexpected idle_timeout_seconds: {agent['idle_timeout_seconds']}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Raise the per-test timeout above the 10s default: this test runs four mngr
# commands, including a `mngr exec` that connects into the agent, which pushes
# the cumulative body time past the default.
@pytest.mark.timeout(30)
# This test exercises only the local provider (it verifies the extra tmux
# window on the local tmux server), so it must NOT carry @pytest.mark.modal:
# the resource guard fails a modal-marked test that never invokes modal in a
# trackable way (the incidental modal discovery in `mngr list` happens in the
# mngr subprocess via the SDK, which the in-process guard cannot observe).
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
    # Mirror the tutorial's two named windows ("server" and "logs"). The
    # tutorial uses `npm run dev` / `tail -f app.log`, which need a real
    # project; here we use distinctly-numbered `sleep` commands so the windows
    # stay alive and we can confirm each window's command is actually running.
    expect(
        e2e.run(
            'mngr create my-task --type command --no-ensure-clean'
            ' -w server="sleep 99991" -w logs="sleep 99992" -- sleep 100078',
            comment="you can simply add extra tmux windows that run alongside your agent",
        )
    ).to_succeed()

    # Verify the agent was created
    list_result = e2e.run("mngr list --format json", comment="Verify agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1

    # Verify both extra tmux windows ("server" and "logs") actually exist,
    # alongside the main window running the agent.
    session_name = "mngr_test-my-task"
    windows_result = e2e.run(
        f"tmux list-windows -t {session_name} -F '#{{window_name}}'",
        comment="that command automatically starts two tmux windows named server and logs",
    )
    expect(windows_result).to_succeed()
    window_names = windows_result.stdout.strip().split("\n")
    for expected_window in ("server", "logs"):
        assert expected_window in window_names, f"Expected '{expected_window}' window, got: {window_names}"

    # Verify the per-window commands are actually running inside the agent, not
    # just that the windows exist. Each sleep has a unique duration so we can
    # match it unambiguously.
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux'",
        comment="Verify each window's command is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 99991")
    expect(ps_result.stdout).to_contain("sleep 99992")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This exercises only a local-provider create; verification lists with
# `--provider local` so it does not fan out to Modal (hence no @pytest.mark.modal).
# The create still copies the working tree via rsync, so the rsync mark stays.
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

    list_result = e2e.run(
        "mngr list --provider local --format json", comment="Verify agent created despite dirty working tree"
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "my-task" in agent_names


@pytest.mark.release
# Unhappy-path counterpart to test_create_with_no_ensure_clean, sharing the same
# tutorial block: the default clean-tree check aborts before any host/agent work,
# so this needs no tmux/rsync/modal resources. Verification lists with
# `--provider local` to avoid fanning out to Modal.
@pytest.mark.timeout(120)
def test_create_aborts_on_dirty_tree_by_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, mngr aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mngr create my-task --no-ensure-clean
    # this is particularly useful when, for example, you are in the middle of a merge conflict and you just want the agent to finish it off
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """)
    # Make the working tree dirty so the default clean check has something to catch
    e2e.run("touch untracked-file.txt && git add untracked-file.txt", comment="Dirty the working tree")

    # Without --no-ensure-clean, create aborts on the dirty working tree.
    result = e2e.run(
        "mngr create my-task --type command -- sleep 100081",
        comment="by default, mngr aborts the create command if the working tree has uncommitted changes",
    )
    expect(result).to_fail()

    # The aborted create must not have left an agent behind.
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="Verify no agent was created when the tree was dirty"
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "my-task" not in agent_names


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This test uses the default local provider, so it never invokes the `modal`
# CLI (the resource guard only tracks the binary, and `mngr list` reaches Modal
# via the SDK, not the CLI). Hence no @pytest.mark.modal -- adding it would trip
# the guard's "marked modal but never invoked modal" check.
# Creating + connecting the agent (including the one-time ttyd install attempt)
# plus the follow-up `mngr list` does not reliably fit in the default 10s
# func-only timeout, so allow more headroom (cf. test_create_with_idle_mode_and_timeout).
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
    # Verify the create output exercised the full initial-message path: the
    # agent had to signal readiness ("Starting agent") before the message was
    # sent ("Sending initial message"). A plain create (no --message) never
    # logs the latter, so asserting on both distinguishes this code path.
    expect(create_result.stderr).to_contain("Starting agent")
    expect(create_result.stderr).to_contain("Sending initial message")

    # Verify the agent was created and is actually live with the expected
    # command. The --message flow only reaches send_message after
    # wait_for_ready_signal succeeds, so a live agent here confirms that the
    # readiness handshake completed (not just that the command was accepted).
    list_result = e2e.run("mngr list --format json", comment="Verify agent created with initial message")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    agent = matching[0]
    assert agent["command"] == "sleep 100081"
    assert agent["state"] in ("RUNNING", "WAITING")
