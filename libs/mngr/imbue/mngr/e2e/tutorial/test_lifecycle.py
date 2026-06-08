"""Tests for the STARTING AND STOPPING AGENTS tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


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
@pytest.mark.timeout(180)
def test_start_idempotent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent. Is idempotent, so is safe to call even if already running.
        mngr start my-task
    """)
    _create_my_task(e2e, 100500)
    # Starting an already-running agent is idempotent: it succeeds rather than erroring.
    expect(e2e.run("mngr start my-task", comment="start a stopped agent (idempotent)")).to_succeed()
    # The redundant start must not have torn the agent down: it is still reachable, and
    # exec lands in the agent's own worktree.
    reachable = e2e.run("mngr exec my-task pwd", comment="verify the agent is still reachable")
    expect(reachable).to_succeed()
    expect(reachable.stdout).to_contain("worktrees/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_start_stopped_agent(e2e: E2eSession) -> None:
    # Happy path counterpart to test_start_idempotent: the tutorial block's primary
    # case is starting an agent that is actually stopped, so stop a running agent and
    # bring it back up.
    e2e.write_tutorial_block("""
        # start a stopped agent. Is idempotent, so is safe to call even if already running.
        mngr start my-task
    """)
    _create_my_task(e2e, 100513)
    expect(e2e.run("mngr stop my-task", comment="stop the running agent").stdout).to_contain("Stopped agent: my-task")
    started = e2e.run("mngr start my-task", comment="start the now-stopped agent")
    expect(started).to_succeed()
    expect(started.stdout).to_contain("Started agent: my-task")
    # The restarted agent is reachable again.
    expect(e2e.run("mngr exec my-task pwd", comment="verify the restarted agent is reachable")).to_succeed()


# Local command agents create + start via tmux/rsync; starting a named agent
# resolves it locally and never enumerates Modal, so this test does not carry
# @pytest.mark.modal. The default 10s pytest timeout is too tight for the full
# create + start round-trip (~15s), so bump it.
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
    expect(e2e.run("mngr start my-task --connect", comment="start and immediately connect")).to_succeed()
    # --connect runs the configured connect_command. The e2e harness's
    # connect_command (mngr-e2e-connect) records the session and writes a
    # "<agent>.pid" file into MNGR_TEST_ASCIINEMA_DIR (== e2e.output_dir). The
    # connect step only runs when start actually started a stopped agent, so the
    # file's presence verifies the whole start-then-connect path -- the behavior
    # that distinguishes --connect from a plain start. This is a local,
    # filesystem-only check that (unlike `mngr list`/`mngr exec`) does not
    # enumerate remote providers, keeping the test free of Modal usage.
    assert (e2e.output_dir / "my-task.pid").exists(), (
        f"Expected --connect to invoke the connect command and write my-task.pid in {e2e.output_dir}, "
        f"but it is missing. Directory contents: {sorted(p.name for p in e2e.output_dir.iterdir())}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_start_multiple_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start multiple agents at once
        mngr start agent-1 agent-2 agent-3
    """)
    _create_named_agents(e2e, [("agent-1", 100502), ("agent-2", 100503), ("agent-3", 100504)])
    result = e2e.run("mngr start agent-1 agent-2 agent-3", comment="start multiple agents at once")
    expect(result).to_succeed()
    # The point of "start multiple agents at once" is that a single invocation
    # addresses every named agent, so assert all three appear in the output
    # rather than only checking the exit code.
    for name in ("agent-1", "agent-2", "agent-3"):
        expect(result.stdout).to_contain(name)


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_start_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start all stopped agents by simply passing their ids from "mngr list" and reading the ids from stdin (that's what the "-" means)
        mngr list --ids | mngr start -
    """)
    _create_my_task(e2e, 100505)
    # Stop the agent first so the stdin-driven start does real work (starting a
    # stopped agent), matching the tutorial's "start all stopped agents" intent.
    expect(e2e.run("mngr stop my-task", comment="stop my-task so it is actually stopped")).to_succeed()
    stopped = e2e.run("mngr list --stopped", comment="confirm my-task is stopped")
    expect(stopped).to_succeed()
    expect(stopped.stdout).to_contain("my-task")
    # start all stopped agents by piping their ids from "mngr list" into stdin.
    result = e2e.run("mngr list --ids | mngr start -", comment="start all via stdin")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("my-task")
    # Verify the start took effect: the agent is no longer in the stopped set.
    after = e2e.run("mngr list --stopped", comment="verify my-task is no longer stopped after stdin-driven start")
    expect(after).to_succeed()
    expect(after.stdout).not_to_contain("my-task")


@pytest.mark.timeout(120)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_start_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would happen without actually starting anything
        mngr list --ids | mngr start - --dry-run
    """)
    _create_my_task(e2e, 100506)

    # Capture every agent's lifecycle state before the dry-run so we can prove
    # the dry-run leaves all of them untouched.
    state_before = e2e.run("mngr list --format '{name}={state}'", comment="capture agent state before the dry-run")
    expect(state_before).to_succeed()

    dry_run = e2e.run("mngr list --ids | mngr start - --dry-run", comment="dry-run to see what would happen")
    expect(dry_run).to_succeed()
    # The dry-run reports the plan (which agents would be started) without acting.
    expect(dry_run.stdout).to_contain("Would be started")
    expect(dry_run.stdout).to_contain("my-task")

    # A dry-run must be a no-op: every agent's state is identical afterwards, so
    # nothing was actually started.
    state_after = e2e.run(
        "mngr list --format '{name}={state}'", comment="confirm the dry-run did not change any agent state"
    )
    expect(state_after).to_succeed()
    expect(state_after.stdout).to_equal(state_before.stdout)


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_stop_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop a running agent
        mngr stop my-task
    """)
    _create_my_task(e2e, 100507)
    expect(e2e.run("mngr stop my-task", comment="stop a running agent")).to_succeed()
    # Verify the stop actually took effect: my-task should now be reported as
    # stopped and should no longer appear among the running agents.
    stopped = e2e.run("mngr list --stopped", comment="verify my-task is now stopped")
    expect(stopped).to_succeed()
    assert "my-task" in stopped.stdout, f"expected my-task in stopped list, got: {stopped.stdout!r}"
    running = e2e.run("mngr list --running", comment="verify my-task is no longer running")
    expect(running).to_succeed()
    assert "my-task" not in running.stdout, f"expected my-task to not be running, got: {running.stdout!r}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_stop_archive(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop and archive the agent (marks it archived so it can be filtered out of listings; its state is preserved).
        mngr stop my-task --archive
    """)
    _create_my_task(e2e, 100508)
    stop_result = e2e.run("mngr stop my-task --archive", comment="stop and archive the agent")
    expect(stop_result).to_succeed()
    # --archive both stops the agent and sets the 'archived_at' label.
    expect(stop_result.stdout).to_contain("Stopped agent: my-task")

    # The agent is now archived: it carries the 'archived_at' label and so
    # shows up under --archived. List queries are scoped to the local provider
    # to avoid enumerating remote providers (which the test never uses).
    archived_result = e2e.run("mngr list --provider local --archived", comment="verify my-task is now archived")
    expect(archived_result).to_succeed()
    expect(archived_result.stdout).to_contain("my-task")

    # Archived agents are excluded from --active, confirming the archive label
    # filters the agent out of normal listings without destroying it.
    active_result = e2e.run(
        "mngr list --provider local --active", comment="verify my-task is excluded from active agents"
    )
    expect(active_result).to_succeed()
    expect(active_result.stdout).not_to_contain("my-task")


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
    # The archive command only archives non-running agents; in the tutorial flow
    # my-task has already been stopped (see "mngr stop my-task --archive" just
    # above this block), so stop it first to mirror that state.
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archiving")).to_succeed()
    expect(e2e.run("mngr archive my-task", comment="archive shortcut for stop --archive")).to_succeed()
    # Archiving sets an "archived_at" label; verify the agent is actually
    # archived rather than just trusting the command's exit code.
    list_result = e2e.run("mngr list --archived --format json", comment="verify my-task is archived")
    expect(list_result).to_succeed()
    archived_agents = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(archived_agents) == 1, f"expected my-task in archived list, got {list_result.stdout}"
    assert "archived_at" in archived_agents[0]["labels"], archived_agents[0]["labels"]


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_archive_running_agent_is_skipped(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
        mngr archive my-task
    """)
    _create_my_task(e2e, 100513)
    # Unhappy path: without --force, archiving a *running* agent is a no-op. The
    # agent is skipped with a warning and the archived_at label is NOT applied.
    result = e2e.run("mngr archive my-task", comment="archive a running agent (skipped without --force)")
    expect(result).to_succeed()
    expect(result.stdout + result.stderr).to_contain("Skipping running agent")
    # Confirm nothing was archived.
    list_result = e2e.run("mngr list --archived --format json", comment="verify my-task was not archived")
    expect(list_result).to_succeed()
    assert not [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"], list_result.stdout


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_stop_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop all running agents
        mngr list --ids | mngr stop -
    """)
    _create_my_task(e2e, 100510)
    stop_result = e2e.run("mngr list --ids | mngr stop -", comment="stop all running agents")
    expect(stop_result).to_succeed()
    # The command reports which agents it stopped.
    expect(stop_result.stdout).to_contain("my-task")
    # Verify the concrete effect: the agent is no longer running, but still
    # exists in a stopped state (stop is not destroy/archive).
    running_after = e2e.run("mngr list --running", comment="verify nothing is left running")
    expect(running_after).to_succeed()
    expect(running_after.stdout).not_to_contain("my-task")
    stopped_after = e2e.run("mngr list --stopped", comment="verify the agent is now stopped")
    expect(stopped_after).to_succeed()
    expect(stopped_after.stdout).to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_archive_stopped_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # archive all stopped agents (handy for cleaning up "mngr list" after a batch of finished work).
        mngr list --stopped --ids | mngr archive -
    """)
    _create_my_task(e2e, 100511)
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archive")).to_succeed()

    # Precondition: my-task is stopped and not yet archived (no archived_at label).
    stopped_before = e2e.run("mngr list --stopped", comment="confirm my-task is stopped before archiving")
    expect(stopped_before).to_succeed()
    expect(stopped_before.stdout).to_match(r"my-task\s+STOPPED")
    archived_before = e2e.run("mngr list --archived", comment="confirm my-task is not yet archived")
    expect(archived_before).to_succeed()
    expect(archived_before.stdout).not_to_contain("my-task")

    expect(
        e2e.run(
            "mngr list --stopped --ids | mngr archive -",
            comment="archive all stopped agents",
        )
    ).to_succeed()

    # Effect: archiving applies the archived_at label, so my-task now shows up
    # under --archived and is filtered out of the cleaned-up --active listing.
    archived_after = e2e.run("mngr list --archived", comment="my-task now appears as archived")
    expect(archived_after).to_succeed()
    expect(archived_after.stdout).to_contain("my-task")
    active_after = e2e.run("mngr list --active", comment="my-task is filtered out of the active listing")
    expect(active_after).to_succeed()
    expect(active_after.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_stop_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be stopped
        mngr list --ids | mngr stop - --dry-run
    """)
    _create_my_task(e2e, 100512)
    dry_run_result = e2e.run("mngr list --ids | mngr stop - --dry-run", comment="dry-run to see what would be stopped")
    expect(dry_run_result).to_succeed()
    # The dry-run must report the agent that would be stopped...
    expect(dry_run_result.stdout).to_contain("Would stop")
    expect(dry_run_result.stdout).to_contain("my-task")
    # ...without actually stopping it.
    expect(dry_run_result.stdout).not_to_contain("Stopped agent")

    # Confirm the dry-run left the agent running: a real stop still finds and
    # stops it (it would report nothing to stop had the dry-run stopped it).
    real_stop_result = e2e.run("mngr stop my-task", comment="verify dry-run left the agent running")
    expect(real_stop_result).to_succeed()
    expect(real_stop_result.stdout).to_contain("Stopped agent: my-task")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_stop_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    # The tutorial's "my-session-name" placeholder lacks the configured tmux
    # session prefix, so mngr should reject it via the --session format guard:
    # a clear validation error and a non-zero exit, cleanly (no Python
    # traceback) rather than crashing.
    result = e2e.run(
        "mngr stop --session my-session-name",
        comment="stop variant that finds an agent by tmux session name",
    )
    combined_output = result.stdout + result.stderr
    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}: {combined_output}"
    assert "Traceback" not in combined_output, f"mngr crashed instead of exiting cleanly: {combined_output}"
    # The error should explain *why* the session was rejected (prefix mismatch).
    assert "does not match the expected format" in combined_output, combined_output
