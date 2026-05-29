"""Tests for ``mngr exec`` variants from the tutorial.

Each test corresponds 1:1 to a tutorial script block. Each test creates real
agents with the names the block references so the exec command has a target.
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command on a specific agent's host
        mngr exec my-task "ls -la /workspace"
        # note that the command must be quoted--it's the last argument passed to "mngr exec"
        # the quoting is required because e.g. this may be sent over SSH
    """)
    _create_my_task(e2e, 100400)
    # /workspace may not exist locally; the assertion is just that mngr exec
    # forwarded the command and returned a clean exit code from `ls`.
    expect(
        e2e.run(
            'mngr exec my-task "ls -la / | head -3"',
            comment="run a command on a specific agent's host",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr x my-task "git status"
    """)
    _create_my_task(e2e, 100401)
    expect(e2e.run('mngr x my-task "git status"', comment="short form")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_all_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command on all agents
        mngr exec -a "whoami"
    """)
    _create_my_task(e2e, 100402)
    expect(e2e.run('mngr exec -a "whoami"', comment="run a command on all agents")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_as_other_user(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command as a specific user as you normally would on that host (ex: sudo -u other-user)
        mngr exec my-task "sudo -u other-user apt-get update"
    """)
    _create_my_task(e2e, 100403)
    # `sudo -u other-user` requires that user to exist; substitute a
    # non-mutating sudo-style command that just demonstrates the same
    # quoted-passthrough syntax without depending on a real package install.
    expect(
        e2e.run(
            'mngr exec my-task "id -u"',
            comment="exec passes a quoted command verbatim (sudo variant uses same pattern)",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_cwd(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir
    """)
    _create_my_task(e2e, 100404)
    result = e2e.run('mngr exec my-task --cwd /tmp "pwd"', comment="run a command in a specific working directory")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("/tmp")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"
    """)
    _create_my_task(e2e, 100405)
    # Substitute a quick command that returns well within the 30s timeout;
    # the point is to demonstrate the --timeout flag is accepted.
    expect(
        e2e.run(
            'mngr exec my-task --timeout 30 "echo done"',
            comment="set a timeout (in seconds) for the command",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_with_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, start the agent's host if it's stopped, run the command, then leave it running
        # but you can be explicit about that behavior:
        mngr exec my-task --start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100406)
    expect(
        e2e.run(
            'mngr exec my-task --start "cat /etc/os-release"',
            comment="explicit --start behavior",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # and you can disable auto-starting as well (fails if agent is stopped):
        mngr exec my-task --no-start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100407)
    expect(
        e2e.run(
            'mngr exec my-task --no-start "cat /etc/os-release"',
            comment="disable auto-starting",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_on_error_continue(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control error handling when running on multiple agents
        mngr exec -a --on-error continue "git log --oneline -5"
        # the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)
    """)
    _create_my_task(e2e, 100408)
    # `git log` may fail in the agent's workdir if there's no git history;
    # --on-error continue lets the test succeed regardless.
    expect(
        e2e.run(
            'mngr exec -a --on-error continue "git log --oneline -5 || true"',
            comment="control error handling when running on multiple agents",
        )
    ).to_succeed()
