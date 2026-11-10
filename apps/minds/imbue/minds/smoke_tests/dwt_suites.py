"""default-workspace-template's CI test suites, run inside the journey's Imbue Cloud workspace.

The template's CI runs three pytest roots through offload, each described by an
``offload-modal*.toml`` at its repo root: the command, the arguments every run
adds, the environment, and the test groups with their retry budgets. The suite
reads those configs from the workspace itself -- the template version the
workspace was created from -- and runs every group the way offload would,
inside the workspace's own gVisor container rather than a Modal sandbox.
"""

import base64
import binascii
import io
import itertools
import re
import shlex
import shutil
import tarfile
import threading
import time
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from pathlib import PurePosixPath
from typing import Annotated
from typing import Final
from typing import assert_never
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StringConstraints
from pydantic import TypeAdapter
from pydantic import ValidationError
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.data_types import DwtGroupAttempt
from imbue.minds.smoke_tests.data_types import DwtJunitCase
from imbue.minds.smoke_tests.data_types import DwtTestGroup
from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.data_types import DwtTestSuite
from imbue.minds.smoke_tests.data_types import SmokeRunVerdict
from imbue.minds.smoke_tests.dwt_workspace_scripts import DETACH_PROGRAM
from imbue.minds.smoke_tests.dwt_workspace_scripts import OFFLOAD_CONFIG_GLOB
from imbue.minds.smoke_tests.dwt_workspace_scripts import READ_OFFLOAD_CONFIGS_COMMAND
from imbue.minds.smoke_tests.dwt_workspace_scripts import RUNNER_DONE_FILE_NAME
from imbue.minds.smoke_tests.dwt_workspace_scripts import RUNNER_FUNCTIONS
from imbue.minds.smoke_tests.dwt_workspace_scripts import RUNNER_LOG_FILE_NAME
from imbue.minds.smoke_tests.dwt_workspace_scripts import RUNNER_PID_FILE_NAME
from imbue.minds.smoke_tests.dwt_workspace_scripts import RUNNER_STATE_COMMAND_TEMPLATE
from imbue.minds.smoke_tests.interfaces import WorkspaceShellInterface
from imbue.minds.smoke_tests.primitives import SMOKE_SKIP_DWT_TESTS_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeDwtReportError
from imbue.minds.smoke_tests.primitives import SmokeWorkspaceExecError

# Every default-workspace-template test is reported as a subtest labelled with this prefix, which is how the
# smoke run's verdict tells their failures from the journey's own.
DWT_SUBTEST_LABEL_PREFIX: Final[str] = "dwt:"

# The only framework the template's CI runs, and so the only one the suite can run as CI does.
_PYTEST_FRAMEWORK: Final[str] = "pytest"

# The exit codes after which a group's junit report is its whole story: all passed, some failed, or no tests matched.
_PYTEST_EXIT_ALL_PASSED: Final[int] = 0
_PYTEST_EXIT_TESTS_FAILED: Final[int] = 1
_PYTEST_EXIT_NO_TESTS_COLLECTED: Final[int] = 5
_PYTEST_EXIT_MEANINGS: Final[dict[int, str]] = {
    2: "interrupted, which a collection error does",
    3: "an internal error",
    4: "a usage error",
    137: "killed, as the workspace's OOM killer sheds a process",
}
_JUNIT_FAILURE_TAGS: Final[frozenset[str]] = frozenset({"failure", "error"})
_JUNIT_SKIPPED_TAG: Final[str] = "skipped"
_JUNIT_XFAIL_TYPE: Final[str] = "pytest.xfail"
_GROUP_RUN_TEST_ID_TEMPLATE: Final[str] = "[{group} group]"

# Per attempt of each group, the runner leaves ``<suite>/<group>/attempt-<n>.{log,exit,xml}``; the log exists
# from the attempt's start.
_ATTEMPT_LOG_FILE_TEMPLATE: Final[str] = "attempt-{number}.log"
_ATTEMPT_EXIT_FILE_TEMPLATE: Final[str] = "attempt-{number}.exit"
_ATTEMPT_REPORT_FILE_TEMPLATE: Final[str] = "attempt-{number}.xml"
# Enough of an attempt's output to show why a run went wrong without burying the report.
_LOG_TAIL_LINE_COUNT: Final[int] = 40

# The template's repo root inside a workspace: where its seed puts the tree, and where its offload configs run from.
DWT_WORKSPACE_REPO_ROOT: Final[PurePosixPath] = PurePosixPath("/home/user/workspace")
# Where a run's runner and results live in the workspace: outside the repo, so no test that inspects the tree
# sees them, and on disk rather than in /tmp's memory-backed tmpfs.
DWT_WORKSPACE_RESULTS_ROOT: Final[PurePosixPath] = PurePosixPath("/var/tmp/studio-smoke-dwt")
_CONFIG_TEXT_BY_FILE_NAME_ADAPTER: Final[TypeAdapter[dict[str, str]]] = TypeAdapter(dict[str, str])
_LOCAL_CONFIGS_DIR_NAME: Final[str] = "offload-configs"
_RUNNER_SCRIPT_FILE_NAME: Final[str] = "run.sh"
_RUNNER_STATE_READ_ATTEMPT_COUNT: Final[int] = 4
_SHORT_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
_FETCH_TIMEOUT_SECONDS: Final[float] = 600.0
_RUNNER_SUITE_NAME: Final[str] = "runner"
_RUNNER_TEST_ID: Final[str] = "[run]"

_EnvironmentVariableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class _OffloadFrameworkSection(FrozenModel):
    """The ``[framework]`` keys a run needs; discovery's own settings are not."""

    model_config = ConfigDict(extra="ignore")

    type: NonEmptyStr = Field(description="The test framework offload drives.")
    command: NonEmptyStr = Field(description="The command running the tests, in shell syntax.")
    run_args: str = Field(default="", description="Arguments every test run adds, in shell syntax.")
    env: dict[_EnvironmentVariableName, str] = Field(
        default_factory=dict, description="Variables set for every test run."
    )


class _OffloadGroupSection(FrozenModel):
    """One ``[groups.<name>]`` table."""

    model_config = ConfigDict(extra="ignore")

    filters: str = Field(default="", description="The arguments selecting the group's tests, in shell syntax.")
    retry_count: NonNegativeInt = Field(default=NonNegativeInt(0), description="Retries for the group's failures.")


class _OffloadReportSection(FrozenModel):
    """The ``[report]`` key naming where CI files the suite's results."""

    model_config = ConfigDict(extra="ignore")

    output_dir: NonEmptyStr = Field(description="Where offload writes the suite's results, relative to the repo.")


class _OffloadConfig(FrozenModel):
    """The parts of an offload config that say which tests run, how, and under what name."""

    model_config = ConfigDict(extra="ignore")

    framework: _OffloadFrameworkSection = Field(description="How the tests run.")
    groups: dict[str, _OffloadGroupSection] = Field(default_factory=dict, description="The test groups, by name.")
    report: _OffloadReportSection = Field(description="Where the results are filed.")


@pure
def parse_dwt_offload_config(config_file_name: str, config_text: str) -> DwtTestSuite:
    """Read one of the template's offload configs as the suite CI runs with it.

    A config the suite cannot run the way CI does is refused rather than
    approximated: a run that quietly differs from CI's would answer a
    different question than the one the smoke run asks.
    """
    try:
        raw_config = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise SmokeConfigError(f"The template's {config_file_name} is not TOML: {exc}") from exc
    try:
        config = _OffloadConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise SmokeConfigError(
            f"The template's {config_file_name} is not an offload config this suite reads: {exc}"
        ) from exc
    if config.framework.type != _PYTEST_FRAMEWORK:
        raise SmokeConfigError(
            f"The template's {config_file_name} runs {config.framework.type}, not {_PYTEST_FRAMEWORK}; "
            "the smoke suite runs only pytest suites."
        )
    if not config.groups:
        raise SmokeConfigError(f"The template's {config_file_name} declares no test groups, so it runs no tests.")
    return DwtTestSuite(
        name=NonEmptyStr(PurePosixPath(config.report.output_dir).name),
        command=config.framework.command,
        run_args=config.framework.run_args,
        environment=dict(config.framework.env),
        groups=tuple(
            DwtTestGroup(name=NonEmptyStr(group_name), filters=group.filters, retry_count=group.retry_count)
            for group_name, group in config.groups.items()
        ),
    )


@pure
def dwt_group_command_line(suite: DwtTestSuite, group: DwtTestGroup) -> str:
    """One group's pytest run as offload composes it: the suite's environment and command, its run arguments, then the group's filters."""
    assignments = tuple(f"{name}={shlex.quote(value)}" for name, value in suite.environment.items())
    parts = ("env", *assignments, str(suite.command), suite.run_args, group.filters)
    return " ".join(part for part in parts if part)


@pure
def parse_junit_cases(junit_xml_text: str) -> tuple[DwtJunitCase, ...]:
    """Every test case of a pytest junit report written in the xunit1 family, which names each case's file."""
    try:
        root = ElementTree.fromstring(junit_xml_text)
    except ElementTree.ParseError as exc:
        raise SmokeDwtReportError(f"The runner's report is not a junit report: {exc}") from exc
    return tuple(_junit_case_of(testcase) for testcase in root.iter("testcase"))


@pure
def _junit_case_of(testcase: Element) -> DwtJunitCase:
    test_id = NonEmptyStr(
        _node_id_of(testcase.get("file", ""), testcase.get("classname", ""), testcase.get("name", ""))
    )
    failures = [child for child in testcase if child.tag in _JUNIT_FAILURE_TAGS]
    skipped = testcase.find(_JUNIT_SKIPPED_TAG)
    if failures:
        return DwtJunitCase(
            test_id=test_id, outcome=DwtTestOutcome.FAILED, detail="\n\n".join(_text_of(child) for child in failures)
        )
    elif skipped is not None:
        reason = skipped.get("message", "")
        detail = f"xfail: {reason}" if skipped.get("type") == _JUNIT_XFAIL_TYPE else reason
        return DwtJunitCase(test_id=test_id, outcome=DwtTestOutcome.SKIPPED, detail=detail)
    else:
        return DwtJunitCase(test_id=test_id, outcome=DwtTestOutcome.PASSED, detail="")


@pure
def _text_of(element: Element) -> str:
    """A failure's traceback, or its one-line message when pytest wrote no traceback."""
    text = element.text or ""
    return text if text.strip() else element.get("message", "")


@pure
def _node_id_of(file: str, classname: str, name: str) -> str:
    """Undo pytest's junit naming: ``classname`` is the case's file as a dotted path, then any class path.

    A module that failed to collect has no classname and the dotted file as its
    name. A test defined in another file than the one it was collected from has a
    ``file`` its classname does not start with, so only the classname places it.
    """
    dotted_file = re.sub(r"\.py$", "", file.replace("/", "."))
    if not classname:
        return file if file and name == dotted_file else name
    elif file and classname == dotted_file:
        return f"{file}::{name}"
    elif file and classname.startswith(f"{dotted_file}."):
        class_path = classname[len(dotted_file) + 1 :].replace(".", "::")
        return f"{file}::{class_path}::{name}"
    else:
        return f"{classname}::{name}"


@pure
def merge_dwt_group_attempts(
    suite_name: NonEmptyStr, group_name: NonEmptyStr, attempts: Sequence[DwtGroupAttempt]
) -> tuple[DwtTestResult, ...]:
    """One result per test the group ran, a pass on any attempt counting as a pass, as offload counts it.

    A run that went wrong rather than its tests -- it never ran, did not
    finish, crashed, or wrote no report -- is reported as a failure of the
    group itself, after the tests it did report.
    """
    if not attempts:
        return (
            _group_run_failure(suite_name, group_name, "The group never ran: the runner ended before reaching it."),
        )
    result_by_test_id: dict[str, DwtTestResult] = {}
    run_failures: list[DwtTestResult] = []
    for attempt in sorted(attempts, key=lambda each: each.number):
        for case in attempt.cases or ():
            earlier = result_by_test_id.get(case.test_id)
            result_by_test_id[case.test_id] = DwtTestResult(
                suite_name=suite_name,
                test_id=case.test_id,
                outcome=case.outcome,
                attempt_count=PositiveInt(1 if earlier is None else earlier.attempt_count + 1),
                detail=case.detail,
            )
        problem = _attempt_problem_or_none(attempt)
        if problem is not None:
            detail = f"{problem}\n\n{attempt.log_tail}" if attempt.log_tail else problem
            run_failures.append(_group_run_failure(suite_name, group_name, detail))
    return (*result_by_test_id.values(), *run_failures)


@pure
def _attempt_problem_or_none(attempt: DwtGroupAttempt) -> str | None:
    """What went wrong with the attempt's run itself, or None when its report says all there is."""
    label = f"Attempt {attempt.number}"
    if attempt.exit_code is None:
        return f"{label} did not finish: the runner stopped before pytest exited."
    elif attempt.exit_code not in (
        _PYTEST_EXIT_ALL_PASSED,
        _PYTEST_EXIT_TESTS_FAILED,
        _PYTEST_EXIT_NO_TESTS_COLLECTED,
    ):
        meaning = _PYTEST_EXIT_MEANINGS.get(attempt.exit_code, "not a test outcome")
        return f"{label}: pytest exited {attempt.exit_code} ({meaning})."
    elif attempt.cases is None:
        return f"{label}: pytest exited {attempt.exit_code} without writing its report."
    elif attempt.exit_code == _PYTEST_EXIT_TESTS_FAILED and not any(
        case.outcome == DwtTestOutcome.FAILED for case in attempt.cases
    ):
        return f"{label}: pytest exited 1 but its report holds no failing test."
    else:
        return None


@pure
def _group_run_failure(suite_name: NonEmptyStr, group_name: NonEmptyStr, detail: str) -> DwtTestResult:
    return DwtTestResult(
        suite_name=suite_name,
        test_id=NonEmptyStr(_GROUP_RUN_TEST_ID_TEMPLATE.format(group=group_name)),
        outcome=DwtTestOutcome.FAILED,
        attempt_count=PositiveInt(1),
        detail=detail,
    )


@pure
def dwt_subtest_label(result: DwtTestResult) -> str:
    """``dwt:<suite>::<node id>``: what the smoke run's report names one default-workspace-template test by."""
    return f"{DWT_SUBTEST_LABEL_PREFIX}{result.suite_name}::{result.test_id}"


@pure
def is_dwt_subtest_label(label: str | None) -> bool:
    return label is not None and label.startswith(DWT_SUBTEST_LABEL_PREFIX)


@pure
def dwt_results_markdown(results: Sequence[DwtTestResult], *, template_ref: str | None) -> str:
    """A summary for a CI job's page: each suite's outcome counts, then every failure by its subtest label.

    ``template_ref`` is the tag the workspace's pool row was baked at, when the
    journey could read it: the summary is the record of which template the
    suites ran against.
    """
    suite_names = sorted({str(result.suite_name) for result in results})
    count_rows = tuple(
        "| {} | {} | {} | {} |".format(
            suite_name,
            *(
                sum(1 for result in results if result.suite_name == suite_name and result.outcome == outcome)
                for outcome in (DwtTestOutcome.PASSED, DwtTestOutcome.FAILED, DwtTestOutcome.SKIPPED)
            ),
        )
        for suite_name in suite_names
    )
    failure_lines = tuple(
        f"- `{dwt_subtest_label(result)}` ({dwt_attempts_phrase(result.attempt_count)})"
        for result in results
        if result.outcome == DwtTestOutcome.FAILED
    )
    lines = (
        "## default-workspace-template's CI suites in the workspace",
        "",
        *((f"Run in a workspace on a pool row baked at template `{template_ref}`.", "") if template_ref else ()),
        "| Suite | Passed | Failed | Skipped |",
        "| --- | --- | --- | --- |",
        *count_rows,
        *(("", "### Failed", "", *failure_lines) if failure_lines else ()),
    )
    return "\n".join(lines) + "\n"


@pure
def dwt_attempts_phrase(attempt_count: int) -> str:
    """How many attempts a result's outcome stands for, in words: CI retries only a retried group's failures."""
    return "its only attempt" if attempt_count == 1 else f"all {attempt_count} attempts"


@pure
def smoke_run_verdict(own_failure_count: int, failed_subtest_labels: Sequence[str | None]) -> SmokeRunVerdict:
    """Split a session's failures into failed default-workspace-template tests and everything else.

    ``own_failure_count`` counts the tests that failed on their own -- a step
    that raised, a fixture that broke -- rather than through a failed subtest.
    """
    dwt_failure_count = sum(1 for label in failed_subtest_labels if is_dwt_subtest_label(label))
    return SmokeRunVerdict(
        dwt_failure_count=NonNegativeInt(dwt_failure_count),
        other_failure_count=NonNegativeInt(own_failure_count + len(failed_subtest_labels) - dwt_failure_count),
    )


@pure
def is_dwt_tests_skip_requested(environ: Mapping[str, str]) -> bool:
    """Whether ``MINDS_SMOKE_SKIP_DWT_TESTS=1`` skips the template's CI tests; unset or ``0`` runs them."""
    value = environ.get(SMOKE_SKIP_DWT_TESTS_ENV_VAR, "")
    if value == "1":
        return True
    if value in ("", "0"):
        return False
    raise SmokeConfigError(f"{SMOKE_SKIP_DWT_TESTS_ENV_VAR} takes 1 (skip) or 0 (run), not {value!r}.")


@pure
def build_dwt_suites_runner_script(
    suites: Sequence[DwtTestSuite],
    results_dir: PurePosixPath,
    repo_root: PurePosixPath,
    env_converge_wait_seconds: int,
) -> str:
    """The bash script running every group of ``suites`` from ``repo_root``, one after another, as offload runs it."""
    header = (
        "#!/usr/bin/env bash",
        "# Runs default-workspace-template's CI test suites the way its offload configs describe them,",
        "# for the Imbue Studio smoke suite, which reads what it leaves under the results directory.",
        "set -uo pipefail",
        f"readonly results_dir={shlex.quote(str(results_dir))}",
        f"readonly repo_root={shlex.quote(str(repo_root))}",
        f"readonly env_converge_wait_seconds={env_converge_wait_seconds}",
        'mkdir -p "$results_dir"',
    )
    group_lines = tuple(
        " ".join(
            (
                "run_group",
                shlex.quote(str(suite.name)),
                shlex.quote(str(group.name)),
                str(group.retry_count),
                shlex.quote(dwt_group_command_line(suite, group)),
            )
        )
        for suite in suites
        for group in suite.groups
    )
    return "\n".join((*header, RUNNER_FUNCTIONS, *group_lines)) + "\n"


def read_dwt_results(results_dir: Path, suites: Sequence[DwtTestSuite]) -> tuple[DwtTestResult, ...]:
    """Every result the runner left under ``results_dir``: one per test, plus each group run's own failure."""
    return tuple(
        result
        for suite in suites
        for group in suite.groups
        for result in merge_dwt_group_attempts(
            suite.name, group.name, read_dwt_group_attempts(results_dir / suite.name / group.name)
        )
    )


def read_dwt_group_attempts(group_dir: Path) -> tuple[DwtGroupAttempt, ...]:
    """The attempts the runner started for one group, in order; none when it never reached the group."""
    attempts: list[DwtGroupAttempt] = []
    for number in itertools.count(1):
        log_path = group_dir / _ATTEMPT_LOG_FILE_TEMPLATE.format(number=number)
        if not log_path.is_file():
            break
        attempts.append(
            DwtGroupAttempt(
                number=PositiveInt(number),
                exit_code=_read_exit_code_or_none(group_dir / _ATTEMPT_EXIT_FILE_TEMPLATE.format(number=number)),
                cases=_read_junit_cases_or_none(group_dir / _ATTEMPT_REPORT_FILE_TEMPLATE.format(number=number)),
                log_tail=_log_tail_of(log_path),
            )
        )
    return tuple(attempts)


def _read_exit_code_or_none(exit_path: Path) -> int | None:
    """The exit code an attempt recorded, or None when it never recorded one."""
    if not exit_path.is_file():
        return None
    text = exit_path.read_text().strip()
    if not text.isdigit():
        logger.warning("Read the exit file {} holding {!r} as no exit code", exit_path, text)
        return None
    return int(text)


def _read_junit_cases_or_none(report_path: Path) -> tuple[DwtJunitCase, ...] | None:
    """The cases of an attempt's report, or None when it wrote none or was cut off writing it."""
    if not report_path.is_file():
        return None
    try:
        return parse_junit_cases(report_path.read_text(errors="replace"))
    except SmokeDwtReportError as exc:
        logger.warning("Read the unreadable junit report {} as no report: {}", report_path, exc)
        return None


class DwtRunnerEnd(UpperCaseStrEnum):
    """How the wait for the runner in the workspace ended."""

    FINISHED = auto()
    VANISHED = auto()
    OUT_OF_TIME = auto()


class _DwtRunnerState(UpperCaseStrEnum):
    """What one look at the runner in the workspace found."""

    STARTING = auto()
    RUNNING = auto()
    FINISHED = auto()
    VANISHED = auto()


def run_dwt_ci_suites_in_workspace(
    workspace: WorkspaceShellInterface,
    repo_root: PurePosixPath,
    workspace_results_dir: PurePosixPath,
    local_results_dir: Path,
    env_converge_wait_seconds: int,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> tuple[DwtTestResult, ...]:
    """Run every suite the template's offload configs describe, inside the workspace, and bring the results back.

    The runner is started detached and polled, so the suites' long run hangs
    off no single connection; past ``deadline_seconds`` it is stopped.
    Everything it left lands under ``local_results_dir``, which is emptied
    first so that an earlier run's attempts cannot be read as this run's.
    Returns one result per test, then the failures of runs that went wrong
    rather than their tests.
    """
    if local_results_dir.exists():
        shutil.rmtree(local_results_dir)
    suites = read_dwt_suites_from_workspace(workspace, repo_root, local_results_dir)
    script = build_dwt_suites_runner_script(suites, workspace_results_dir, repo_root, env_converge_wait_seconds)
    with log_span("Starting default-workspace-template's CI suites in the workspace under {}", workspace_results_dir):
        _start_dwt_runner(workspace, script, workspace_results_dir, repo_root)
    runner_end = _wait_for_dwt_runner(
        workspace, workspace_results_dir, repo_root, deadline_seconds, poll_interval_seconds
    )
    if runner_end == DwtRunnerEnd.OUT_OF_TIME:
        _stop_dwt_runner(workspace, workspace_results_dir, repo_root)
    with log_span("Fetching the suites' results from the workspace into {}", local_results_dir):
        fetch_workspace_directory(workspace, workspace_results_dir, local_results_dir, repo_root)
    runner_log_tail = _log_tail_of(local_results_dir / RUNNER_LOG_FILE_NAME)
    return (
        *read_dwt_results(local_results_dir, suites),
        *_runner_end_failures(runner_end, deadline_seconds, runner_log_tail),
    )


def read_dwt_suites_from_workspace(
    workspace: WorkspaceShellInterface, repo_root: PurePosixPath, local_results_dir: Path
) -> tuple[DwtTestSuite, ...]:
    """The suites the template's offload configs describe, as the workspace holds them; each config is kept locally."""
    stdout = workspace.run(READ_OFFLOAD_CONFIGS_COMMAND, repo_root, _SHORT_COMMAND_TIMEOUT_SECONDS)
    try:
        config_text_by_file_name = _CONFIG_TEXT_BY_FILE_NAME_ADAPTER.validate_json(stdout)
    except ValidationError as exc:
        raise SmokeWorkspaceExecError(f"The workspace did not list its offload configs as JSON: {exc}") from exc
    if not config_text_by_file_name:
        raise SmokeConfigError(
            f"The template at {repo_root} has no {OFFLOAD_CONFIG_GLOB}, so it declares no CI suites to run."
        )
    configs_dir = local_results_dir / _LOCAL_CONFIGS_DIR_NAME
    configs_dir.mkdir(parents=True, exist_ok=True)
    for file_name, config_text in config_text_by_file_name.items():
        (configs_dir / file_name).write_text(config_text)
    return tuple(
        parse_dwt_offload_config(file_name, config_text)
        for file_name, config_text in sorted(config_text_by_file_name.items())
    )


def _start_dwt_runner(
    workspace: WorkspaceShellInterface, script: str, workspace_results_dir: PurePosixPath, repo_root: PurePosixPath
) -> None:
    """Write the runner into a fresh results directory and start it in a session of its own."""
    script_path = workspace_results_dir / _RUNNER_SCRIPT_FILE_NAME
    encoded_script = base64.b64encode(script.encode()).decode()
    command = " && ".join(
        (
            f"rm -rf {shlex.quote(str(workspace_results_dir))}",
            f"mkdir -p {shlex.quote(str(workspace_results_dir))}",
            f"printf '%s' {shlex.quote(encoded_script)} | base64 -d > {shlex.quote(str(script_path))}",
            " ".join(
                (
                    "python3 -c",
                    shlex.quote(DETACH_PROGRAM),
                    shlex.quote(str(script_path)),
                    shlex.quote(str(workspace_results_dir / RUNNER_LOG_FILE_NAME)),
                )
            ),
        )
    )
    workspace.run(command, repo_root, _SHORT_COMMAND_TIMEOUT_SECONDS)


def _wait_for_dwt_runner(
    workspace: WorkspaceShellInterface,
    workspace_results_dir: PurePosixPath,
    repo_root: PurePosixPath,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> DwtRunnerEnd:
    """Poll the runner until it finishes, vanishes, or outlives the deadline, logging each step it reaches."""
    deadline = time.monotonic() + deadline_seconds
    last_progress = ""
    while time.monotonic() < deadline:
        threading.Event().wait(timeout=poll_interval_seconds)
        state, progress = _read_dwt_runner_state(workspace, workspace_results_dir, repo_root)
        if progress and progress != last_progress:
            logger.info("The runner in the workspace is on: {}", progress)
            last_progress = progress
        match state:
            case _DwtRunnerState.FINISHED:
                return DwtRunnerEnd.FINISHED
            case _DwtRunnerState.VANISHED:
                return DwtRunnerEnd.VANISHED
            case _DwtRunnerState.STARTING | _DwtRunnerState.RUNNING:
                pass
            case _ as unreachable:
                assert_never(unreachable)
    return DwtRunnerEnd.OUT_OF_TIME


@retry(
    retry=retry_if_exception_type(SmokeWorkspaceExecError),
    stop=stop_after_attempt(_RUNNER_STATE_READ_ATTEMPT_COUNT),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _read_dwt_runner_state(
    workspace: WorkspaceShellInterface, workspace_results_dir: PurePosixPath, repo_root: PurePosixPath
) -> tuple[_DwtRunnerState, str]:
    """The runner's state, and the last line it logged; a dropped connection is retried before it counts."""
    stdout = workspace.run(
        RUNNER_STATE_COMMAND_TEMPLATE.format(
            done=shlex.quote(str(workspace_results_dir / RUNNER_DONE_FILE_NAME)),
            pid=shlex.quote(str(workspace_results_dir / RUNNER_PID_FILE_NAME)),
            log=shlex.quote(str(workspace_results_dir / RUNNER_LOG_FILE_NAME)),
        ),
        repo_root,
        _SHORT_COMMAND_TIMEOUT_SECONDS,
    )
    state_line, _, progress = stdout.partition("\n")
    try:
        state = _DwtRunnerState(state_line.strip())
    except ValueError as exc:
        raise SmokeWorkspaceExecError(f"The runner's state read as {stdout[:200]!r}, not one of its states.") from exc
    return state, progress.strip()


def _stop_dwt_runner(
    workspace: WorkspaceShellInterface, workspace_results_dir: PurePosixPath, repo_root: PurePosixPath
) -> None:
    """End the runner's whole session, the pytest it is waiting on included."""
    pid_path = shlex.quote(str(workspace_results_dir / RUNNER_PID_FILE_NAME))
    with log_span("Stopping the runner in the workspace, which outlived its deadline"):
        workspace.run(
            f'kill -TERM -- -"$(cat {pid_path})" 2>/dev/null; true', repo_root, _SHORT_COMMAND_TIMEOUT_SECONDS
        )


def fetch_workspace_directory(
    workspace: WorkspaceShellInterface, workspace_dir: PurePosixPath, local_dir: Path, cwd: PurePosixPath
) -> None:
    """Copy everything under ``workspace_dir`` into ``local_dir``, as a gzipped tarball over the command's stdout."""
    encoded_archive = workspace.run(
        f"tar -C {shlex.quote(str(workspace_dir))} -czf - . | base64", cwd, _FETCH_TIMEOUT_SECONDS
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded_archive)), mode="r:gz") as archive:
            archive.extractall(local_dir, filter="data")
    except (binascii.Error, tarfile.TarError, EOFError) as exc:
        raise SmokeWorkspaceExecError(f"The workspace sent an unreadable archive of {workspace_dir}: {exc}") from exc


def _log_tail_of(log_path: Path) -> str:
    """The last lines of a log the runner left, or nothing when it left none."""
    if not log_path.is_file():
        return ""
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINE_COUNT:])


@pure
def _runner_end_failures(
    runner_end: DwtRunnerEnd, deadline_seconds: float, runner_log_tail: str
) -> tuple[DwtTestResult, ...]:
    """The runner's own failure, when the suites did not get to finish."""
    match runner_end:
        case DwtRunnerEnd.FINISHED:
            return ()
        case DwtRunnerEnd.VANISHED:
            problem = "The runner's process ended without finishing, as one the workspace's OOM killer sheds does."
        case DwtRunnerEnd.OUT_OF_TIME:
            problem = (
                f"The suites did not finish within {deadline_seconds / 60:.0f} minutes, so the runner was stopped; "
                "the groups it had not finished are reported as such."
            )
        case _ as unreachable:
            assert_never(unreachable)
    detail = f"{problem}\n\n{runner_log_tail}" if runner_log_tail else problem
    return (
        DwtTestResult(
            suite_name=NonEmptyStr(_RUNNER_SUITE_NAME),
            test_id=NonEmptyStr(_RUNNER_TEST_ID),
            outcome=DwtTestOutcome.FAILED,
            attempt_count=PositiveInt(1),
            detail=detail,
        ),
    )
