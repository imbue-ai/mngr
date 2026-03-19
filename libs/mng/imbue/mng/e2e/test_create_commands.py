"""Tests for custom commands and creation options from the tutorial."""

import pytest

from imbue.skitwright.expect import expect
from imbue.skitwright.session import Session


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_custom_command(e2e: Session, agent_name: str) -> None:
    """
    # you can run *any* literal command instead of a named agent type:
    mng create my-task --command python -- my_script.py
    # remember that the arguments to the "agent" (or command) come after the `--` separator
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean",
            comment="you can run *any* literal command instead of a named agent type",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent is created and running")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(rf"{agent_name}\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_idle_mode_and_timeout(e2e: Session, agent_name: str) -> None:
    """
    # this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
    # this make debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
    mng create my-task --command python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
    # see "RUNNING NON-AGENT PROCESSES" below for more details
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --idle-mode run --idle-timeout 60",
            comment="this enables some pretty interesting use cases, like running servers or other programs",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with idle-mode run")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_extra_tmux_windows(e2e: Session, agent_name: str) -> None:
    """
    # alternatively, you can simply add extra tmux windows that run alongside your agent:
    mng create my-task -w server="npm run dev" -w logs="tail -f app.log"
    # that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean -w extra=\"sleep 99999\"",
            comment="you can simply add extra tmux windows that run alongside your agent",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with extra tmux window")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_no_ensure_clean(e2e: Session, agent_name: str) -> None:
    """
    # by default, mng aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mng create my-task --no-ensure-clean
    # this is particularly useful for starting agents when, eg, you are in the middle of a merge conflict and you just want the agent to finish it off, for example
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean",
            comment="by default, mng aborts the create command if the working tree has uncommitted changes",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with --no-ensure-clean")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_connect_command(e2e: Session, agent_name: str) -> None:
    """
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mng create my-task --connect-command "my_script.sh"
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --connect-command \"echo connected\" --no-connect",
            comment="you can use a custom connect command instead of the default",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with custom connect command")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_message(e2e: Session, agent_name: str) -> None:
    """
    # you can send a message when starting the agent (great for scripting):
    mng create my-task --no-connect --message "Do the thing"
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --no-connect --message \"Do the thing\"",
            comment="you can send a message when starting the agent (great for scripting)",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with initial message")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)
