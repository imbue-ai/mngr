import os
import shlex
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from imbue.minds.smoke_tests.mock_workspace_shell_test import LocalWorkspaceShell
from imbue.minds.smoke_tests.workspace_runner import RunnerEnd
from imbue.minds.smoke_tests.workspace_runner import start_detached_runner
from imbue.minds.smoke_tests.workspace_runner import wait_for_detached_runner
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_PID_FILE_NAME
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_PREAMBLE

pytestmark = [pytest.mark.timeout(30)]


def _write_cat_that_finishes_the_runner_while_it_reads(bin_dir: Path, pid_path: Path, release_fifo: Path) -> None:
    """A ``cat`` that, the first time it reads the runner's pid, lets the runner end and waits until it is gone.

    The state probe reads the pid between its check for ``done`` and its liveness check, so this puts a
    runner's whole exit, trap and reaping included, inside the probe's window between the two.
    """
    released_marker = bin_dir / "released"
    cat_path = bin_dir / "cat"
    cat_path.write_text(
        "#!/bin/bash\n"
        'pid="$(/bin/cat "$@")"\n'
        f'if [ "$1" = {shlex.quote(str(pid_path))} ] && [ ! -e {shlex.quote(str(released_marker))} ]; then\n'
        f"  touch {shlex.quote(str(released_marker))}\n"
        f"  echo go > {shlex.quote(str(release_fifo))}\n"
        '  while kill -0 "$pid" 2>/dev/null; do :; done\n'
        "fi\n"
        'printf "%s\\n" "$pid"\n'
    )
    cat_path.chmod(0o755)


def test_a_runner_that_finishes_while_its_state_is_read_reads_as_finished(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    release_fifo = tmp_path / "release"
    os.mkfifo(release_fifo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_cat_that_finishes_the_runner_while_it_reads(bin_dir, results_dir / RUNNER_PID_FILE_NAME, release_fifo)
    workspace = LocalWorkspaceShell(
        environment={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": os.environ["HOME"]}
    )
    script = "\n".join(
        (
            f"results_dir={shlex.quote(str(results_dir))}",
            RUNNER_PREAMBLE,
            f"read -r _ < {shlex.quote(str(release_fifo))}",
        )
    )

    start_detached_runner(workspace, script, PurePosixPath(results_dir), PurePosixPath(tmp_path))
    runner_end = wait_for_detached_runner(
        workspace,
        PurePosixPath(results_dir),
        PurePosixPath(tmp_path),
        deadline_seconds=20.0,
        poll_interval_seconds=0.05,
    )

    assert (bin_dir / "released").exists()
    assert runner_end == RunnerEnd.FINISHED
