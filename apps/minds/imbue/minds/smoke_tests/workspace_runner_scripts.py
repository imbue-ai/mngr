"""The text the smoke suite runs inside a workspace to start a detached runner there and look at it.

Nothing here runs in the suite's own process: all of it is executed in the workspace.
:mod:`imbue.minds.smoke_tests.workspace_runner` fills in the per-run parts.
"""

from typing import Final

# What a runner leaves directly under its results directory, as its text below names them: its pid, its own
# output, and ``done`` once it has ended, however it ended.
RUNNER_DONE_FILE_NAME: Final[str] = "done"
RUNNER_PID_FILE_NAME: Final[str] = "runner.pid"
RUNNER_LOG_FILE_NAME: Final[str] = "runner.log"

# The start of every runner script, after it sets ``results_dir``.
RUNNER_PREAMBLE: Final[str] = r"""
echo "$$" > "$results_dir/runner.pid"
trap 'date -u +%Y-%m-%dT%H:%M:%SZ > "$results_dir/done"' EXIT
"""

# Starts the runner in a session of its own, so it outlives the command that started it and can be stopped
# whole, and gives it nothing to hold the command's output open with.
DETACH_PROGRAM: Final[str] = (
    "import subprocess, sys; "
    "subprocess.Popen(['bash', sys.argv[1]], start_new_session=True, stdin=subprocess.DEVNULL, "
    "stdout=open(sys.argv[2], 'wb'), stderr=subprocess.STDOUT)"
)
# Prints the runner's state, then the last line it logged. The runner writes ``done`` before it exits, so
# ``done`` is looked for again once the runner is found gone: a runner that finished after the first look
# would otherwise read as one that vanished.
RUNNER_STATE_COMMAND_TEMPLATE: Final[str] = (
    "if [ -e {done} ]; then echo FINISHED; "
    "elif [ ! -s {pid} ]; then echo STARTING; "
    'elif kill -0 "$(cat {pid})" 2>/dev/null; then echo RUNNING; '
    "elif [ -e {done} ]; then echo FINISHED; "
    "else echo VANISHED; fi; "
    "tail -n 1 {log} 2>/dev/null || true"
)
