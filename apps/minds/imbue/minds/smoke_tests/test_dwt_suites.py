"""default-workspace-template's CI suites, run for real against a repo shaped like the template's.

The repo holds two offload configs like the template's: a root suite with a
group of plain tests and a retried group of flaky ones, and an app suite whose
command moves into its own directory and whose own config deselects a test that
CI's run arguments bring back. Each group runs a real nested pytest, so the
retries, the reports and the exit codes are pytest's own; the "workspace" is
this machine, reached through a local shell.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.data_types import DwtTestSuite
from imbue.minds.smoke_tests.dwt_suites import build_dwt_suites_runner_script
from imbue.minds.smoke_tests.dwt_suites import parse_dwt_offload_config
from imbue.minds.smoke_tests.dwt_suites import read_dwt_results
from imbue.minds.smoke_tests.dwt_suites import run_dwt_ci_suites_in_workspace
from imbue.minds.smoke_tests.mock_workspace_shell_test import LocalWorkspaceShell
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.testing import write_stub_supervisorctl

_RUNNER_TIMEOUT_SECONDS = 120.0
_ENV_CONVERGE_EXITED = "env-converge                     EXITED    Oct 29 08:00 AM"
_ENV_CONVERGE_RUNNING = "env-converge                     RUNNING   pid 4242, uptime 0:05:00"

# Like the template's root config, it leaves the app's tests to the app's own suite.
_ROOT_PYTEST_INI = """[pytest]
addopts = --ignore=apps
markers =
    flaky: retried by offload
"""

_ROOT_TESTS = """import os
from pathlib import Path

import pytest


def test_sees_the_config_s_environment() -> None:
    assert os.environ["DWT_SMOKE_PROBE"] == "from the config"


def test_fails_every_time() -> None:
    raise AssertionError("a real failure 48213")


def test_skips() -> None:
    pytest.skip("no browser here")


@pytest.mark.flaky
def test_flaky_passes_on_its_second_attempt() -> None:
    marker = Path(__file__).with_name(".first-attempt-ran")
    if not marker.exists():
        marker.write_text("ran")
        raise AssertionError("the first attempt fails")


@pytest.mark.flaky
def test_flaky_fails_every_attempt() -> None:
    raise AssertionError("still broken 90417")
"""

# A root test that outlives any deadline a test gives the runner.
_HANGING_ROOT_TESTS = """import threading


def test_hangs() -> None:
    threading.Event().wait(timeout=97)
"""

# Like the template's chat app: its own config deselects a marker by default.
_APP_PYTEST_INI = """[pytest]
addopts = -m "not browser"
markers =
    browser: drives a browser
    flaky: retried by offload
"""

_APP_TESTS = """import pytest


@pytest.mark.browser
def test_runs_although_the_app_s_own_config_deselects_it() -> None:
    pass
"""

_EXPECTED_OUTCOMES = {
    ("root", "test_sample.py::test_sees_the_config_s_environment"): (DwtTestOutcome.PASSED, 1),
    ("root", "test_sample.py::test_fails_every_time"): (DwtTestOutcome.FAILED, 1),
    ("root", "test_sample.py::test_skips"): (DwtTestOutcome.SKIPPED, 1),
    ("root", "test_sample.py::test_flaky_passes_on_its_second_attempt"): (DwtTestOutcome.PASSED, 2),
    ("root", "test_sample.py::test_flaky_fails_every_attempt"): (DwtTestOutcome.FAILED, 3),
    ("chat", "test_app.py::test_runs_although_the_app_s_own_config_deselects_it"): (DwtTestOutcome.PASSED, 1),
}


def _root_config(python: str) -> str:
    return f"""
[framework]
type = "pytest"
command = "{python} -m pytest"
env = {{ DWT_SMOKE_PROBE = "from the config" }}

[groups.all]
retry_count = 0
filters = "-m 'not flaky'"

[groups.flaky]
retry_count = 2
filters = "-m flaky"

[report]
output_dir = "test-results/root"
"""


def _app_config(python: str) -> str:
    return f"""
[framework]
type = "pytest"
command = "bash -c '[ -d apps/chat ] && cd apps/chat; exec {python} -m pytest \\"$@\\"' --"
run_args = "-m ''"

[groups.all]
retry_count = 0
filters = "-m 'not flaky'"

[report]
output_dir = "test-results/chat"
"""


def _write_template_shaped_repo(repo_root: Path, root_tests: str) -> tuple[DwtTestSuite, ...]:
    python = shlex.quote(sys.executable)
    (repo_root / "apps" / "chat").mkdir(parents=True)
    (repo_root / "pytest.ini").write_text(_ROOT_PYTEST_INI)
    (repo_root / "test_sample.py").write_text(root_tests)
    (repo_root / "apps" / "chat" / "pytest.ini").write_text(_APP_PYTEST_INI)
    (repo_root / "apps" / "chat" / "test_app.py").write_text(_APP_TESTS)
    configs = {"offload-modal.toml": _root_config(python), "offload-modal-chat.toml": _app_config(python)}
    for file_name, text in configs.items():
        (repo_root / file_name).write_text(text)
    return tuple(parse_dwt_offload_config(file_name, text) for file_name, text in configs.items())


def _workspace_environment(tmp_path: Path, env_converge_status: str) -> dict[str, str]:
    """A "workspace" whose supervisorctl reports ``env_converge_status``; no third-party pytest plugin loads in it."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    write_stub_supervisorctl(stub_bin, status_lines=(env_converge_status,))
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _run_runner_script(script: str, tmp_path: Path, env_converge_status: str) -> None:
    script_path = tmp_path / "run.sh"
    script_path.write_text(script)
    subprocess.run(
        ["bash", str(script_path)],
        env=_workspace_environment(tmp_path, env_converge_status),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=_RUNNER_TIMEOUT_SECONDS,
    )


def _outcome_by_test(results: tuple[DwtTestResult, ...]) -> dict[tuple[str, str], tuple[DwtTestOutcome, int]]:
    return {
        (str(result.suite_name), str(result.test_id)): (result.outcome, result.attempt_count) for result in results
    }


def test_the_runner_runs_every_group_as_ci_does_and_leaves_one_result_per_test(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    suites = _write_template_shaped_repo(tmp_path / "workspace", _ROOT_TESTS)
    script = build_dwt_suites_runner_script(
        suites=suites,
        results_dir=PurePosixPath(results_dir),
        repo_root=PurePosixPath(tmp_path / "workspace"),
        env_converge_wait_seconds=60,
    )

    _run_runner_script(script, tmp_path, _ENV_CONVERGE_EXITED)

    assert (results_dir / "done").is_file()
    assert "EXITED" in (results_dir / "env-converge-status.txt").read_text()
    results = read_dwt_results(results_dir, suites)
    assert _outcome_by_test(results) == _EXPECTED_OUTCOMES
    detail_by_test = {str(result.test_id): result.detail for result in results}
    assert "a real failure 48213" in detail_by_test["test_sample.py::test_fails_every_time"]
    assert "still broken 90417" in detail_by_test["test_sample.py::test_flaky_fails_every_attempt"]


def test_the_runner_stops_waiting_for_env_converge_at_its_deadline(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    suites = _write_template_shaped_repo(tmp_path / "workspace", _ROOT_TESTS)
    script = build_dwt_suites_runner_script(
        suites=suites,
        results_dir=PurePosixPath(results_dir),
        repo_root=PurePosixPath(tmp_path / "workspace"),
        env_converge_wait_seconds=0,
    )

    _run_runner_script(script, tmp_path, _ENV_CONVERGE_RUNNING)

    assert "RUNNING" in (results_dir / "env-converge-status.txt").read_text()
    assert _outcome_by_test(read_dwt_results(results_dir, suites)) == _EXPECTED_OUTCOMES


def test_the_suites_run_in_the_workspace_and_come_back_as_one_result_per_test(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    local_results_dir = tmp_path / "fetched"
    _write_template_shaped_repo(repo_root, _ROOT_TESTS)

    results = run_dwt_ci_suites_in_workspace(
        workspace=LocalWorkspaceShell(environment=_workspace_environment(tmp_path, _ENV_CONVERGE_EXITED)),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=local_results_dir,
        env_converge_wait_seconds=60,
        deadline_seconds=_RUNNER_TIMEOUT_SECONDS,
        poll_interval_seconds=0.2,
    )

    assert _outcome_by_test(results) == _EXPECTED_OUTCOMES
    assert (local_results_dir / "root" / "all" / "attempt-1.xml").is_file()
    assert (local_results_dir / "offload-configs" / "offload-modal-chat.toml").is_file()


def test_a_rerun_into_the_same_local_directory_reports_only_its_own_attempts(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    local_results_dir = tmp_path / "fetched"
    _write_template_shaped_repo(repo_root, _ROOT_TESTS)
    # An earlier run's second attempt of a group this run attempts once.
    stale_group_dir = local_results_dir / "root" / "all"
    stale_group_dir.mkdir(parents=True)
    (stale_group_dir / "attempt-2.log").write_text("an earlier run's output\n")
    (stale_group_dir / "attempt-2.exit").write_text("1\n")

    results = run_dwt_ci_suites_in_workspace(
        workspace=LocalWorkspaceShell(environment=_workspace_environment(tmp_path, _ENV_CONVERGE_EXITED)),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=local_results_dir,
        env_converge_wait_seconds=60,
        deadline_seconds=_RUNNER_TIMEOUT_SECONDS,
        poll_interval_seconds=0.2,
    )

    assert _outcome_by_test(results) == _EXPECTED_OUTCOMES


def test_suites_that_outlive_the_deadline_are_stopped_and_reported_as_unfinished(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    _write_template_shaped_repo(repo_root, _HANGING_ROOT_TESTS)

    results = run_dwt_ci_suites_in_workspace(
        workspace=LocalWorkspaceShell(environment=_workspace_environment(tmp_path, _ENV_CONVERGE_EXITED)),
        repo_root=PurePosixPath(repo_root),
        workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
        local_results_dir=tmp_path / "fetched",
        env_converge_wait_seconds=60,
        deadline_seconds=3.0,
        poll_interval_seconds=0.2,
    )

    # The suites run in their configs' name order, so the chat suite finished before the root suite hung.
    result_by_test = {(str(result.suite_name), str(result.test_id)): result for result in results}
    assert set(result_by_test) == {
        ("chat", "test_app.py::test_runs_although_the_app_s_own_config_deselects_it"),
        ("root", "[all group]"),
        ("root", "[flaky group]"),
        ("runner", "[run]"),
    }
    assert result_by_test[("chat", "test_app.py::test_runs_although_the_app_s_own_config_deselects_it")].outcome == (
        DwtTestOutcome.PASSED
    )
    assert "did not finish:" in result_by_test[("root", "[all group]")].detail
    assert "never ran" in result_by_test[("root", "[flaky group]")].detail
    assert "did not finish within" in result_by_test[("runner", "[run]")].detail
    assert all(result.outcome == DwtTestOutcome.FAILED for key, result in result_by_test.items() if key[0] != "chat")


def test_a_template_without_offload_configs_is_refused(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()

    with pytest.raises(SmokeConfigError, match="offload-modal"):
        run_dwt_ci_suites_in_workspace(
            workspace=LocalWorkspaceShell(environment=_workspace_environment(tmp_path, _ENV_CONVERGE_EXITED)),
            repo_root=PurePosixPath(repo_root),
            workspace_results_dir=PurePosixPath(tmp_path / "workspace-results"),
            local_results_dir=tmp_path / "fetched",
            env_converge_wait_seconds=60,
            deadline_seconds=_RUNNER_TIMEOUT_SECONDS,
            poll_interval_seconds=0.2,
        )
