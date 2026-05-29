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
    # The tutorial comment promises the user they can discover the other
    # commands here. Verify that the top-level commands it advertises (push and
    # pull are covered as `mngr git` subcommands, not top-level) are actually
    # listed in the help output, so the tutorial's claim stays accurate.
    for command in ("create", "list", "destroy", "message", "connect", "clone", "git"):
        expect(result.stdout).to_contain(command)


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
    # The help text describes the command and documents its many arguments.
    expect(result.stdout).to_contain("Create and run an agent")
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")


@pytest.mark.release
def test_create_with_unknown_flag_fails(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: passing an option that is not
    # among the documented arguments must fail with a usage error that points
    # the user back toward the help output.
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create --this-flag-does-not-exist",
        comment="an undocumented option is rejected with a usage error",
    )
    expect(result).to_fail()
    # Click reports unknown options with exit code 2.
    expect(result).to_have_exit_code(2)
    expect(result.stderr).to_contain("No such option: --this-flag-does-not-exist")
    expect(result.stderr).to_contain("Usage:")


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
        timeout=90.0,
    )
    expect(create_result).to_succeed()

    # The whole point of --format json is machine-readable stdout: it must be a
    # single JSON object carrying the new agent's identifiers (and nothing that
    # would break a `json.loads` in a caller's script).
    created = json.loads(create_result.stdout)
    assert created["agent_id"].startswith("agent-")
    assert created["host_id"].startswith("host-")

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    # The listed agent must be the one we just created, by both name and id.
    listed = parsed["agents"][0]
    assert listed["name"] == "my-task"
    assert listed["id"] == created["agent_id"]


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_quiet_output(e2e: E2eSession) -> None:
    # Covers the `--quiet` line of the same tutorial block: --quiet suppresses
    # all output, yet the agent must still be created.
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --quiet -- sleep 100064",
        comment="--quiet suppresses all output",
        timeout=90.0,
    )
    expect(create_result).to_succeed()
    # --quiet must produce no stdout at all (so it never pollutes a script's pipe).
    assert create_result.stdout == ""

    # Despite the silence, the agent must really exist.
    list_result = e2e.run("mngr list --format json", comment="Verify the quiet-created agent exists")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert [a["name"] for a in parsed["agents"]] == ["my-task"]


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

    # The point of --headless is that the agent is fully usable for scripting
    # and automation without any interactivity. Drive it non-interactively via
    # mngr exec to confirm it was actually created and is reachable -- a clean
    # exit and the echoed marker prove the headless agent is operational.
    exec_result = e2e.run(
        'mngr exec my-task "echo headless-ok"',
        comment="drive the headless agent non-interactively (no interactivity required)",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("headless-ok")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --label team=backend --host-label env=staging -- sleep 100068",
            comment="you can add labels to organize your agents and tags for host metadata",
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
