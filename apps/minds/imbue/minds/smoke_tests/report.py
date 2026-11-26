"""The HTML report a smoke session writes beside its verdict: what ran, against what, and how each scenario went.

The report is one level of detail below the verdict. Its run section says when
the session ran, where (the CI run or the machine), from which commit, against
which tier and build, and which of the suite's inputs the environment set. Its
scenario section lists every selected scenario, failures first, each with its
duration, the reason it failed or skipped, the steps it got through, and links
into the evidence: the test's source at the run's commit, the screenshots and
logs it left, and the Sentry issues the app it launched reported.

This module builds and renders the report from plain data; the pytest session
is read in ``testing.py``.
"""

import itertools
import platform
import re
import socket
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import StrictUndefined
from loguru import logger
from pydantic import AnyUrl

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.data_types import SmokeArtifact
from imbue.minds.smoke_tests.data_types import SmokeArtifactKind
from imbue.minds.smoke_tests.data_types import SmokeRunCheckout
from imbue.minds.smoke_tests.data_types import SmokeRunEnvironment
from imbue.minds.smoke_tests.data_types import SmokeRunParameter
from imbue.minds.smoke_tests.data_types import SmokeRunParameterSpec
from imbue.minds.smoke_tests.data_types import SmokeRunReport
from imbue.minds.smoke_tests.data_types import SmokeScenarioOutcome
from imbue.minds.smoke_tests.data_types import SmokeScenarioResult
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_BINARY_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_CHANNEL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_DEV_CHECKOUT_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_HANDOFF_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_MANIFEST_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_PROFILE_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RESTORE_FROM_STORAGE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RUN_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_SENTRY_ORG_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_SHARE_GRANTEE_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_SKIP_DWT_TESTS_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.utils.sentry.core import SentryDeployEnvironment
from imbue.minds.utils.sentry.core import sentry_deploy_environment_from_minds_env_name
from imbue.minds.utils.sentry.core import sentry_dsn_for_environment
from imbue.minds.utils.sentry.frontend import frontend_sentry_dsn_for_environment


def _parameter_spec(env_var: str, meaning: str, *, is_value_hidden: bool) -> SmokeRunParameterSpec:
    return SmokeRunParameterSpec(
        env_var=NonEmptyStr(env_var), meaning=NonEmptyStr(meaning), is_value_hidden=is_value_hidden
    )


# Every input a smoke session reads from its environment (``primitives.py`` is the contract), in the
# order a reader asks about them.
SMOKE_RUN_PARAMETER_SPECS: Final[tuple[SmokeRunParameterSpec, ...]] = (
    _parameter_spec(
        SMOKE_TIER_ENV_VAR,
        "The deployed env under test.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_CHANNEL_ENV_VAR,
        "The release channel whose published build is under test.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_BINARY_PATH_ENV_VAR,
        "An Imbue Studio executable under test; wins over the other build variables.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_DEV_CHECKOUT_ENV_VAR,
        "1 tests this checkout's Electron; wins over a channel.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_SKIP_DWT_TESTS_ENV_VAR,
        "1 skips default-workspace-template's CI suites in the journey's workspace.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR,
        "The template tag the journey's pool row must have been baked at.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_RESTORE_FROM_STORAGE_ENV_VAR,
        "1 has step 4b wait until the stopped workspace has left its server, so the start restores it from storage.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_SHARE_GRANTEE_EMAIL_ENV_VAR,
        "The address step 5b grants the journey's workspace to, which the tier emails an invitation.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_RUN_ID_ENV_VAR,
        "The id naming the run's workspaces, manifest and Vault handoff.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_HANDOFF_FILE_ENV_VAR,
        "The state file the just minds-smoke-setup recipe wrote: the run's throwaway account.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_MANIFEST_PATH_ENV_VAR,
        "Where the run records the resources it creates, for teardown.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_PROFILE_FILE_ENV_VAR,
        "Where the app under test records a Perfetto trace of the run.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_ACCOUNT_EMAIL_ENV_VAR,
        "The tier's persistent account, which the journey acts as.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_ACCOUNT_PASSWORD_ENV_VAR,
        "The persistent account's password.",
        is_value_hidden=True,
    ),
    _parameter_spec(
        SMOKE_THROWAWAY_EMAIL_ENV_VAR,
        "The run's throwaway account, which the signup scenario verifies.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        "PYTEST_MAX_DURATION_SECONDS",
        "The session's time budget, in seconds.",
        is_value_hidden=False,
    ),
    _parameter_spec(
        SMOKE_SENTRY_ORG_ENV_VAR,
        "The Sentry organization this report links the app's errors into.",
        is_value_hidden=False,
    ),
)


@pure
def smoke_run_parameters_from_environ(environ: Mapping[str, str]) -> tuple[SmokeRunParameter, ...]:
    """Every input the suite reads, with what ``environ`` gives it; an empty value counts as unset, as the suite reads it."""
    return tuple(_parameter_from_environ(spec, environ) for spec in SMOKE_RUN_PARAMETER_SPECS)


@pure
def _parameter_from_environ(spec: SmokeRunParameterSpec, environ: Mapping[str, str]) -> SmokeRunParameter:
    value = environ.get(str(spec.env_var), "")
    is_set = value != ""
    return SmokeRunParameter(
        spec=spec, is_set=is_set, shown_value=value if is_set and not spec.is_value_hidden else None
    )


# pytest marks the lines of the exception it reports with an ``E`` gutter.
_PYTEST_EXCEPTION_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^E(?:\s|$)")
_NATIVE_TRACEBACK_HEADER: Final[str] = "Traceback (most recent call last):"
# Enough to say why a scenario failed; the full text is a click away.
_SUMMARY_MAX_LINE_COUNT: Final[int] = 15
_SUMMARY_MAX_CHARACTER_COUNT: Final[int] = 2000


@pure
def failure_summary_of(failure_text: str) -> str:
    """The few lines that say why a test failed: the last exception pytest marked, else the text itself."""
    summary_lines = _failure_summary_lines(failure_text.strip().splitlines())
    return "\n".join(summary_lines[:_SUMMARY_MAX_LINE_COUNT])[:_SUMMARY_MAX_CHARACTER_COUNT]


@pure
def _failure_summary_lines(lines: Sequence[str]) -> Sequence[str]:
    marked_runs = [
        tuple(run) for is_marked, run in itertools.groupby(lines, key=_is_pytest_exception_line) if is_marked
    ]
    if marked_runs:
        return tuple(line[1:].strip() for line in marked_runs[-1])
    if lines and lines[0] == _NATIVE_TRACEBACK_HEADER:
        return lines[-1:]
    return lines


@pure
def _is_pytest_exception_line(line: str) -> bool:
    return _PYTEST_EXCEPTION_LINE_PATTERN.match(line) is not None


# Where a run outside CI links its source: the repository the smoke suite is developed in.
_CANONICAL_REPOSITORY_URL: Final[str] = "https://github.com/imbue-ai/mngr-internal"
_GITHUB_ACTIONS_ENABLED_VALUE: Final[str] = "true"
_SENTRY_ISSUES_URL_PREFIX: Final[str] = "https://sentry.io/organizations"
# The window brackets the scenario generously: this machine's clock and Sentry's can disagree, and an
# event reaches Sentry a while after the app raised it.
_SENTRY_WINDOW_LEAD: Final[timedelta] = timedelta(minutes=5)
_SENTRY_WINDOW_LAG: Final[timedelta] = timedelta(hours=1)
_SENTRY_TIME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"


@pure
def _is_github_actions(environ: Mapping[str, str]) -> bool:
    return environ.get("GITHUB_ACTIONS", "") == _GITHUB_ACTIONS_ENABLED_VALUE


@pure
def repository_url_from_environ(environ: Mapping[str, str]) -> AnyUrl:
    """The repository the run's source links point into: the CI run's own, else the canonical one."""
    if _is_github_actions(environ):
        return AnyUrl(f"{environ['GITHUB_SERVER_URL']}/{environ['GITHUB_REPOSITORY']}")
    return AnyUrl(_CANONICAL_REPOSITORY_URL)


@pure
def github_source_url(repository_url: AnyUrl, git_sha: str, source_path: str, source_line: int) -> AnyUrl:
    return AnyUrl(f"{_github_blob_url(repository_url, git_sha, source_path)}#L{source_line}")


@pure
def _github_blob_url(repository_url: AnyUrl, git_ref: str, path: str) -> str:
    return f"{str(repository_url).rstrip('/')}/blob/{git_ref}/{quote(path)}"


@pure
def github_commit_url(repository_url: AnyUrl, git_sha: str) -> AnyUrl:
    return AnyUrl(f"{str(repository_url).rstrip('/')}/commit/{git_sha}")


@pure
def ci_run_url_from_environ(environ: Mapping[str, str]) -> AnyUrl | None:
    """The GitHub Actions run, at the attempt the session ran in; None outside CI."""
    if not _is_github_actions(environ):
        return None
    return AnyUrl(
        f"{environ['GITHUB_SERVER_URL']}/{environ['GITHUB_REPOSITORY']}/actions/runs/"
        f"{environ['GITHUB_RUN_ID']}/attempts/{environ['GITHUB_RUN_ATTEMPT']}"
    )


@pure
def ci_run_description_from_environ(environ: Mapping[str, str]) -> str:
    """The workflow, run, attempt, job and runner the session ran in; empty outside CI."""
    if not _is_github_actions(environ):
        return ""
    return (
        f"{environ['GITHUB_WORKFLOW']} run {environ['GITHUB_RUN_ID']}, attempt {environ['GITHUB_RUN_ATTEMPT']}, "
        f"job {environ['GITHUB_JOB']}, on runner {environ['RUNNER_NAME']}"
    )


@pure
def sentry_issues_url_or_none(
    *,
    sentry_org: str,
    sentry_environment: SentryDeployEnvironment,
    # The ``user.id`` the app tags every event with: minted per data home, so it names this run's app alone.
    app_install_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> AnyUrl | None:
    """The Sentry issues the app's backend and frontend reported around a scenario; None without an org."""
    if not sentry_org:
        return None
    query = urlencode(
        (
            ("project", _sentry_project_id_of(sentry_dsn_for_environment(sentry_environment))),
            ("project", _sentry_project_id_of(frontend_sentry_dsn_for_environment(sentry_environment))),
            ("query", f"user.id:{app_install_id}"),
            ("start", (started_at.astimezone(timezone.utc) - _SENTRY_WINDOW_LEAD).strftime(_SENTRY_TIME_FORMAT)),
            ("end", (finished_at.astimezone(timezone.utc) + _SENTRY_WINDOW_LAG).strftime(_SENTRY_TIME_FORMAT)),
            ("utc", "true"),
        )
    )
    return AnyUrl(f"{_SENTRY_ISSUES_URL_PREFIX}/{quote(sentry_org)}/issues/?{query}")


@pure
def _sentry_project_id_of(dsn: str) -> str:
    """A DSN's project id: its path, ``https://<key>@<org>.ingest.sentry.io/<project id>``."""
    return urlsplit(dsn).path.strip("/")


_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
# Deeper files, such as the template suites' per-attempt reports, are reached through the directory's link.
_ARTIFACT_GLOBS: Final[tuple[str, ...]] = ("*", "*/*")
# A file's modification time can trail the clock it is compared against by a filesystem's granularity.
_MODIFICATION_TIME_TOLERANCE: Final[timedelta] = timedelta(seconds=2)


def list_scenario_artifacts(
    output_dir: Path, *, report_dir: Path, written_since: datetime
) -> tuple[SmokeArtifact, ...]:
    """The files a scenario wrote to ``output_dir`` since ``written_since``, at most two levels deep.

    Earlier runs leave their files in the same directory, so a file this run
    did not write is not this run's evidence.
    """
    if not output_dir.is_dir():
        return ()
    oldest_timestamp = (written_since - _MODIFICATION_TIME_TOLERANCE).timestamp()
    written_paths = sorted(
        path
        for pattern in _ARTIFACT_GLOBS
        for path in output_dir.glob(pattern)
        if path.is_file() and path.stat().st_mtime >= oldest_timestamp
    )
    return tuple(
        SmokeArtifact(
            relative_path=NonEmptyStr(path.relative_to(report_dir).as_posix()),
            kind=SmokeArtifactKind.IMAGE if path.suffix.lower() in _IMAGE_SUFFIXES else SmokeArtifactKind.FILE,
        )
        for path in written_paths
    )


SMOKE_REPORT_FILE_NAME: Final[str] = "report.html"
_REPORT_TEMPLATE_NAME: Final[str] = "report.html.j2"
_JINJA_ENVIRONMENT: Final[Environment] = Environment(
    loader=PackageLoader("imbue.minds.smoke_tests", "report_assets"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
# Failures first: they are what a reader opens the report for.
_SCENARIO_GROUP_TITLES: Final[tuple[tuple[SmokeScenarioOutcome, str], ...]] = (
    (SmokeScenarioOutcome.FAILED, "Failed"),
    (SmokeScenarioOutcome.ERROR, "Errored in setup or teardown"),
    (SmokeScenarioOutcome.NOT_RUN, "Not run"),
    (SmokeScenarioOutcome.PASSED, "Passed"),
    (SmokeScenarioOutcome.SKIPPED, "Skipped"),
)
# Passes and skips start folded, so the page opens on what went wrong.
_FOLDED_SCENARIO_OUTCOMES: Final[frozenset[SmokeScenarioOutcome]] = frozenset(
    {SmokeScenarioOutcome.PASSED, SmokeScenarioOutcome.SKIPPED}
)
_FAILING_OUTCOMES: Final[frozenset[SmokeScenarioOutcome]] = frozenset(
    {SmokeScenarioOutcome.FAILED, SmokeScenarioOutcome.ERROR}
)
_VERDICT_WORD_BY_EXIT_STATUS: Final[dict[int, str]] = {0: "PASSED", 1: "FAILED", 2: "INTERRUPTED", 5: "EMPTY"}
_UNEXPECTED_EXIT_VERDICT_WORD: Final[str] = "ERRORED"
# A session that exits cleanly with nothing passed proved nothing about the tier, so it is not called a pass.
_ALL_SKIPPED_VERDICT_WORD: Final[str] = "SKIPPED"
_EXIT_STATUS_MEANINGS: Final[dict[int, str]] = {
    0: "every selected scenario passed or skipped",
    1: "a scenario failed",
    2: "the session was interrupted",
    3: "pytest hit an internal error",
    4: "pytest was invoked wrongly",
    5: "no scenario was collected",
}
_UNKNOWN_EXIT_STATUS_MEANING: Final[str] = "not a status pytest documents"
# Linked from the report's footer.
_TESTING_OVERVIEW_PATH: Final[str] = "apps/minds/docs/testing-overview.md"
_TESTING_OVERVIEW_SMOKE_ANCHOR: Final[str] = "110-smoke-tests-appsmindssmoke_tests"
_SMOKE_CONFTEST_PATH: Final[str] = "apps/minds/smoke_tests/conftest.py"
_DEFAULT_BRANCH: Final[str] = "main"
_SCENARIO_ANCHOR_TEMPLATE: Final[str] = "scenario-{idx}"


def render_smoke_run_report_html(report: SmokeRunReport) -> str:
    """The report as one self-contained HTML page; the files it links to are relative to where it is written."""
    anchored_scenarios = tuple(
        (_SCENARIO_ANCHOR_TEMPLATE.format(idx=idx), scenario) for idx, scenario in enumerate(report.scenarios, start=1)
    )
    git_ref = str(report.environment.checkout.git_sha) if report.environment.checkout is not None else _DEFAULT_BRANCH
    sentry_environment = (
        sentry_deploy_environment_from_minds_env_name(str(report.backend.env_name))
        if report.backend is not None
        else None
    )
    return _JINJA_ENVIRONMENT.get_template(_REPORT_TEMPLATE_NAME).render(
        report=report,
        verdict_word=_verdict_word_of(report),
        exit_status_meaning=_EXIT_STATUS_MEANINGS.get(report.exit_status, _UNKNOWN_EXIT_STATUS_MEANING),
        headline=_headline_of(report),
        failing_anchors=tuple(
            (anchor, scenario) for anchor, scenario in anchored_scenarios if scenario.outcome in _FAILING_OUTCOMES
        ),
        scenario_groups=_scenario_groups_of(anchored_scenarios),
        sentry_project_names=(
            (f"minds-{sentry_environment.value}", f"minds-frontend-{sentry_environment.value}")
            if sentry_environment is not None
            else None
        ),
        docs_url=(
            f"{_github_blob_url(report.environment.repository_url, git_ref, _TESTING_OVERVIEW_PATH)}"
            f"#{_TESTING_OVERVIEW_SMOKE_ANCHOR}"
        ),
        conftest_url=_github_blob_url(report.environment.repository_url, git_ref, _SMOKE_CONFTEST_PATH),
        format_duration=format_duration,
        format_utc_time=format_utc_time,
    )


def write_smoke_run_report(report_path: Path, report: SmokeRunReport) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_smoke_run_report_html(report), encoding="utf-8")


@pure
def _scenario_groups_of(
    anchored_scenarios: Sequence[tuple[str, SmokeScenarioResult]],
) -> tuple[tuple[SmokeScenarioOutcome, str, bool, tuple[tuple[str, SmokeScenarioResult], ...]], ...]:
    """The scenarios by outcome, failures first, each with whether its fold starts open; outcomes no scenario had are left out."""
    groups = tuple(
        (
            outcome,
            title,
            outcome not in _FOLDED_SCENARIO_OUTCOMES,
            tuple(entry for entry in anchored_scenarios if entry[1].outcome == outcome),
        )
        for outcome, title in _SCENARIO_GROUP_TITLES
    )
    return tuple(group for group in groups if group[3])


@pure
def _verdict_word_of(report: SmokeRunReport) -> str:
    is_nothing_passed = all(scenario.outcome != SmokeScenarioOutcome.PASSED for scenario in report.scenarios)
    if report.exit_status == 0 and is_nothing_passed:
        return _ALL_SKIPPED_VERDICT_WORD
    return _VERDICT_WORD_BY_EXIT_STATUS.get(report.exit_status, _UNEXPECTED_EXIT_VERDICT_WORD)


@pure
def _headline_of(report: SmokeRunReport) -> str:
    """One sentence: how many scenarios failed, or passed, and what else the reader should know first."""
    count_by_outcome = Counter(scenario.outcome for scenario in report.scenarios)
    scenario_count = len(report.scenarios)
    failing_count = sum(count_by_outcome[outcome] for outcome in _FAILING_OUTCOMES)
    not_run_count = count_by_outcome[SmokeScenarioOutcome.NOT_RUN]
    passed_count = count_by_outcome[SmokeScenarioOutcome.PASSED]
    skipped_count = count_by_outcome[SmokeScenarioOutcome.SKIPPED]
    noun = "scenario" if scenario_count == 1 else "scenarios"
    if scenario_count == 0:
        lead = "No scenario ran"
    elif failing_count:
        lead = f"{failing_count} of {scenario_count} {noun} failed"
    elif not_run_count:
        lead = f"{not_run_count} of {scenario_count} {noun} did not run"
    elif passed_count == 0:
        lead = "The only scenario skipped" if scenario_count == 1 else f"All {scenario_count} {noun} skipped"
    else:
        lead = f"{passed_count} of {scenario_count} {noun} passed"
    error_count = len(report.collection_errors)
    extra_phrases = (
        f"{skipped_count} skipped" if skipped_count and passed_count + failing_count + not_run_count else "",
        f"{not_run_count} not run" if not_run_count and failing_count else "",
        f"{error_count} test file{'' if error_count == 1 else 's'} could not be collected" if error_count else "",
    )
    return ", ".join((lead, *(phrase for phrase in extra_phrases if phrase)))


@pure
def format_duration(seconds: float) -> str:
    """``4.2s``, ``42s``, ``8m 03s`` or ``1h 02m``: precise where it matters, short where it does not."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    whole_seconds = round(seconds)
    if whole_seconds < 60:
        return f"{whole_seconds}s"
    minutes, remaining_seconds = divmod(whole_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes:02d}m"


@pure
def format_utc_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_GIT_TIMEOUT_SECONDS: Final[float] = 30.0
# What ``git rev-parse --abbrev-ref HEAD`` answers for a detached HEAD.
_DETACHED_HEAD_NAME: Final[str] = "HEAD"
_UNKNOWN_HOST_NAME: Final[str] = "unknown host"


def smoke_run_environment_of(
    *, environ: Mapping[str, str], invocation_dir: Path, pytest_arguments: Sequence[str], pytest_version: str
) -> SmokeRunEnvironment:
    """Where the session runs (this machine, and the CI run if any), from which commit, and how pytest was invoked."""
    repository_url = repository_url_from_environ(environ)
    return SmokeRunEnvironment(
        host_name=NonEmptyStr(socket.gethostname() or _UNKNOWN_HOST_NAME),
        platform_description=NonEmptyStr(platform.platform()),
        python_version=NonEmptyStr(platform.python_version()),
        pytest_version=NonEmptyStr(pytest_version),
        pytest_arguments=tuple(pytest_arguments),
        ci_run_url=ci_run_url_from_environ(environ),
        ci_run_description=ci_run_description_from_environ(environ),
        repository_url=repository_url,
        checkout=smoke_run_checkout_or_none(invocation_dir, repository_url),
    )


def smoke_run_checkout_or_none(directory: Path, repository_url: AnyUrl) -> SmokeRunCheckout | None:
    """The commit of the checkout holding ``directory``, or None when git cannot describe one there."""
    with ConcurrencyGroup(name="smoke-report-git") as concurrency_group:
        description = _git_output_or_none(
            concurrency_group, directory, ("rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD")
        )
        description_lines = description.splitlines() if description is not None else []
        if len(description_lines) != 3:
            return None
        root_dir, git_sha, branch_name = description_lines
        status = _git_output_or_none(concurrency_group, directory, ("status", "--porcelain", "--untracked-files=no"))
        remote_branches = _git_output_or_none(
            concurrency_group, directory, ("branch", "--remotes", "--contains", git_sha)
        )
    return SmokeRunCheckout(
        root_dir=Path(root_dir),
        git_sha=NonEmptyStr(git_sha),
        branch_name="" if branch_name == _DETACHED_HEAD_NAME else branch_name,
        is_dirty=bool(status and status.strip()),
        is_pushed=bool(remote_branches and remote_branches.strip()),
        commit_url=github_commit_url(repository_url, git_sha),
    )


def _git_output_or_none(concurrency_group: ConcurrencyGroup, directory: Path, arguments: Sequence[str]) -> str | None:
    finished = concurrency_group.run_process_to_completion(
        command=("git", *arguments), timeout=_GIT_TIMEOUT_SECONDS, is_checked_after=False, cwd=directory
    )
    if finished.returncode != 0:
        logger.debug(
            "git {} exited {} in {}: {}", " ".join(arguments), finished.returncode, directory, finished.stderr.strip()
        )
        return None
    return finished.stdout
