"""default-workspace-template's OOM drill, run for real through the suite's runner against a repo shaped like the template's.

The "workspace" is this machine, reached through a local shell, and the template's drill is a stand-in at the
template's path that prints the verdict it was given, logs a line and exits as the real one would. The runner, its
detaching, its polling and the fetch of what it left are the suite's own.
"""

import json
import os
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

import pytest

from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.mock_workspace_shell_test import LocalWorkspaceShell
from imbue.minds.smoke_tests.oom_drill import OOM_DRILL_RELATIVE_PATH
from imbue.minds.smoke_tests.oom_drill import run_oom_drill_in_workspace

# Each test drives the runner's real bash and the stand-in drill's python3, which take a few seconds when the suite
# runs in parallel.
pytestmark = [pytest.mark.timeout(60)]

_DRILL_TIMEOUT_SECONDS = 360
_PASSED_VERDICT: dict[str, Any] = {
    "passed": True,
    "failure": "",
    "bands": [1000, 300],
    "sleeper_bands_in_shed_order": [1000, 300],
    "kills": [],
}


def _write_template_with_a_stand_in_drill(repo_root: Path, verdict: dict[str, Any], exit_code: int) -> Path:
    """A template checkout whose drill prints ``verdict``, logs a line and exits ``exit_code``; returns where the
    drill records the arguments it was given."""
    argv_path = repo_root / "drill-argv"
    drill_path = repo_root / OOM_DRILL_RELATIVE_PATH
    drill_path.parent.mkdir(parents=True)
    drill_path.write_text(
        "import sys\n"
        f"open({str(argv_path)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        f"print({json.dumps(verdict)!r})\n"
        "print('the stand-in drill ran', file=sys.stderr)\n"
        f"sys.exit({exit_code})\n"
    )
    return argv_path


def _local_workspace() -> LocalWorkspaceShell:
    return LocalWorkspaceShell(environment={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]})


def test_the_drill_runs_in_the_workspace_with_its_timeout_and_its_verdict_comes_back(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    local_results_dir = tmp_path / "fetched"
    argv_path = _write_template_with_a_stand_in_drill(repo_root, _PASSED_VERDICT, exit_code=0)

    result = run_oom_drill_in_workspace(
        workspace=_local_workspace(),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=local_results_dir,
        drill_timeout_seconds=_DRILL_TIMEOUT_SECONDS,
        deadline_seconds=30.0,
        poll_interval_seconds=0.5,
    )

    assert (result.outcome, result.detail) == (DwtTestOutcome.PASSED, "")
    assert argv_path.read_text() == f"--timeout {_DRILL_TIMEOUT_SECONDS}"
    assert json.loads((local_results_dir / "verdict.json").read_text()) == _PASSED_VERDICT
    assert "the stand-in drill ran" in (local_results_dir / "drill.log").read_text()
    assert (local_results_dir / "done").is_file()


def test_a_drill_that_fails_in_the_workspace_comes_back_failed_with_its_reason(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    failed_verdict = {**_PASSED_VERDICT, "passed": False, "failure": "the hog died", "sleeper_bands_in_shed_order": []}
    _write_template_with_a_stand_in_drill(repo_root, failed_verdict, exit_code=1)

    result = run_oom_drill_in_workspace(
        workspace=_local_workspace(),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=tmp_path / "fetched",
        drill_timeout_seconds=_DRILL_TIMEOUT_SECONDS,
        deadline_seconds=30.0,
        poll_interval_seconds=0.5,
    )

    assert result.outcome == DwtTestOutcome.FAILED
    assert "The drill failed: the hog died" in result.detail
    assert "Sleepers shed, in order: none (of 1000, 300)" in result.detail


def test_a_drill_that_outlives_the_deadline_is_stopped_and_reported(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    drill_path = repo_root / OOM_DRILL_RELATIVE_PATH
    drill_path.parent.mkdir(parents=True)
    drill_path.write_text("import time\ntime.sleep(60)\n")

    result = run_oom_drill_in_workspace(
        workspace=_local_workspace(),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=tmp_path / "fetched",
        drill_timeout_seconds=_DRILL_TIMEOUT_SECONDS,
        deadline_seconds=2.0,
        poll_interval_seconds=0.5,
    )

    assert result.outcome == DwtTestOutcome.FAILED
    assert "The drill did not finish within" in result.detail
