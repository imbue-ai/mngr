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
def test_exec_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command on a specific agent's host
        mngr exec my-task "ls -la /workspace"
        # note that the command must be quoted--it's the last argument passed to "mngr exec"
        # the quoting is required because e.g. this may be sent over SSH
    """)
    _create_my_task(e2e, 100400)
    # /workspace may not exist on the agent's host, so list `/` instead. Beyond
    # a clean exit code, assert that the command's stdout was actually forwarded
    # back from the host: `ls -la` always emits a leading "total" line, so its
    # presence proves exec ran the command and returned its output (not just a
    # zero exit code from an empty/short-circuited invocation). The pipe runs on
    # the host because the whole quoted string is sent there as one command.
    result = e2e.run(
        'mngr exec my-task "ls -la / | head -3"',
        comment="run a command on a specific agent's host",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("total")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr x my-task "git status"
    """)
    _create_my_task(e2e, 100401)
    # ``my-task`` is a local command agent, so neither create nor exec ever
    # provisions a Modal environment -- there is no @pytest.mark.modal because
    # the modal resource guard is never tripped (the modal CLI is only invoked
    # via environment_create when creating a remote/Modal-backed agent).
    result = e2e.run('mngr x my-task "git status"', comment="short form")
    expect(result).to_succeed()
    # ``mngr x`` is the documented short form of ``mngr exec``; verify it
    # actually ran git inside the agent's work_dir by observing real git
    # status output rather than relying solely on the exit code. The agent
    # runs on its own ``mngr/my-task`` branch, which git status reports.
    expect(result.stdout).to_contain("On branch")


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
@pytest.mark.timeout(120)
def test_exec_as_other_user(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command as a specific user as you normally would on that host (ex: sudo -u other-user)
        mngr exec my-task "sudo -u other-user apt-get update"
    """)
    _create_my_task(e2e, 100403)
    # `sudo -u other-user` requires that user to exist; substitute a
    # non-mutating sudo-style command that just demonstrates the same
    # quoted-passthrough syntax without depending on a real package install.
    result = e2e.run(
        'mngr exec my-task "id -u"',
        comment="exec passes a quoted command verbatim (sudo variant uses same pattern)",
    )
    expect(result).to_succeed()
    # Verify exec actually ran the quoted command on the agent host and streamed
    # back its real output: `id -u` prints a numeric uid on its own line.
    expect(result.stdout).to_match(r"(?m)^\s*\d+\s*$")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_exec_cwd(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir
    """)
    _create_my_task(e2e, 100404)
    # With --cwd, the command runs in the given directory: `pwd` prints exactly /tmp.
    result = e2e.run('mngr exec my-task --cwd /tmp "pwd"', comment="run a command in a specific working directory")
    expect(result).to_succeed()
    expect(result.stdout).to_match(r"(?m)^/tmp$")
    # Without --cwd, the command runs in the agent's work_dir, which is not /tmp.
    # This confirms --cwd actually changed the directory rather than matching a
    # default that happened to already be /tmp (the work_dir lives under /tmp).
    default_result = e2e.run('mngr exec my-task "pwd"', comment="by default, commands are run in the agent's work_dir")
    expect(default_result).to_succeed()
    expect(default_result.stdout).not_to_match(r"(?m)^/tmp$")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_exec_cwd_nonexistent(e2e: E2eSession) -> None:
    """Unhappy path for the same ``--cwd`` block: a missing directory fails.

    Shares the ``mngr exec --cwd`` tutorial block but exercises the error case
    where the requested working directory does not exist on the agent host. The
    command cannot be started there, so exec must surface a nonzero exit code
    rather than silently falling back to the work_dir.
    """
    e2e.write_tutorial_block("""
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir
    """)
    _create_my_task(e2e, 100405)
    # Point --cwd at a directory that does not exist on the agent host. exec
    # should fail (nonzero exit) rather than run the command in some fallback
    # directory; assert on the exit code, which is the user-observable effect.
    result = e2e.run(
        'mngr exec my-task --cwd /nonexistent-dir-xyz "pwd"',
        comment="a nonexistent --cwd directory causes exec to fail",
    )
    expect(result).to_fail()
    # The command must not have run in the default work_dir: a real /tmp-rooted
    # work_dir path in stdout would mean the bad --cwd was silently ignored.
    expect(result.stdout).not_to_match(r"(?m)^/tmp/")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"
    """)
    _create_my_task(e2e, 100405)
    # Substitute a quick command that returns well within the 30s timeout;
    # the point is to demonstrate the --timeout flag is accepted and that a
    # command finishing inside the budget runs to completion normally.
    result = e2e.run(
        'mngr exec my-task --timeout 30 "echo done"',
        comment="set a timeout (in seconds) for the command",
    )
    expect(result).to_succeed()
    # The command actually ran (not just that the flag parsed): its stdout is forwarded.
    expect(result.stdout).to_contain("done")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_timeout_enforced(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"
    """)
    _create_my_task(e2e, 100409)
    # Unhappy path for the same tutorial block: a command that would run far
    # longer than its --timeout must be terminated, causing exec to fail. The
    # inner --timeout (3s) is well below the sleep (120s), so if the timeout is
    # enforced the command returns quickly with a non-zero exit; if it were
    # ignored, the sleep would outlast e2e.run's own 30s budget and raise.
    result = e2e.run(
        'mngr exec my-task --timeout 3 "sleep 120"',
        comment="a command that exceeds its timeout is terminated and fails",
    )
    expect(result).to_fail()
    # The successful "echo done" path reports success; the terminated command must not.
    expect(result.stdout).not_to_contain("Command succeeded")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_exec_with_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, start the agent's host if it's stopped, run the command, then leave it running
        # but you can be explicit about that behavior:
        mngr exec my-task --start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100406)
    result = e2e.run(
        'mngr exec my-task --start "cat /etc/os-release"',
        comment="explicit --start behavior",
    )
    expect(result).to_succeed()
    # Verify exec actually forwarded the command and captured the host's output,
    # not just that it exited cleanly. /etc/os-release exists on every Linux host
    # and always contains an os-release `ID=` field, regardless of distro.
    expect(result.stdout).to_contain("ID=")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # and you can disable auto-starting as well (fails if agent is stopped):
        mngr exec my-task --no-start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100407)
    # The agent's host is already online (create started it), so --no-start
    # succeeds without auto-starting. Assert on the actual command output --
    # every /etc/os-release defines NAME= -- to prove the command ran on the
    # host and returned its contents rather than just exiting 0 as a no-op.
    result = e2e.run(
        'mngr exec my-task --no-start "cat /etc/os-release"',
        comment="disable auto-starting",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("NAME=")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_on_error_continue(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control error handling when running on multiple agents
        mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"
        # the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)
    """)
    _create_my_task(e2e, 100408)
    # `git log` may fail in the agent's workdir if there's no git history;
    # --on-error continue lets the test succeed regardless.
    result = e2e.run(
        'mngr list --ids | mngr exec - --on-error continue "git log --oneline -5 || true"',
        comment="control error handling when running on multiple agents",
    )
    expect(result).to_succeed()
    # Confirm the command actually ran on the agent's host rather than just
    # exiting 0: the agent's work_dir is the test git repo, so `git log`
    # returns its history (the fixture's "Initial commit").
    expect(result.stdout).to_contain("Initial commit")
