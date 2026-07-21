"""Tests for ``mngr exec`` variants from the tutorial.

Each test corresponds 1:1 to a tutorial script block. Each test creates real
agents with the names the block references so the exec command has a target.
"""

import time

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
            timeout=90.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_basic(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a command on a specific agent's host
        mngr exec my-task "ls -la /workspace"
        # note that the command must be quoted--it's the last argument passed to "mngr exec"
        # the quoting is required because e.g. this may be sent over SSH

    Scope: `mngr exec <agent> "<cmd>"` runs the quoted command on that specific
    agent's host and forwards its stdout back. The whole quoted string (here
    including a pipe) executes on the host as one command; `ls -la` always emits
    a leading "total" line, proving exec ran the command and returned its output
    rather than short-circuiting to a bare zero exit.
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_short_form(e2e: E2eSession) -> None:
    """Tutorial block:
        # short form
        mngr x my-task "git status"

    Scope: `mngr x` is the documented short form of `mngr exec`; it runs the
    quoted command inside the agent's work_dir and forwards real output. `git
    status` reports the agent's own `mngr/my-task` branch ("On branch ..."),
    proving git ran in the work_dir rather than exec just exiting 0.
    """
    _create_my_task(e2e, 100401)
    # ``my-task`` is a local command agent in a git project, so neither create
    # nor exec ever provisions a Modal environment or transfers files via rsync
    # -- there is no @pytest.mark.modal or @pytest.mark.rsync because neither
    # resource guard is tripped (the modal CLI is only invoked via
    # environment_create for a remote/Modal-backed agent, and rsync only for a
    # non-git project's file sync).
    result = e2e.run('mngr x my-task "git status"', comment="short form")
    expect(result).to_succeed()
    # ``mngr x`` is the documented short form of ``mngr exec``; verify it
    # actually ran git inside the agent's work_dir by observing real git
    # status output rather than relying solely on the exit code. The agent
    # runs on its own ``mngr/my-task`` branch (the default branch name for an
    # agent), which git status reports on its "On branch" line.
    expect(result.stdout).to_contain("On branch mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_all_agents(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a command on all agents
        mngr list --ids | mngr exec - "whoami"

    Scope: `mngr exec -a "<cmd>"` targets all agents at once (rather than a named
    agent) and runs the command against them, exiting 0.
    """
    _create_my_task(e2e, 100402)
    result = e2e.run('mngr list --ids | mngr exec - "whoami"', comment="run a command on all agents")
    expect(result).to_succeed()
    # Exit 0 alone is insufficient: exec short-circuits to a clean exit when its
    # stdin agent list is empty. Confirm the command actually ran against the
    # enumerated agent -- exec emits "Command succeeded on agent <name>" only for
    # a result it truly executed -- so this proves "-" targeted all agents rather
    # than exec no-op'ing on empty input.
    expect(result.stdout).to_contain("Command succeeded on agent")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_as_other_user(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a command as a specific user as you normally would on that host (ex: sudo -u other-user)
        mngr exec my-task "sudo -u other-user apt-get update"

    Scope: exec passes the quoted command verbatim to the agent host, so any
    host-native form (here the `sudo -u other-user` variant) works the same way.
    The substituted `id -u` exercises that passthrough without needing a real
    other user or package install: it runs on the host and streams back its real
    output (a numeric uid on its own line).
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_exec_cwd(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir

    Scope: `--cwd <dir>` runs the command in that directory (`pwd` prints exactly
    /tmp), while omitting it runs in the agent's work_dir (`pwd` is not /tmp).
    The contrast proves --cwd changed the directory rather than matching a default
    that happened to already be /tmp.
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_exec_cwd_nonexistent(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a command in a specific working directory
        mngr exec my-task --cwd /tmp "pwd"
        # by default, commands are run in the agent's work_dir

    Scope: the unhappy path of the same `--cwd` block. When the requested working
    directory does not exist on the agent host, the command cannot be started
    there, so exec surfaces a nonzero exit code rather than silently falling back
    to the work_dir (stdout shows no /tmp-rooted work_dir path).
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_timeout(e2e: E2eSession) -> None:
    """Tutorial block:
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"

    Scope: `--timeout <seconds>` is accepted, and a command that finishes well
    within the budget runs to completion normally -- exec succeeds and forwards
    its stdout back (the substituted `echo done` proves the command actually ran,
    not just that the flag parsed).
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_exec_timeout_enforced(e2e: E2eSession) -> None:
    """Tutorial block:
        # set a timeout (in seconds) for the command
        mngr exec my-task --timeout 30 "python long_script.py"

    Scope: the unhappy path of the same `--timeout` block. A command that would
    run far longer than its --timeout is terminated, causing exec to fail. The
    inner --timeout (3s) is well below the sleep (120s), so an enforced timeout
    returns quickly with a non-zero exit (and no "Command succeeded"); were it
    ignored, the sleep would outlast e2e.run's own budget.
    """
    _create_my_task(e2e, 100409)
    # Unhappy path for the same tutorial block: a command that would run far
    # longer than its --timeout must be terminated, causing exec to fail. The
    # inner --timeout (3s) is well below the sleep (120s), so if the timeout is
    # enforced the command returns quickly with a non-zero exit; if it were
    # ignored, the sleep would run until e2e.run's own (90s) budget expired.
    #
    # Give e2e.run an explicit 90s budget (well above the enforced return time
    # but still below the 120s sleep) so an ignored --timeout would be caught by
    # that budget -- run_command returns exit 124 at 90s -- rather than by the
    # inner timeout. This widens the gap between the "enforced" and "ignored"
    # outcomes so the timing check below can tell them apart robustly.
    started_at = time.monotonic()
    result = e2e.run(
        'mngr exec my-task --timeout 3 "sleep 120"',
        comment="a command that exceeds its timeout is terminated and fails",
        timeout=90.0,
    )
    elapsed = time.monotonic() - started_at
    expect(result).to_fail()
    # The successful "echo done" path reports success; the terminated command must not.
    expect(result.stdout).not_to_contain("Command succeeded")
    # Observe that the timeout was actually *enforced by exec*, not merely masked
    # by e2e.run's own budget killing the sleep. Exit-code and stdout checks alone
    # cannot distinguish these: an ignored --timeout would let `sleep 120` run
    # until the 90s budget expired, and run_command then returns exit 124 with no
    # "Command succeeded" -- satisfying both checks above. So assert the command
    # returned well within that budget: an enforced 3s timeout returns in a
    # handful of seconds (mngr startup plus the 3s budget, ~15s observed), far
    # below the 45s bar, whereas an ignored timeout could not return before 90s.
    assert elapsed < 45.0, f"exec did not return quickly ({elapsed:.1f}s); --timeout was likely not enforced"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_exec_with_start(e2e: E2eSession) -> None:
    """Tutorial block:
        # by default, start the agent's host if it's stopped, run the command, then leave it running
        # but you can be explicit about that behavior:
        mngr exec my-task --start "cat /etc/os-release"

    Scope: `--start` makes the default auto-start behavior explicit -- exec
    succeeds, runs the command on the host, and forwards its real output. Every
    Linux /etc/os-release contains an `ID=` field, proving exec captured the
    host's file contents rather than just exiting cleanly.
    """
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_no_start(e2e: E2eSession) -> None:
    """Tutorial block:
        # and you can disable auto-starting as well (fails if agent is stopped):
        mngr exec my-task --no-start "cat /etc/os-release"

    Scope: `--no-start` disables auto-starting (it would fail if the agent were
    stopped). Here the host is already online from create, so exec succeeds
    without starting anything and forwards the command's real output -- every
    /etc/os-release defines `NAME=`, proving the command ran rather than no-op'd.
    """
    _create_my_task(e2e, 100407)
    # The agent's host is already online (create started it), so --no-start
    # succeeds without auto-starting. Assert on the actual command output --
    # every /etc/os-release defines NAME= -- to prove the command ran on the
    # host and returned its contents rather than just exiting 0 as a no-op.
    result = e2e.run(
        'mngr exec my-task --no-start "cat /etc/os-release"',
        comment="disable auto-starting",
        timeout=90.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("NAME=")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_on_error_continue(e2e: E2eSession) -> None:
    """Tutorial block:
        # control error handling when running on multiple agents
        mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"
        # the choices for --on-error are the same as for messaging: "continue" (try all agents) and "abort" (stop if any agent fails)

    Scope: piping `mngr list --ids` into `mngr exec -` runs the command on each
    listed agent, and `--on-error continue` tries all agents even if some fail
    (so the run succeeds overall). The command runs in each agent's git work_dir
    -- `git log` returns the fixture's history ("Initial commit"), proving it
    actually executed on the host rather than exec just exiting 0.
    """
    _create_my_task(e2e, 100408)
    # The agent's work_dir is the fixture git repo, so `git log` succeeds and
    # returns its history. `--on-error continue` would try every listed agent
    # even if some failed; here the single agent succeeds, so the run exits 0.
    result = e2e.run(
        'mngr list --ids | mngr exec - --on-error continue "git log --oneline -5"',
        comment="control error handling when running on multiple agents",
    )
    expect(result).to_succeed()
    # Confirm the command actually ran on the agent's host rather than just
    # exiting 0: the agent's work_dir is the test git repo, so `git log`
    # returns its history (the fixture's "Initial commit").
    expect(result.stdout).to_contain("Initial commit")
