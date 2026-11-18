"""Reads a finished smoke session into its run report (``report.py``): which scenarios ran, how each ended, and why.

The smoke suite's fixtures put what they resolve into the pytest stash as they
resolve it -- the backend, the build, the run id, the manifest, and per
scenario the state directory of the app it launched -- so the report shows
what the run used rather than resolving any of it a second time.
"""

import inspect
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest
from loguru import logger

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import SmokeCollectionError
from imbue.minds.smoke_tests.data_types import SmokeRunEnvironment
from imbue.minds.smoke_tests.data_types import SmokeRunManifestEntry
from imbue.minds.smoke_tests.data_types import SmokeRunReport
from imbue.minds.smoke_tests.data_types import SmokeRunVerdict
from imbue.minds.smoke_tests.data_types import SmokeScenarioOutcome
from imbue.minds.smoke_tests.data_types import SmokeScenarioResult
from imbue.minds.smoke_tests.data_types import SmokeSubtestResult
from imbue.minds.smoke_tests.data_types import SmokeTestPhase
from imbue.minds.smoke_tests.data_types import StudioBinary
from imbue.minds.smoke_tests.manifest import read_manifest_entries
from imbue.minds.smoke_tests.primitives import SMOKE_SENTRY_ORG_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.report import failure_summary_of
from imbue.minds.smoke_tests.report import github_source_url
from imbue.minds.smoke_tests.report import list_scenario_artifacts
from imbue.minds.smoke_tests.report import sentry_issues_url_or_none
from imbue.minds.smoke_tests.report import smoke_run_environment_of
from imbue.minds.smoke_tests.report import smoke_run_parameters_from_environ
from imbue.minds.smoke_tests.steps import STEPS_FILE_NAME
from imbue.minds.smoke_tests.steps import read_step_records
from imbue.minds.utils.sentry.core import ANONYMOUS_USER_ID_FILENAME
from imbue.minds.utils.sentry.core import sentry_deploy_environment_from_minds_env_name

# On the session's config: what the suite's session fixtures resolved.
SMOKE_BACKEND_KEY: Final[pytest.StashKey[SmokeBackend]] = pytest.StashKey[SmokeBackend]()
SMOKE_STUDIO_BINARY_KEY: Final[pytest.StashKey[StudioBinary]] = pytest.StashKey[StudioBinary]()
SMOKE_RUN_ID_KEY: Final[pytest.StashKey[SmokeRunId]] = pytest.StashKey[SmokeRunId]()
SMOKE_RUN_MANIFEST_KEY: Final[pytest.StashKey[Path]] = pytest.StashKey[Path]()
# On a scenario's item: the state directory of the app it launched, which holds the app's Sentry install id.
SMOKE_APP_STATE_DIR_KEY: Final[pytest.StashKey[Path]] = pytest.StashKey[Path]()

_SETUP_PHASE: Final[str] = "setup"
_CALL_PHASE: Final[str] = "call"
_TEARDOWN_PHASE: Final[str] = "teardown"
# Enough of a failing phase's output to show what led up to the failure.
_CAPTURED_OUTPUT_TAIL_LINE_COUNT: Final[int] = 200
_SKIP_REASON_PREFIX: Final[str] = "Skipped: "
_UNNAMED_SUBTEST_LABEL: Final[str] = "(subtest)"
_SESSION_NODE_ID: Final[str] = "(session)"


def smoke_run_report_of_session(
    session: pytest.Session,
    *,
    terminal_reporter: pytest.TerminalReporter,
    exit_status: int,
    outputs_dir: Path,
    verdict: SmokeRunVerdict,
    environ: Mapping[str, str],
    finished_at: datetime,
) -> SmokeRunReport:
    """The finished session as its report: every selected scenario, the run's inputs, and where and what it ran against.

    The results are read off the terminal reporter, which keeps every report of
    the session, the subtests' included, under the category it printed it as.
    """
    all_reports = tuple(report for reports in terminal_reporter.stats.values() for report in reports)
    test_reports = tuple(report for report in all_reports if isinstance(report, pytest.TestReport))
    config = session.config
    environment = smoke_run_environment_of(
        environ=environ,
        invocation_dir=config.invocation_params.dir,
        pytest_arguments=config.invocation_params.args,
        pytest_version=pytest.__version__,
    )
    backend = config.stash.get(SMOKE_BACKEND_KEY, None)
    scenarios = tuple(
        _scenario_of(
            item,
            tuple(report for report in test_reports if report.nodeid == item.nodeid),
            outputs_dir=outputs_dir,
            environment=environment,
            backend=backend,
            sentry_org=environ.get(SMOKE_SENTRY_ORG_ENV_VAR, ""),
        )
        for item in session.items
    )
    scenario_starts = tuple(scenario.started_at for scenario in scenarios if scenario.started_at is not None)
    return SmokeRunReport(
        run_id=config.stash.get(SMOKE_RUN_ID_KEY, None),
        backend=backend,
        studio_binary=config.stash.get(SMOKE_STUDIO_BINARY_KEY, None),
        started_at=min(scenario_starts) if scenario_starts else finished_at,
        finished_at=finished_at,
        exit_status=exit_status,
        environment=environment,
        parameters=smoke_run_parameters_from_environ(environ),
        scenarios=scenarios,
        collection_errors=tuple(
            _collection_error_of(report)
            for report in all_reports
            if isinstance(report, pytest.CollectReport) and report.failed
        ),
        deselected_count=NonNegativeInt(len(terminal_reporter.stats.get("deselected", ()))),
        verdict=verdict,
        created_resources=_created_resources_of(config.stash.get(SMOKE_RUN_MANIFEST_KEY, None)),
    )


def _scenario_of(
    item: pytest.Item,
    item_reports: Sequence[pytest.TestReport],
    *,
    outputs_dir: Path,
    environment: SmokeRunEnvironment,
    backend: SmokeBackend | None,
    sentry_org: str,
) -> SmokeScenarioResult:
    report_by_phase = {report.when: report for report in item_reports if not isinstance(report, pytest.SubtestReport)}
    subtests = tuple(
        _subtest_of(report)
        for report in sorted(
            (report for report in item_reports if isinstance(report, pytest.SubtestReport)),
            key=lambda report: report.start,
        )
    )
    outcome, failed_phase, deciding_report = _outcome_of(report_by_phase, subtests)
    started_at = _report_time(report_by_phase[_SETUP_PHASE].start) if _SETUP_PHASE in report_by_phase else None
    finished_at = _report_time(max(report.stop for report in item_reports)) if item_reports else None
    output_dir = outputs_dir / item.name
    artifacts = (
        list_scenario_artifacts(output_dir, report_dir=outputs_dir, written_since=started_at)
        if started_at is not None
        else ()
    )
    source_path, source_line = _source_location_of(item, environment)
    app_install_id = _app_install_id_or_none(item.stash.get(SMOKE_APP_STATE_DIR_KEY, None))
    is_failed = outcome in (SmokeScenarioOutcome.FAILED, SmokeScenarioOutcome.ERROR)
    return SmokeScenarioResult(
        node_id=NonEmptyStr(item.nodeid),
        name=NonEmptyStr(item.name),
        description=_description_of(item),
        source_path=NonEmptyStr(source_path),
        source_line=PositiveInt(source_line),
        source_url=(
            github_source_url(environment.repository_url, str(environment.checkout.git_sha), source_path, source_line)
            if environment.checkout is not None
            else None
        ),
        outcome=outcome,
        failed_phase=failed_phase,
        started_at=started_at,
        setup_seconds=_phase_seconds(report_by_phase, _SETUP_PHASE),
        call_seconds=_phase_seconds(report_by_phase, _CALL_PHASE),
        teardown_seconds=_phase_seconds(report_by_phase, _TEARDOWN_PHASE),
        summary=_summary_of(deciding_report) if deciding_report is not None else "",
        failure_text=deciding_report.longreprtext if deciding_report is not None and is_failed else "",
        captured_output=_captured_output_of(deciding_report) if deciding_report is not None and is_failed else "",
        subtests=subtests,
        steps=read_step_records(output_dir / STEPS_FILE_NAME) if started_at is not None else (),
        output_dir_relative_path=item.name if artifacts else None,
        artifacts=artifacts,
        app_install_id=app_install_id,
        sentry_url=(
            sentry_issues_url_or_none(
                sentry_org=sentry_org,
                sentry_environment=sentry_deploy_environment_from_minds_env_name(str(backend.env_name)),
                app_install_id=app_install_id,
                started_at=started_at,
                finished_at=finished_at,
            )
            if app_install_id is not None
            and backend is not None
            and started_at is not None
            and finished_at is not None
            else None
        ),
    )


def _outcome_of(
    report_by_phase: Mapping[str | None, pytest.TestReport], subtests: Sequence[SmokeSubtestResult]
) -> tuple[SmokeScenarioOutcome, SmokeTestPhase | None, pytest.TestReport | None]:
    """How the scenario ended, the phase it failed in, and the report that says why."""
    setup = report_by_phase.get(_SETUP_PHASE)
    call = report_by_phase.get(_CALL_PHASE)
    teardown = report_by_phase.get(_TEARDOWN_PHASE)
    if setup is None:
        return SmokeScenarioOutcome.NOT_RUN, None, None
    if setup.failed:
        return SmokeScenarioOutcome.ERROR, SmokeTestPhase.SETUP, setup
    if setup.skipped:
        return SmokeScenarioOutcome.SKIPPED, None, setup
    if call is None:
        return SmokeScenarioOutcome.NOT_RUN, None, None
    if call.failed or any(subtest.outcome == SmokeScenarioOutcome.FAILED for subtest in subtests):
        return SmokeScenarioOutcome.FAILED, SmokeTestPhase.CALL, call
    if call.skipped:
        return SmokeScenarioOutcome.SKIPPED, None, call
    if teardown is not None and teardown.failed:
        return SmokeScenarioOutcome.ERROR, SmokeTestPhase.TEARDOWN, teardown
    return SmokeScenarioOutcome.PASSED, None, None


def _subtest_of(report: pytest.SubtestReport) -> SmokeSubtestResult:
    if report.failed:
        outcome = SmokeScenarioOutcome.FAILED
    elif report.skipped:
        outcome = SmokeScenarioOutcome.SKIPPED
    else:
        outcome = SmokeScenarioOutcome.PASSED
    return SmokeSubtestResult(
        label=NonEmptyStr(_subtest_label_of(report)),
        outcome=outcome,
        duration_seconds=NonNegativeFloat(report.duration),
        summary=_summary_of(report) if not report.passed else "",
        failure_text=report.longreprtext if report.failed else "",
    )


def _subtest_label_of(report: pytest.SubtestReport) -> str:
    """The subtest's message, else its keyword arguments, as pytest labels it."""
    if report.context.msg:
        return report.context.msg
    if report.context.kwargs:
        return ", ".join(f"{name}={value}" for name, value in report.context.kwargs.items())
    return _UNNAMED_SUBTEST_LABEL


def _summary_of(report: pytest.TestReport) -> str:
    """Why a failed report failed, or why a skipped one skipped."""
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2]).removeprefix(_SKIP_REASON_PREFIX)
    return failure_summary_of(report.longreprtext)


def _captured_output_of(report: pytest.TestReport) -> str:
    """The end of each output the phase captured, under the title pytest gives it."""
    return "\n\n".join(f"----- {title} -----\n{_tail_of(text)}" for title, text in report.sections if text.strip())


def _tail_of(text: str) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= _CAPTURED_OUTPUT_TAIL_LINE_COUNT:
        return "\n".join(lines)
    dropped_count = len(lines) - _CAPTURED_OUTPUT_TAIL_LINE_COUNT
    return "\n".join((f"({dropped_count} earlier lines left out)", *lines[-_CAPTURED_OUTPUT_TAIL_LINE_COUNT:]))


def _phase_seconds(report_by_phase: Mapping[str | None, pytest.TestReport], phase: str) -> NonNegativeFloat:
    report = report_by_phase.get(phase)
    return NonNegativeFloat(report.duration if report is not None else 0.0)


def _report_time(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _description_of(item: pytest.Item) -> str:
    """The first paragraph of the test's docstring, on one line; empty when it has none."""
    docstring = inspect.getdoc(item.obj) if isinstance(item, pytest.Function) else None
    if not docstring:
        return ""
    return " ".join(docstring.split("\n\n", 1)[0].split())


def _source_location_of(item: pytest.Item, environment: SmokeRunEnvironment) -> tuple[str, int]:
    """The test's file relative to the checkout root (else to pytest's rootdir), and its 1-based first line."""
    item_path = item.path.resolve()
    checkout = environment.checkout
    if checkout is not None and item_path.is_relative_to(checkout.root_dir.resolve()):
        relative_path = item_path.relative_to(checkout.root_dir.resolve()).as_posix()
    else:
        relative_path = Path(item.location[0]).as_posix()
    line_idx = item.location[1]
    return relative_path, (line_idx if line_idx is not None else 0) + 1


def _app_install_id_or_none(app_state_dir: Path | None) -> str | None:
    """The Sentry install id the app wrote under its state directory, read without minting one."""
    if app_state_dir is None:
        return None
    id_path = app_state_dir / ANONYMOUS_USER_ID_FILENAME
    if not id_path.is_file():
        return None
    return id_path.read_text().strip() or None


def _created_resources_of(manifest_path: Path | None) -> tuple[SmokeRunManifestEntry, ...]:
    if manifest_path is None:
        return ()
    try:
        return read_manifest_entries(manifest_path)
    except SmokeConfigError as exc:
        logger.warning("The run report lists no created resources: the run manifest is unreadable ({})", exc)
        return ()


def _collection_error_of(report: pytest.CollectReport) -> SmokeCollectionError:
    return SmokeCollectionError(
        node_id=NonEmptyStr(report.nodeid or _SESSION_NODE_ID),
        summary=failure_summary_of(report.longreprtext),
        failure_text=report.longreprtext,
    )
