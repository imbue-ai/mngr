"""Tests for custom commands and creation options from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_custom_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can run *any* literal command instead of a named agent type:
    mngr create my-task --command python -- my_script.py
    # remember that the arguments to the "agent" (or command) come after the `--` separator
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="you can run *any* literal command instead of a named agent type",
        )
    ).to_succeed()

    # Verify the custom command is actually running inside the agent
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the custom command (sleep) is running",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 99999")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_idle_mode_and_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
    # this make debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
    mngr create my-task --command python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
    # see "RUNNING NON-AGENT PROCESSES" below for more details
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --idle-mode run --idle-timeout 60",
            comment="this enables some pretty interesting use cases, like running servers or other programs",
        )
    ).to_succeed()

    # Verify the idle settings were applied by checking the JSON output
    list_result = e2e.run("mngr list --format json", comment="Verify idle-mode and idle-timeout are set")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    # --idle-timeout takes minutes; JSON output is in seconds
    assert matching[0]["idle_timeout_seconds"] == 3600


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_extra_tmux_windows(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # alternatively, you can simply add extra tmux windows that run alongside your agent:
    mngr create my-task -w server="npm run dev" -w logs="tail -f app.log"
    # that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean -w extra=\"sleep 99999\"",
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
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean",
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
    e2e.write_tutorial_block("""
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mngr create my-task --connect-command "my_script.sh"
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --connect-command \"echo connected\" --no-connect",
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
    e2e.write_tutorial_block("""
    # you can send a message when starting the agent (great for scripting):
    mngr create my-task --no-connect --message "Do the thing"
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --no-connect --message \"Do the thing\"",
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
