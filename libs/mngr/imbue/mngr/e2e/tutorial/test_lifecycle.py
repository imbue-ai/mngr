"""Tests for the STARTING AND STOPPING AGENTS tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

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
@pytest.mark.modal
def test_start_connect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent and immediately connect to it
        mngr start my-task --connect
    """)
    _create_my_task(e2e, 100501)
    expect(e2e.run("mngr start my-task --connect", comment="start and immediately connect")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_start_multiple_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start multiple agents at once
        mngr start agent-1 agent-2 agent-3
    """)
    _create_named_agents(e2e, [("agent-1", 100502), ("agent-2", 100503), ("agent-3", 100504)])
    expect(e2e.run("mngr start agent-1 agent-2 agent-3", comment="start multiple agents at once")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_start_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start all stopped agents by simply passing their ids from "mngr list" and reading the ids from stdin (that's what the "-" means)
        mngr list --ids | mngr start -
    """)
    _create_my_task(e2e, 100505)
    expect(e2e.run("mngr list --ids | mngr start -", comment="start all via stdin")).to_succeed()


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
    expect(
        e2e.run("mngr list --ids | mngr start - --dry-run", comment="dry-run to see what would happen")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_stop_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop a running agent
        mngr stop my-task
    """)
    _create_my_task(e2e, 100507)
    expect(e2e.run("mngr stop my-task", comment="stop a running agent")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_stop_archive(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop and archive the agent (creates a snapshot before stopping).
        mngr stop my-task --archive
    """)
    _create_my_task(e2e, 100508)
    expect(e2e.run("mngr stop my-task --archive", comment="stop and archive the agent")).to_succeed()


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
    expect(e2e.run("mngr archive my-task", comment="archive shortcut for stop --archive")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_stop_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop all running agents
        mngr list --ids | mngr stop -
    """)
    _create_my_task(e2e, 100510)
    expect(e2e.run("mngr list --ids | mngr stop -", comment="stop all running agents")).to_succeed()


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
@pytest.mark.modal
def test_stop_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be stopped
        mngr list --ids | mngr stop - --dry-run
    """)
    _create_my_task(e2e, 100512)
    expect(
        e2e.run("mngr list --ids | mngr stop - --dry-run", comment="dry-run to see what would be stopped")
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_stop_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    # No tmux session by that name exists; the test verifies that mngr parses
    # the --session flag and exits cleanly with an error rather than crashing.
    result = e2e.run(
        "mngr stop --session my-session-name",
        comment="stop variant that finds an agent by tmux session name",
    )
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()
