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
    # The tutorial comment advertises these commands by name; verify the help
    # output actually lists each one so the tutorial's claim stays accurate.
    # (push/pull are not top-level commands -- that functionality lives under
    # the `git` command -- so they are intentionally not asserted here.)
    for command in ("create", "list", "destroy", "message", "connect", "clone"):
        expect(result.stdout).to_contain(command)


# Resolving an unknown command forces click to load every lazy subcommand to
# build suggestions, which exceeds the global 10s pytest-timeout under load.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_unknown_command_fails(e2e: E2eSession) -> None:
    # Unhappy path for the same `mngr --help` tutorial block: an unrecognized
    # command must fail and point the user back to --help for discovery.
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr definitely-not-a-real-command",
        comment="an unknown command fails and points back to --help",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("No such command")
    expect(result.stderr).to_contain("--help")


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
    # Help text is the entire purpose of --help: it must describe the command
    # and surface its documented options without emitting any stderr noise
    # (warnings, deprecation notices, etc.).
    expect(result.stderr).to_be_empty()
    expect(result.stdout).to_contain("Create and run an agent")
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")


@pytest.mark.release
def test_create_invalid_flag_fails(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    # Unhappy path for the same "learn more via --help" block: an unknown option
    # must be rejected by the argument parser (rather than silently creating an
    # agent), so users who mistype a flag get a clear error instead of surprise.
    # Argument parsing happens before any agent-creation logic runs, so a clean
    # non-zero exit with an explanatory error is the full observable effect.
    result = e2e.run(
        "mngr create --nonexistent-flag",
        comment="an unknown option is rejected rather than creating an agent",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("No such option")
    expect(result.stderr).to_contain("--nonexistent-flag")
    # The rejection is purely a parse error: nothing should be printed to stdout.
    expect(result.stdout).to_be_empty()


@pytest.mark.release
def test_list_with_no_agents(e2e: E2eSession) -> None:
    # No @pytest.mark.modal: in a fresh environment with no agents, `mngr list`
    # never creates a Modal environment and discovers via the Modal Python SDK,
    # so it never invokes the guarded `modal` CLI binary (unlike `mngr create
    # --provider modal`, which does via environment_create during provider init).
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr list --format json", comment="List agents as JSON in a fresh environment")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_named_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mngr create my-task
    # that command give the agent a name of "my-task". If you don't specify a name, mngr will generate a random one for you.
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100063",
            comment="when creating agents to accomplish tasks, it's recommended that you give them a name",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    # Beyond appearing in the list, confirm the named agent is actually running
    # by executing a command on its host -- exec resolves the agent purely by
    # the name we assigned and only succeeds against a live host.
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify the named agent is actually running")
    expect(exec_result).to_succeed()
    # pwd prints the agent's working directory as an absolute path on its own line.
    expect(exec_result.stdout).to_match(r"(?m)^/\S")


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
    # The whole point of --format json is machine-parseable output, so verify
    # the create command emits a single valid JSON object exposing the IDs a
    # script would consume.
    created = json.loads(create_result.stdout)
    assert created["agent_id"].startswith("agent-")
    assert created["host_id"].startswith("host-")

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    # The agent surfaced by `list` must be the one we just created -- the IDs
    # round-trip between the two JSON outputs, which is what makes them useful
    # for scripting.
    listed_agent = parsed["agents"][0]
    assert listed_agent["id"] == created["agent_id"]
    assert listed_agent["name"] == "my-task"
    assert listed_agent["type"] == "command"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100066",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    destroy_result = e2e.run("mngr destroy my-task --force", comment="Destroy the agent")
    expect(destroy_result).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100067",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("renamed-task")
    expect(list_result.stdout).not_to_contain("my-task")


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
