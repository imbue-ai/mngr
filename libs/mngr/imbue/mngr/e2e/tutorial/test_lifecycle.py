"""Tests for the STARTING AND STOPPING AGENTS tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import json
from typing import Any

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _get_agent_details(e2e: E2eSession, agent_name: str) -> dict[str, Any]:
    """Return the parsed `mngr list --format json` details for a single agent."""
    list_result = e2e.run("mngr list --format json", comment=f"inspect {agent_name} state and labels")
    expect(list_result).to_succeed()
    matching = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == agent_name]
    assert len(matching) == 1, f"Expected exactly one agent named {agent_name!r}, got: {matching}"
    return matching[0]
def _get_local_agent_state(e2e: E2eSession, name: str) -> str | None:
    """Return the lifecycle state of a local agent by name, or None if absent.

    Queries only the local provider (``--provider local`` is a fan-out control,
    not a post-discovery filter) so the lookup never touches remote providers
    like Modal.
    """
    result = e2e.run(
        "mngr list --provider local --format json",
        comment=f"look up local agent state for {name}",
    )
    expect(result).to_succeed()
    agents = json.loads(result.stdout)["agents"]
    for agent in agents:
        if agent["name"] == name:
            return agent["state"]
    return None


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


def _create_named_agents(e2e: E2eSession, names_and_sleeps: list[tuple[str, int]]) -> None:
    for name, sleep_value in names_and_sleeps:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_start_idempotent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent. Is idempotent, so is safe to call even if already running.
        mngr start my-task
    """)
    _create_my_task(e2e, 100500)
    expect(e2e.run("mngr start my-task", comment="start a stopped agent (idempotent)")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_start_connect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent and immediately connect to it
        mngr start my-task --connect
    """)
    _create_my_task(e2e, 100501)
    # Stop the agent first so that the start command genuinely starts a *stopped*
    # agent (create auto-starts it), matching the tutorial block's description.
    expect(e2e.run("mngr stop my-task", comment="stop my-task so start has a stopped agent to start")).to_succeed()
    expect(e2e.run("mngr start my-task --connect", comment="start and immediately connect")).to_succeed()
    # Verify the agent is actually running again after start by executing a
    # command inside it: exec only succeeds against a running, reachable agent.
    exec_result = e2e.run("mngr exec my-task 'echo started-ok'", comment="verify the agent is running after start")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("started-ok")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_start_multiple_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start multiple agents at once
        mngr start agent-1 agent-2 agent-3
    """)
    names_and_sleeps = [("agent-1", 100502), ("agent-2", 100503), ("agent-3", 100504)]
    _create_named_agents(e2e, names_and_sleeps)
    # Stop all three first so that the start command below has real work to do
    # (starting an already-running agent is a no-op; see test_start_idempotent).
    expect(e2e.run("mngr stop agent-1 agent-2 agent-3", comment="stop the agents before restarting them")).to_succeed()
    expect(e2e.run("mngr start agent-1 agent-2 agent-3", comment="start multiple agents at once")).to_succeed()
    # Verify the command actually started all three agents, not just exited 0.
    result = e2e.run("mngr list --format json", comment="verify all three agents are running again")
    expect(result).to_succeed()
    agents_by_name = {agent["name"]: agent for agent in json.loads(result.stdout)["agents"]}
    for name, _ in names_and_sleeps:
        assert name in agents_by_name, f"Expected {name} in agent list, got: {sorted(agents_by_name)}"
        assert agents_by_name[name]["state"] in ("RUNNING", "WAITING"), (
            f"Expected {name} to be running after start, got: {agents_by_name[name]['state']}"
        )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_start_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start all stopped agents by simply passing their ids from "mngr list" and reading the ids from stdin (that's what the "-" means)
        mngr list --ids | mngr start -
    """)
    _create_my_task(e2e, 100505)
    # Stop the agent first so the stdin-driven start has to actually restart a
    # stopped agent -- the exact scenario the tutorial describes ("start all
    # stopped agents").
    expect(e2e.run("mngr stop my-task", comment="stop my-task so it is stopped before the bulk start")).to_succeed()
    start_result = e2e.run("mngr list --ids | mngr start -", comment="start all via stdin")
    expect(start_result).to_succeed()
    # The id piped from "mngr list --ids" must have driven a real start, so the
    # started agent is named in the output (not an empty no-op).
    expect(start_result.stdout).to_contain("my-task")
    # Verify the actual effect: the agent is running again after the bulk start.
    list_result = e2e.run("mngr list", comment="verify the agent is running after the stdin-driven start")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_start_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would happen without actually starting anything
        mngr list --ids | mngr start - --dry-run
    """)
    _create_my_task(e2e, 100506)
    # Stop the agent so the dry-run has a deterministic stopped candidate to
    # report (start only considers stopped agents). This is idempotent: if the
    # agent is already stopped, stop is a no-op that still exits 0.
    expect(e2e.run("mngr stop my-task", comment="stop my-task so it is a start candidate")).to_succeed()

    result = e2e.run("mngr list --ids | mngr start - --dry-run", comment="dry-run to see what would happen")
    expect(result).to_succeed()
    # The dry-run must report that the stopped agent would be started, rather
    # than silently doing nothing.
    assert "my-task" in result.stdout, f"dry-run did not report my-task as a start candidate:\n{result.stdout}"

    # The dry-run must NOT actually start anything: my-task should still be
    # stopped afterward.
    still_stopped = e2e.run(
        "mngr list --stopped --format '{name}'",
        comment="confirm my-task is still stopped after the dry-run",
    )
    expect(still_stopped).to_succeed()
    assert "my-task" in still_stopped.stdout, (
        f"dry-run appears to have started my-task (no longer stopped):\n{still_stopped.stdout}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_stop_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop a running agent
        mngr stop my-task
    """)
    _create_my_task(e2e, 100507)
    # The freshly created agent should be alive (RUNNING or WAITING) before we stop it.
    assert _get_local_agent_state(e2e, "my-task") in ("RUNNING", "WAITING")
    expect(e2e.run("mngr stop my-task", comment="stop a running agent")).to_succeed()
    # Verify the actual effect: the agent transitioned to STOPPED rather than
    # only trusting the command's exit code.
    assert _get_local_agent_state(e2e, "my-task") == "STOPPED"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_stop_archive(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop and archive the agent (creates a snapshot before stopping).
        mngr stop my-task --archive
    """)
    _create_my_task(e2e, 100508)
    expect(e2e.run("mngr stop my-task --archive", comment="stop and archive the agent")).to_succeed()

    # Archiving stops the agent and tags it with an "archived_at" label, so it
    # should now appear under "mngr list --archived" in the STOPPED state.
    archived_result = e2e.run("mngr list --archived --format json", comment="verify my-task is now archived")
    expect(archived_result).to_succeed()
    archived = [a for a in json.loads(archived_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(archived) == 1, f"Expected my-task among archived agents, got: {archived_result.stdout}"
    assert archived[0]["state"] == "STOPPED", f"Expected archived agent to be STOPPED, got: {archived[0]['state']}"
    assert "archived_at" in archived[0]["labels"], f"Expected an archived_at label, got: {archived[0]['labels']}"

    # An archived agent is no longer "active", so it must be excluded from "mngr list --active".
    active_result = e2e.run("mngr list --active --format json", comment="verify my-task is no longer active")
    expect(active_result).to_succeed()
    active_names = [a["name"] for a in json.loads(active_result.stdout)["agents"]]
    assert "my-task" not in active_names, f"Expected my-task excluded from active agents, got: {active_names}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_archive_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
        mngr archive my-task
    """)
    _create_my_task(e2e, 100509)
    # `mngr archive` only archives non-running agents by default. In the tutorial
    # sequence my-task has already been stopped by this point (see the earlier
    # `mngr stop my-task` / `mngr stop my-task --archive` blocks), so stop it
    # first to mirror that context. The running-agent path is covered by
    # test_archive_command_force.
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archiving")).to_succeed()
    expect(e2e.run("mngr archive my-task", comment="archive shortcut for stop --archive")).to_succeed()
    # Verify the archive actually took effect: the agent must carry the
    # 'archived_at' label and remain present (archived, not destroyed).
    details = _get_agent_details(e2e, "my-task")
    assert "archived_at" in details["labels"], f"Expected an archived_at label, got labels: {details['labels']}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_archive_command_force(e2e: E2eSession) -> None:
    # Shares the same tutorial block as test_archive_command, but exercises the
    # running-agent paths: by default `mngr archive` skips running agents, and
    # --force stops them first -- the true equivalent of `mngr stop --archive`.
    e2e.write_tutorial_block("""
        # you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
        mngr archive my-task
    """)
    _create_my_task(e2e, 100513)
    # Unhappy path: archiving a still-running agent without --force is a no-op.
    # It exits 0 but warns and skips, and the agent must NOT become archived.
    skip_result = e2e.run("mngr archive my-task", comment="archiving a running agent is skipped without --force")
    expect(skip_result).to_succeed()
    expect(skip_result.stdout + skip_result.stderr).to_contain("Skipping running agent")
    before = _get_agent_details(e2e, "my-task")
    assert "archived_at" not in before["labels"], f"Running agent should not be archived yet, got: {before['labels']}"
    # The agent is still live (not stopped) -- a command agent idles in WAITING
    # rather than RUNNING, but either way it must not have been stopped.
    assert before["state"] != "STOPPED", f"Expected my-task to still be live, got: {before['state']}"
    # Happy path: --force stops the running agent and then archives it.
    expect(e2e.run("mngr archive my-task --force", comment="force-stop and archive a running agent")).to_succeed()
    after = _get_agent_details(e2e, "my-task")
    assert "archived_at" in after["labels"], f"Expected an archived_at label after --force, got: {after['labels']}"
    assert after["state"] == "STOPPED", f"Expected my-task to be STOPPED after --force, got: {after['state']}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_stop_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop all running agents
        mngr list --ids | mngr stop -
    """)
    # Create several running agents so the "stop all" fan-out has more than one target.
    _create_named_agents(e2e, [("task-a", 100510), ("task-b", 100513)])
    # stop all running agents
    expect(e2e.run("mngr list --ids | mngr stop -", comment="stop all running agents")).to_succeed()
    # Verify every agent actually transitioned to STOPPED, not just that the command exited 0.
    list_after = e2e.run("mngr list", comment="verify all agents are STOPPED")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_match(r"task-a\s+STOPPED")
    expect(list_after.stdout).to_match(r"task-b\s+STOPPED")


@pytest.mark.release
def test_stop_all_via_stdin_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop all running agents
        mngr list --ids | mngr stop -
    """)
    # Edge case: with no agents at all, "mngr list --ids" emits nothing, so "mngr stop -"
    # reads an empty id list from stdin. This must succeed cleanly (an empty stdin is a
    # no-op), not raise the "Must specify at least one agent" usage error that bare
    # "mngr stop" raises.
    expect(
        e2e.run("mngr list --ids | mngr stop -", comment="stop all when there are no agents")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_archive_stopped_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # archive all stopped agents (handy for cleaning up "mngr list" after a batch of finished work).
        mngr list --stopped --ids | mngr archive -
    """)
    _create_my_task(e2e, 100511)
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archive")).to_succeed()
    expect(
        e2e.run(
            "mngr list --stopped --ids | mngr archive -",
            comment="archive all stopped agents",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_stop_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be stopped
        mngr list --ids | mngr stop - --dry-run
    """)
    _create_my_task(e2e, 100512)
    dry_run_result = e2e.run(
        "mngr list --ids | mngr stop - --dry-run", comment="dry-run to see what would be stopped"
    )
    expect(dry_run_result).to_succeed()
    # The dry-run must report the agent that would be stopped...
    expect(dry_run_result.stdout).to_contain("Would stop")
    expect(dry_run_result.stdout).to_contain("my-task")
    # ...but must not actually stop anything.
    expect(dry_run_result.stdout).not_to_contain("Stopped agent")
    # The core dry-run guarantee: the agent was left running, so a subsequent
    # real stop still finds it and stops it (rather than reporting that there
    # are no running agents to stop).
    real_stop = e2e.run("mngr stop my-task", comment="verify a real stop still finds the agent running")
    expect(real_stop).to_succeed()
    expect(real_stop.stdout).to_contain("Stopped agent: my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_stop_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    # An agent's tmux session name is the configured prefix followed by the
    # agent name (see create.py), so the running "my-task" agent is reachable
    # via "${MNGR_PREFIX}my-task". Exercise the documented behavior end to end:
    # stop a real running agent by its session name and confirm it stopped.
    _create_my_task(e2e, 100513)
    expect(
        e2e.run(
            'mngr stop --session "${MNGR_PREFIX}my-task"',
            comment="stop variant that finds an agent by tmux session name",
        )
    ).to_succeed()
    # Verify the actual effect, not just the exit code: my-task is now STOPPED.
    listing = e2e.run("mngr list --format json", comment="confirm my-task is now stopped")
    expect(listing).to_succeed()
    states_by_name = {agent["name"]: agent["state"] for agent in json.loads(listing.stdout)["agents"]}
    assert states_by_name.get("my-task") == "STOPPED", (
        f"expected my-task to be STOPPED after stop --session, got: {states_by_name}"
    )


@pytest.mark.release
def test_stop_by_session_name_invalid_prefix(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    # A session name that does not start with the configured prefix cannot be
    # mapped to an agent, so mngr rejects it with a clear error before touching
    # any provider (hence no @pytest.mark.modal: this path never reaches Modal).
    result = e2e.run(
        "mngr stop --session my-session-name",
        comment="stop with a session name that does not match the configured prefix",
    )
    expect(result).to_fail()
    assert "does not match the expected format" in (result.stdout + result.stderr), (
        f"expected a prefix-format error, got stdout={result.stdout!r} stderr={result.stderr!r}"
    )
