"""Basic end-to-end tests for the mngr CLI.

These tests exercise mngr through its CLI interface via subprocess. The e2e
fixture configures a custom connect_command that records tmux sessions via
asciinema instead of attaching interactively.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr --help",
        comment="or see the other commands--list, destroy, message, connect, push, pull, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    # The tutorial comment advertises these other commands, so the help output
    # must actually list them. (push/pull are folded into the `git` command, so
    # they are not asserted here.)
    for command in ("create", "list", "destroy", "message", "connect", "clone"):
        expect(result.stdout).to_contain(command)


@pytest.mark.release
def test_unknown_command_fails(e2e: E2eSession) -> None:
    # Shares the tutorial block with test_help_succeeds: that block teaches users
    # to discover commands via `mngr --help`. This is the unhappy path -- invoking
    # a command that does not exist must fail and point the user back to --help.
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr definitely-not-a-real-command",
        comment="an unknown command fails and points the user to --help",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("No such command")
    expect(result.stderr).to_contain("mngr --help")


@pytest.mark.release
def test_create_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")


@pytest.mark.release
def test_create_rejects_unknown_option(e2e: E2eSession) -> None:
    """Unhappy path for the same `mngr create` block: an option not listed in
    --help is rejected with a non-zero exit and a usage error, so the user is
    pointed back at the documented arguments.
    """
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create --this-flag-does-not-exist",
        comment="an option not listed in --help is rejected",
    )
    # Click/Typer use exit code 2 for usage errors. The unknown option is
    # rejected at argument-parsing time, before any host/agent work begins.
    expect(result).to_have_exit_code(2)
    # The error and usage hint go to stderr, leaving stdout clean for scripting.
    expect(result.stdout).to_be_empty()
    expect(result.stderr).to_contain("No such option: --this-flag-does-not-exist")
    expect(result.stderr).to_contain("Usage: mngr create")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_json_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --format json -- sleep 100064",
        comment="you can control output format for scripting",
    )
    expect(create_result).to_succeed()
    # The whole point of --format json is a machine-readable object on stdout
    # that a script can parse to get the new agent's identifiers.
    created = json.loads(create_result.stdout)
    assert created["agent_id"].startswith("agent-")
    assert created["host_id"].startswith("host-")

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["errors"] == []
    assert len(parsed["agents"]) == 1
    # The listed agent must be exactly the one we just created -- matching ids
    # ties the two JSON payloads together, which is how a script chains commands.
    agent = parsed["agents"][0]
    assert agent["id"] == created["agent_id"]
    assert agent["host"]["id"] == created["host_id"]
    assert agent["name"] == "my-task"
    assert agent["type"] == "command"
    assert agent["command"] == "sleep 100064"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # mngr is very much meant to be used for scripting and automation, so nothing requires interactivity.
    # if you want to be sure that interactivity is disabled, you can use the --headless flag:
    mngr create my-task --headless
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --headless -- sleep 100065",
            comment="if you want to be sure that interactivity is disabled, you can use the --headless flag",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the headless agent is actually running and reachable, not merely
    # listed: exec a command inside it and confirm it returns the agent's
    # working directory (an absolute path).
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify the headless agent is running and reachable")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_match(r"^/")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# No @pytest.mark.modal: this is a purely local create with local host tags.
# `mngr list`'s Modal discovery runs in the mngr subprocess via the in-process
# SDK (gRPC), which the resource guard only tracks inside the pytest process --
# never in a subprocess. The guard's modal binary wrapper is the only
# subprocess-tracked path, and mngr only shells out to the `modal` CLI when
# creating a Modal host (which this test never does). Marking it @modal would
# fail the NEVER_INVOKED resource-guard check once the test passes.
# The local `mngr create` routinely exceeds the global 10s pytest timeout, so
# raise it (mirrors the other e2e create tests, e.g. test_create_commands.py).
@pytest.mark.timeout(180)
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --label team=backend --host-label env=staging -- sleep 100068",
            comment="you can add labels to organize your agents and tags for host metadata",
            timeout=120.0,
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify labels appear in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
    assert matching_agents[0]["host"]["tags"]["env"] == "staging"
