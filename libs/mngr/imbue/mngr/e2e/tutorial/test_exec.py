"""Tests for ``mngr exec`` variants from the tutorial.

Each test corresponds 1:1 to a tutorial script block. Each test creates real
agents with the names the block references so the exec command has a target.
"""

import os

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
    # /workspace may not exist locally, so run `ls` against `/` instead.
    result = e2e.run(
        'mngr exec my-task "ls -la / | head -3"',
        comment="run a command on a specific agent's host",
    )
    expect(result).to_succeed()
    # Verify the command actually ran on the agent's host and its stdout was
    # forwarded back (not merely that the exit code was 0): `ls -la` always
    # prints a "total" header line, which only appears if `ls` really executed.
    expect(result.stdout).to_contain("total")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_propagates_command_failure(e2e: E2eSession) -> None:
    # Unhappy-path counterpart to test_exec_basic, sharing the same tutorial
    # block: when the forwarded command exits non-zero, `mngr exec` must
    # propagate a non-zero exit code rather than masking the failure.
    e2e.write_tutorial_block("""
        # run a command on a specific agent's host
        mngr exec my-task "ls -la /workspace"
        # note that the command must be quoted--it's the last argument passed to "mngr exec"
        # the quoting is required because e.g. this may be sent over SSH
    """)
    _create_my_task(e2e, 100409)
    # `ls` of a path that does not exist exits non-zero on the host; mngr exec
    # should surface that as its own non-zero exit code.
    expect(
        e2e.run(
            'mngr exec my-task "ls /this-path-does-not-exist-xyz"',
            comment="exec propagates a failing command's exit code",
        )
    ).to_fail()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr x my-task "git status"
    """)
    _create_my_task(e2e, 100401)
    result = e2e.run('mngr x my-task "git status"', comment="short form")
    expect(result).to_succeed()
    # Verify the command actually ran `git status` on the agent's host (not just
    # that exec exited 0): the output must contain real git-status text. The agent
    # runs on its own branch, so "On branch" proves git status executed remotely.
    expect(result.stdout).to_contain("On branch")
    expect(result.stdout).to_contain("Command succeeded on agent my-task")


@pytest.mark.timeout(60)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_short_form_matches_long_form(e2e: E2eSession) -> None:
    # Shares the EXEC "short form" tutorial block: `mngr x` is the documented
    # alias for `mngr exec`, so running the same command both ways must produce
    # equivalent results.
    e2e.write_tutorial_block("""
        # short form
        mngr x my-task "git status"
    """)
    _create_my_task(e2e, 100409)
    short = e2e.run('mngr x my-task "git status"', comment="short form")
    full = e2e.run('mngr exec my-task "git status"', comment="long form (mngr x is an alias for mngr exec)")
    expect(short).to_succeed()
    expect(full).to_succeed()
    # The git working tree is unchanged between the two runs, so the short form
    # must report the same status as the long form.
    expect(short.stdout).to_equal(full.stdout)
    expect(short.stdout).to_contain("On branch")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
# `mngr list` performs real provider discovery (including Modal), which is
# slower than the single-agent exec tests, so override the default 10s timeout.
@pytest.mark.timeout(120)
def test_exec_all_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command on all agents
        mngr list --ids | mngr exec - "whoami"
    """)
    _create_my_task(e2e, 100402)
    # "run on all agents" is expressed by piping the ids from `mngr list` into
    # `mngr exec -` (there is no -a/--all flag). With one agent created, the id
    # produced by `mngr list` must resolve back to my-task and the command must
    # run successfully on it.
    result = e2e.run('mngr list --ids | mngr exec - "whoami"', comment="run a command on all agents")
    expect(result).to_succeed()
    # The exec output reports per-agent success by name, which proves the piped
    # id was routed to the right agent and `whoami` ran there.
    expect(result.stdout).to_contain("Command succeeded on agent my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
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
    # The agent runs locally as the current user, so the quoted `id -u` must
    # have executed on the host and printed this process's real uid -- this
    # confirms exec ran the command with the expected user context, not just
    # that it exited cleanly.
    expect(result.stdout).to_contain(str(os.getuid()))


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_cwd(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir
    """)
    _create_my_task(e2e, 100404)
    result = e2e.run('mngr exec my-task --cwd /tmp "pwd"', comment="run a command in a specific working directory")
    expect(result).to_succeed()
    # The agent's work_dir is itself a temp dir under /tmp, so a substring check
    # would pass even if --cwd were ignored. Require pwd to print exactly /tmp
    # on its own line to prove --cwd actually changed the working directory.
    expect(result.stdout).to_match(r"(?m)^/tmp$")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_cwd_defaults_to_work_dir(e2e: E2eSession) -> None:
    # Shares the EXEC --cwd tutorial block; this covers its second line:
    # "by default, commands are run in the agent's work_dir". It verifies that
    # omitting --cwd runs in the work_dir (not /tmp) and that passing --cwd /tmp
    # overrides that default, so the two invocations report different directories.
    e2e.write_tutorial_block("""
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir
    """)
    _create_my_task(e2e, 100404)
    # Without --cwd, the command runs in the agent's work_dir, which is not /tmp.
    default_result = e2e.run(
        'mngr exec my-task "pwd"',
        comment="by default, commands are run in the agent's work_dir",
    )
    expect(default_result).to_succeed()
    expect(default_result.stdout).not_to_match(r"(?m)^/tmp$")
    # With --cwd /tmp, the same command instead reports exactly /tmp, confirming
    # the flag overrides the work_dir default.
    cwd_result = e2e.run(
        'mngr exec my-task --cwd /tmp "pwd"',
        comment="run a command in a specific working directory",
    )
    expect(cwd_result).to_succeed()
    expect(cwd_result.stdout).to_match(r"(?m)^/tmp$")


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
    # command finishing inside the window runs to completion normally.
    result = e2e.run(
        'mngr exec my-task --timeout 30 "echo done"',
        comment="set a timeout (in seconds) for the command",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("done")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_timeout_exceeded(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"
    """)
    _create_my_task(e2e, 100409)
    # Unhappy path for the same tutorial block: a command that runs longer than
    # the timeout must be terminated and surface a non-zero exit code, proving
    # the flag actually enforces the limit rather than merely being accepted.
    # `sleep 30` stands in for the tutorial's long-running `python long_script.py`.
    expect(
        e2e.run(
            'mngr exec my-task --timeout 1 "sleep 30"',
            comment="a command exceeding the timeout is terminated and fails",
        )
    ).to_fail()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_with_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, start the agent's host if it's stopped, run the command, then leave it running
        # but you can be explicit about that behavior:
        mngr exec my-task --start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100406)
    # Stop the agent first so that --start has to actually restart a stopped
    # host -- that is the behavior the flag controls (on an already-running
    # host --start is a no-op and would not exercise anything interesting).
    expect(e2e.run("mngr stop my-task", comment="stop the agent so --start must restart it")).to_succeed()
    result = e2e.run(
        'mngr exec my-task --start "cat /etc/os-release"',
        comment="explicit --start behavior",
    )
    expect(result).to_succeed()
    # The command really ran on the freshly-started host: /etc/os-release
    # always contains a NAME= field on Linux.
    expect(result.stdout).to_contain("NAME=")
    # Per the tutorial, --start leaves the host running afterward. A --no-start
    # exec fails on a stopped host, so its success here confirms the host was
    # left running.
    expect(
        e2e.run(
            'mngr exec my-task --no-start "echo still-running"',
            comment="host is left running after --start",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_no_start(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # and you can disable auto-starting as well (fails if agent is stopped):
        mngr exec my-task --no-start "cat /etc/os-release"
    """)
    _create_my_task(e2e, 100407)
    # The agent's host is already running, so --no-start runs the command
    # without auto-starting anything. Assert on the os-release content to
    # confirm `cat` actually executed on the host (every /etc/os-release has a
    # NAME= field), not just that mngr returned a clean exit code.
    result = e2e.run(
        'mngr exec my-task --no-start "cat /etc/os-release"',
        comment="disable auto-starting",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("NAME=")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_on_error_continue(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control error handling when running on multiple agents
        mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"
        # the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)
    """)
    _create_my_task(e2e, 100408)
    # A fresh agent runs in a git repo, so `git log` succeeds and --on-error
    # continue keeps going across every agent.
    result = e2e.run(
        'mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"',
        comment="control error handling when running on multiple agents",
    )
    expect(result).to_succeed()
    # The command really executed on the agent's host: `git log --oneline`
    # prints at least one short-hash commit line.
    expect(result.stdout).to_match(r"[0-9a-f]{7,} ")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_exec_on_error_continue_attempts_every_agent(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: when the command fails on every
    # agent, "continue" still runs it on all of them (rather than stopping at
    # the first) and the aggregate exit code reflects the failure.
    e2e.write_tutorial_block("""
        # control error handling when running on multiple agents
        mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"
        # the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)
    """)
    for name, sleep_value in (("task-one", 100409), ("task-two", 100410)):
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()
    # `false` exits non-zero on every agent. With --on-error continue, exec
    # attempts all agents and surfaces the failure via a non-zero exit code.
    result = e2e.run(
        'mngr list --ids | mngr exec - --on-error continue "false"',
        comment="command fails on every agent; continue tries them all",
    )
    expect(result).to_fail()
    # Both agents were attempted -- each appears in exec's per-agent output.
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("task-one")
    expect(combined_output).to_contain("task-two")
