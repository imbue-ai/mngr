"""Tests for mngr create agent-type and option combinations from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_runs_agent_types_configured_command(e2e: E2eSession) -> None:
    # Use a locally-bound name since we assert on the exact command string below.
    expected_command = "sleep 123456789"
    sleep_agent_type = e2e.make_sleep_agent_type(expected_command)
    e2e.write_tutorial_block("""
    # any program on PATH can be used directly as an agent type (falls back to
    # running the program as-is), and arguments to the program go after `--`:
    mngr create my-task python -- my_script.py
    # remember that the arguments to the "agent" (or command) come after the `--` separator
    """)
    expect(
        e2e.run(
            f"mngr create my-task --type {sleep_agent_type} --no-ensure-clean",
            comment="any program on PATH can be used as an agent type -- here we use a registered test_sleep type",
        )
    ).to_succeed()

    # Verify the agent's configured command (sleep) is actually running inside the agent
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the agent's sleep command is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_with_idle_mode_and_timeout(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100077")
    e2e.write_tutorial_block("""
    # this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
    # this makes debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
    mngr create my-task python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
    # see "RUNNING NON-AGENT PROCESSES" below for more details
    """)
    # Idle timeout requires a remote provider (local provider rejects it).
    # Use Modal to exercise the real idle timeout path.
    result = e2e.run(
        f"mngr create my-task --provider modal --type {sleep_agent_type} --no-ensure-clean"
        " --idle-mode run --idle-timeout 60 --no-connect",
        comment="idle timeout requires a remote provider",
        timeout=120.0,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_extra_tmux_windows(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100078")
    e2e.write_tutorial_block("""
    # alternatively, you can simply add extra tmux windows that run alongside your agent:
    mngr create my-task -w server="npm run dev" -w logs="tail -f app.log"
    # that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)
    """)
    expect(
        e2e.run(
            f'mngr create my-task --type {sleep_agent_type} --no-ensure-clean -w extra="sleep 99999"',
            comment="you can simply add extra tmux windows that run alongside your agent",
        )
    ).to_succeed()

    # Verify the extra tmux window exists by listing tmux windows
    list_result = e2e.run("mngr list --format json", comment="Verify agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_no_ensure_clean(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100079")
    e2e.write_tutorial_block("""
    # by default, mngr aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mngr create my-task --no-ensure-clean
    # this is particularly useful for starting agents when, eg, you are in the middle of a merge conflict and you just want the agent to finish it off, for example
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """)
    # Make the working tree dirty so --no-ensure-clean is actually needed
    e2e.run("touch untracked-file.txt && git add untracked-file.txt", comment="Dirty the working tree")

    expect(
        e2e.run(
            f"mngr create my-task --type {sleep_agent_type} --no-ensure-clean",
            comment="by default, mngr aborts the create command if the working tree has uncommitted changes",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent created despite dirty working tree")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_connect_command(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100080")
    e2e.write_tutorial_block("""
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mngr create my-task --connect-command "my_script.sh"
    """)
    expect(
        e2e.run(
            f'mngr create my-task --type {sleep_agent_type} --no-ensure-clean --connect-command "echo connected" --no-connect',
            comment="you can use a custom connect command instead of the default",
        )
    ).to_succeed()

    # Verify the agent was created and is running
    list_result = e2e.run("mngr list --format json", comment="Verify agent created with custom connect command")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_message(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100081")
    e2e.write_tutorial_block("""
    # you can send a message when starting the agent (great for scripting):
    mngr create my-task --no-connect --message "Do the thing"
    """)
    expect(
        e2e.run(
            f'mngr create my-task --type {sleep_agent_type} --no-ensure-clean --no-connect --message "Do the thing"',
            comment="you can send a message when starting the agent (great for scripting)",
        )
    ).to_succeed()

    # Verify the agent was created
    list_result = e2e.run("mngr list --format json", comment="Verify agent created with initial message")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
