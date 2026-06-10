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
    # must actually list them.
    for command in ("create", "list", "destroy", "message", "connect", "clone"):
        expect(result.stdout).to_contain(command)
    # The remaining advertised commands -- push and pull -- are folded into the
    # `git` command. Rather than dropping them with a bare comment, assert that
    # the `git` command is listed and that its summary actually describes the
    # advertised push/pull functionality, so it stays discoverable from --help.
    expect(result.stdout).to_contain("git")
    expect(result.stdout).to_contain("Push or pull git commits")


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
    # Click/Typer use exit code 2 for usage errors; the unknown command is
    # rejected at argument-parsing time, mirroring test_create_rejects_unknown_option.
    expect(result).to_have_exit_code(2)
    # The error and usage hint go to stderr, leaving stdout clean for scripting.
    expect(result.stdout).to_be_empty()
    # The error must name the offending command (not just a generic failure) and
    # point the user back to `mngr --help`, as the shared tutorial block teaches.
    expect(result.stderr).to_contain("No such command 'definitely-not-a-real-command'")
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
    # This is genuinely the `create` command's help, not some other command's:
    # the NAME line carries create's summary and the synopsis names the command.
    expect(result.stdout).to_contain("mngr create - Create and run an agent")
    expect(result.stdout).to_contain("SYNOPSIS")
    # The tutorial promises "tons more arguments", so the help must actually
    # document them under an OPTIONS section (the structural backbone of the
    # man-page-style help) and list a representative spread of the flags.
    expect(result.stdout).to_contain("OPTIONS")
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")
    # The man-page-style help renders concrete usage examples, not just an
    # option list -- this section is what makes the command discoverable. It
    # must contain real runnable `mngr create` invocations, not just a heading.
    expect(result.stdout).to_contain("EXAMPLES")
    expect(result.stdout).to_match(r"\$ mngr create")


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
    # The rejection must be a clean usage error, not an unhandled crash: a Python
    # traceback leaking to stderr would mean argument parsing blew up rather than
    # failing gracefully.
    expect(result.stderr).not_to_contain("Traceback (most recent call last)")


@pytest.mark.release
# The unknown option is rejected at parse time so no host work happens, but the
# `mngr list` verification below performs remote discovery that routinely exceeds
# the global 10s pytest timeout, so raise it like the other create tests above.
@pytest.mark.timeout(120)
def test_create_rejects_unknown_option_with_valid_args(e2e: E2eSession) -> None:
    """Same `mngr create` block, realistic typo scenario: a user takes an
    otherwise-valid invocation (a positional name plus valid flags) and adds one
    misspelled option. Click rejects the whole command at parse time before the
    create callback runs, so the unknown option poisons the invocation regardless
    of the valid arguments around it -- nothing is created.
    """
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create my-task --no-connect --this-flag-does-not-exist",
        comment="an unknown option is rejected even alongside otherwise-valid arguments",
    )
    # Parsing aborts on the unknown option even though `my-task` and `--no-connect`
    # are valid, so the exit code and clean-stderr contract match the bare case.
    expect(result).to_have_exit_code(2)
    expect(result.stdout).to_be_empty()
    expect(result.stderr).to_contain("No such option: --this-flag-does-not-exist")
    expect(result.stderr).to_contain("Usage: mngr create")
    expect(result.stderr).not_to_contain("Traceback (most recent call last)")

    # The claim above -- that the option is rejected before any host/agent work --
    # must hold concretely: the failed create must leave nothing behind, so the
    # listing is empty.
    list_result = e2e.run("mngr list --format json", comment="Verify no agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["agents"] == []


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

    # The agent is created on the local provider, so verification scopes
    # `mngr list` to `--provider local`. An unscoped `mngr list` fans out to every
    # configured provider (Modal, Docker, ...); when one is unreachable it aborts
    # the whole listing (the default --on-error abort), and even when reachable a
    # slow remote provider can exceed the per-test timeout. This mirrors the
    # convention used by the other create tests (e.g. test_agent_types).
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify agent appears in JSON list")
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
def test_create_quiet_suppresses_output(e2e: E2eSession) -> None:
    """Covers the second half of the same tutorial block: the `--quiet` comment.

    `--quiet` promises to suppress *all* console output, so a successful create
    must leave both stdout and stderr empty while still actually creating the
    agent. This is the counterpart to test_create_with_json_output, which
    exercises the `--format json` half of the block.
    """
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --quiet -- sleep 100066",
        comment="--quiet suppresses all output",
    )
    expect(create_result).to_succeed()
    # The whole point of --quiet is that a successful create prints nothing at
    # all -- not the JSON result on stdout, nor the progress/warning lines that
    # otherwise go to stderr.
    expect(create_result.stdout).to_be_empty()
    expect(create_result.stderr).to_be_empty()

    # Suppressing output must not suppress the work: the agent must still exist.
    # `mngr list` output is unaffected by the create command's --quiet flag.
    # Scope discovery to the local provider (the quiet agent is a local `command`
    # agent): this verifies exactly the provider that owns the agent and avoids an
    # incidental hard dependency on other backends (e.g. Docker) being installed,
    # which this test does not require (note the absence of @pytest.mark.docker).
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="Verify the quiet-created agent still exists"
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["errors"] == []
    assert len(parsed["agents"]) == 1
    agent = parsed["agents"][0]
    assert agent["name"] == "my-task"
    assert agent["type"] == "command"
    assert agent["command"] == "sleep 100066"


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
    # working directory. The default (worktree) create mode places each agent in
    # its own git worktree at <host_dir>/worktrees/<agent-name>-<uuid> (see
    # create_work_dir's worktree mode in hosts/host.py), so the reported pwd must
    # be an absolute path naming this agent's dedicated worktree. Matching that
    # exact shape is a stronger check than "the path is absolute": it confirms
    # the headless create gave the agent an isolated, agent-named workspace.
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify the headless agent is running and reachable")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_match(r"^/.*/my-task-[0-9a-f]+")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This test stays on the local provider throughout (the create is a local
# command agent and the verification below scopes `mngr list` to
# --provider local), so it never queries Modal/Docker and is intentionally not
# marked @pytest.mark.modal.
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

    # Scope discovery to the local provider where this command agent actually
    # lives: --provider restricts which providers are queried (unlike the
    # --local result filter, which still fans out to remote providers). This
    # keeps the check fast and never depends on Docker/Modal being reachable.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify labels appear in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
    assert matching_agents[0]["host"]["tags"]["env"] == "staging"


@pytest.mark.release
# No host work happens (create fails at label parsing, and `mngr list --provider
# local` creates nothing), so no tmux/rsync markers. The local-only listing also
# keeps this off the remote-discovery path, but it can still exceed the global
# 10s pytest timeout, so raise it like the other create tests above.
@pytest.mark.timeout(120)
def test_create_rejects_malformed_label(e2e: E2eSession) -> None:
    """Unhappy path for the same `mngr create --label` block: a label missing the
    `=` separator is not in KEY=VALUE format, so create must fail with a clear
    error before any agent is created.
    """
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --label team -- sleep 100069",
        comment="a label that is not in KEY=VALUE format is rejected",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("KEY=VALUE")

    # The malformed input must be rejected before any agent is created -- nothing
    # should be left behind in the listing. `mngr create` defaults to the local
    # provider, so a leaked agent could only appear there; scope the listing to
    # `--provider local` to verify this without depending on the health of the
    # remote providers (docker/modal) the e2e fixture leaves enabled.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify no agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert [a for a in parsed["agents"] if a["name"] == "my-task"] == []
