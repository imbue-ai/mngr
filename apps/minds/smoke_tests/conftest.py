"""Fixtures of the Imbue Studio smoke suite: the target, the accounts, and the launched app.

Everything a run needs arrives through the environment (``primitives.py`` in
``imbue/minds/smoke_tests`` is the contract), and every fixture that cannot
run skips with a sentence naming the missing input, so a stray collection of
these tests outside ``just minds-smoke`` reports what it lacks instead of
failing. The suite's test job holds only user credentials: the persistent
account and the run's throwaway account.
"""

import os
import pwd
import shutil
import sys
import time
from collections.abc import Generator
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import httpx2
import pytest

from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests.primitives import SignupEmailAddress
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.smoke_tests.accounts import SMOKE_USER_AGENT
from imbue.minds.smoke_tests.accounts import persistent_account_credentials_from_env_or_none
from imbue.minds.smoke_tests.accounts import read_persistent_account_credentials_or_none
from imbue.minds.smoke_tests.app_flows import screenshot_every_page
from imbue.minds.smoke_tests.backend import resolve_smoke_backend
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.backend import stop_retention_wait_seconds
from imbue.minds.smoke_tests.binary import BINARY_SELECTION_ENV_VARS
from imbue.minds.smoke_tests.binary import is_studio_binary_selected
from imbue.minds.smoke_tests.binary import launched_studio_app
from imbue.minds.smoke_tests.binary import resolve_studio_binary
from imbue.minds.smoke_tests.browser import PLAYWRIGHT_BROWSERS_PATH_ENV_VAR
from imbue.minds.smoke_tests.browser import default_playwright_browsers_dir
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import SmokeRunHandoff
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.data_types import StudioBinary
from imbue.minds.smoke_tests.data_types import ThrowawayAccount
from imbue.minds.smoke_tests.dwt_suites import is_dwt_tests_skip_requested
from imbue.minds.smoke_tests.dwt_suites import smoke_run_verdict
from imbue.minds.smoke_tests.handoff import read_handoff_state_file_or_none
from imbue.minds.smoke_tests.handoff import resolve_test_job_handoff_or_none
from imbue.minds.smoke_tests.manifest import default_manifest_path
from imbue.minds.smoke_tests.manifest import resolve_manifest_path_or_none
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_HANDOFF_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RESTORE_FROM_STORAGE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RUN_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.primitives import is_smoke_switch_on
from imbue.minds.smoke_tests.report import SMOKE_REPORT_FILE_NAME
from imbue.minds.smoke_tests.report import write_smoke_run_report
from imbue.minds.smoke_tests.steps import STEPS_FILE_NAME
from imbue.minds.smoke_tests.testing import SMOKE_APP_STATE_DIR_KEY
from imbue.minds.smoke_tests.testing import SMOKE_BACKEND_KEY
from imbue.minds.smoke_tests.testing import SMOKE_RUN_ID_KEY
from imbue.minds.smoke_tests.testing import SMOKE_RUN_MANIFEST_KEY
from imbue.minds.smoke_tests.testing import SMOKE_STUDIO_BINARY_KEY
from imbue.minds.smoke_tests.testing import smoke_run_report_of_session

# Captured from /etc/passwd at import, before the autouse HOME redirect, so the
# browsers ``playwright install chromium`` put under the real home are found.
_REAL_HOME: Final[Path] = Path(pwd.getpwuid(os.getuid()).pw_dir)
_LINUX_PLATFORM_NAME: Final[str] = "linux"
_SMOKE_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
# Where a run's artifacts land when the environment names no place for them.
_SMOKE_OUTPUT_DIR_NAME: Final[str] = "smoke"
# Where a failed test's copy of the app's own logs lands, under its output directory.
_APP_LOGS_DIR_NAME: Final[str] = "app-logs"
# A run minted here, with no handoff to take an id from, is named by its start time.
_MINTED_RUN_ID_FORMAT: Final[str] = "%Y%m%dt%H%M%Sz"
_TEST_OUTPUTS_DIR_NAME: Final[str] = ".test_output"
_STUDIO_APP_FIXTURE_NAME: Final[str] = "studio_app"
# What failed in the session, by kind, for the workflow deciding whether a rerun could help.
_VERDICT_FILE_NAME: Final[str] = "verdict.json"
# The tests that failed on their own rather than through a failed subtest, as ``<node id> (<phase>)``.
_OWN_FAILURES_KEY: Final[pytest.StashKey[list[str]]] = pytest.StashKey[list[str]]()
# Set while a test's body runs. pytest makes each subtest's report while the body runs, through the same
# hook as the test's own reports, so a report made meanwhile is a subtest's.
_IS_BODY_RUNNING_KEY: Final[pytest.StashKey[bool]] = pytest.StashKey[bool]()


@pytest.fixture(scope="session")
def backend(request: pytest.FixtureRequest) -> SmokeBackend:
    """The deployed environment under test, named by ``MINDS_SMOKE_TIER``."""
    tier = os.environ.get(SMOKE_TIER_ENV_VAR, "")
    if not tier:
        pytest.skip(f"{SMOKE_TIER_ENV_VAR} is not set; run the smoke suite with `just minds-smoke <tier>`.")
    env_name = DevEnvName(tier)
    resolved_backend = resolve_smoke_backend(env_name, resolve_smoke_client_config_path(env_name))
    request.config.stash[SMOKE_BACKEND_KEY] = resolved_backend
    return resolved_backend


@pytest.fixture(scope="session")
def studio_binary(request: pytest.FixtureRequest, backend: SmokeBackend) -> StudioBinary:
    """The Imbue Studio build under test, resolved once per run (a channel's build is downloaded here)."""
    if not is_studio_binary_selected(os.environ):
        pytest.skip(
            f"No Imbue Studio build is named; set one of {', '.join(BINARY_SELECTION_ENV_VARS)} "
            "(MINDS_SMOKE_DEV_CHECKOUT=1 runs this checkout's Electron)."
        )
    resolved_binary = resolve_studio_binary(backend, os.environ)
    request.config.stash[SMOKE_STUDIO_BINARY_KEY] = resolved_binary
    return resolved_binary


@pytest.fixture(scope="session")
def is_dwt_tests_skipped() -> bool:
    """Whether the journey skips its default-workspace-template steps, by ``MINDS_SMOKE_SKIP_DWT_TESTS``."""
    return is_dwt_tests_skip_requested(os.environ)


@pytest.fixture(scope="session")
def storage_restore_wait_seconds(backend: SmokeBackend) -> float | None:
    """How long the journey waits for its stopped workspace to leave its server, so that its start restores it from
    object storage (``MINDS_SMOKE_RESTORE_FROM_STORAGE=1``); None when the journey starts the workspace at once."""
    if not is_smoke_switch_on(os.environ, SMOKE_RESTORE_FROM_STORAGE_ENV_VAR, "restore", "restart in place"):
        return None
    return stop_retention_wait_seconds(backend.stop_retention_seconds)


@pytest.fixture(scope="session")
def expected_template_ref() -> str | None:
    """The template tag the journey's workspace must lease a row baked at (``MINDS_SMOKE_EXPECTED_TEMPLATE_REF``), or None."""
    return os.environ.get(SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR, "").strip() or None


@pytest.fixture(scope="session")
def persistent_account(backend: SmokeBackend) -> SmokeAccountCredentials:
    """The tier's persistent smoke account: exported by the CI job, else read from the tier's Vault leaves."""
    from_env = persistent_account_credentials_from_env_or_none(os.environ)
    if from_env is not None:
        return from_env
    tier = str(backend.tier)
    try:
        from_vault = read_persistent_account_credentials_or_none(tier)
    except VaultReadError as exc:
        pytest.skip(
            f"The persistent smoke account is in neither {SMOKE_ACCOUNT_EMAIL_ENV_VAR}/{SMOKE_ACCOUNT_PASSWORD_ENV_VAR} "
            f"nor a readable Vault entry ({exc})."
        )
    if from_vault is None:
        pytest.skip(
            f"The {tier} tier has no persistent smoke account yet; "
            f"run `just minds-smoke-create-persistent-account {tier}` first."
        )
    return from_vault


@pytest.fixture(scope="session")
def _smoke_run_handoff(backend: SmokeBackend) -> SmokeRunHandoff:
    """The run's throwaway account and mailbox, from wherever the setup command handed them off."""
    tier = str(backend.tier)
    try:
        handoff = resolve_test_job_handoff_or_none(os.environ, tier=tier)
    except VaultReadError as exc:
        pytest.skip(f"The run's handoff could not be read from Vault ({exc}).")
    if handoff is None:
        pytest.skip(
            "This run has no throwaway account: export the handoff, or set "
            f"{SMOKE_HANDOFF_FILE_ENV_VAR} to the file `just minds-smoke-setup {tier} <run-id> <file>` wrote, "
            f"or {SMOKE_RUN_ID_ENV_VAR} to a run whose handoff is in Vault."
        )
    if str(handoff.tier) != tier:
        raise SmokeConfigError(
            f"The handoff of run {handoff.run_id} was minted on the {handoff.tier} tier, "
            f"but this run targets {tier}; re-run the smoke setup for this tier."
        )
    return handoff


@pytest.fixture(scope="session")
def throwaway_account(_smoke_run_handoff: SmokeRunHandoff) -> ThrowawayAccount:
    """The unverified account the setup command minted for this run."""
    return _smoke_run_handoff.throwaway


@pytest.fixture(scope="session")
def throwaway_inbox(_smoke_run_handoff: SmokeRunHandoff) -> MailtmInbox:
    """The mail.tm mailbox that is the throwaway account's address."""
    return MailtmInbox(
        address=SignupEmailAddress(str(_smoke_run_handoff.mailtm_address)),
        account_address=_smoke_run_handoff.mailtm_address,
        jwt=_smoke_run_handoff.mailtm_jwt,
    )


@pytest.fixture(scope="session")
def smoke_run_id(request: pytest.FixtureRequest) -> SmokeRunId:
    """This run's id: the environment's, else the handoff state file's, else one minted now.

    Names the run's workspaces and its manifest, so a local run without a
    handoff still gets a manifest of its own.
    """
    resolved_run_id = _resolve_smoke_run_id()
    request.config.stash[SMOKE_RUN_ID_KEY] = resolved_run_id
    return resolved_run_id


def _resolve_smoke_run_id() -> SmokeRunId:
    from_env = os.environ.get(SMOKE_RUN_ID_ENV_VAR, "")
    if from_env:
        return SmokeRunId(from_env)
    handoff_file = os.environ.get(SMOKE_HANDOFF_FILE_ENV_VAR, "")
    if handoff_file:
        handoff = read_handoff_state_file_or_none(Path(handoff_file))
        if handoff is not None:
            return SmokeRunId(str(handoff.run_id))
    return SmokeRunId(time.strftime(_MINTED_RUN_ID_FORMAT, time.gmtime()))


@pytest.fixture(scope="session")
def run_manifest(request: pytest.FixtureRequest, smoke_run_id: SmokeRunId) -> Path:
    """Where this run records the resources it creates, for teardown to release."""
    from_env = resolve_manifest_path_or_none()
    manifest_path = (
        from_env if from_env is not None else default_manifest_path(_smoke_outputs_dir(request.config), smoke_run_id)
    )
    request.config.stash[SMOKE_RUN_MANIFEST_KEY] = manifest_path
    return manifest_path


@pytest.fixture(scope="session")
def smoke_http_client() -> Iterator[httpx2.Client]:
    """A client for the read-only probes: labelled as the smoke suite, and never following a redirect it asserts on."""
    with httpx2.Client(
        timeout=_SMOKE_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": SMOKE_USER_AGENT}, follow_redirects=False
    ) as client:
        yield client


@pytest.fixture(scope="session")
def smoke_display(request: pytest.FixtureRequest) -> str | None:
    """A display for the app: Xvfb's on Linux (started if none is set), the window server's elsewhere."""
    if sys.platform == _LINUX_PLATFORM_NAME:
        return request.getfixturevalue("xvfb_display")
    return None


@pytest.fixture(autouse=True)
def _playwright_browsers_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Playwright at the browsers installed under the real home; the test HOME redirect would hide them."""
    if os.environ.get(PLAYWRIGHT_BROWSERS_PATH_ENV_VAR):
        return
    monkeypatch.setenv(
        PLAYWRIGHT_BROWSERS_PATH_ENV_VAR, str(default_playwright_browsers_dir(_REAL_HOME, sys.platform))
    )


def _smoke_outputs_dir(config: pytest.Config) -> Path:
    """``.test_output/smoke`` under the directory pytest was invoked from (the checkout root under ``just``)."""
    return config.invocation_params.dir / _TEST_OUTPUTS_DIR_NAME / _SMOKE_OUTPUT_DIR_NAME


def _output_dir_for(item: pytest.Item) -> Path:
    return _smoke_outputs_dir(item.config) / item.name


@pytest.fixture
def smoke_output_dir(request: pytest.FixtureRequest) -> Path:
    """This test's directory under ``.test_output/smoke/``, for screenshots and other artifacts."""
    output_dir = _output_dir_for(request.node)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


@pytest.fixture
def smoke_steps_path(smoke_output_dir: Path) -> Path:
    """This test's steps file (``steps.py``), cleared of the steps an earlier run recorded there."""
    steps_path = smoke_output_dir / STEPS_FILE_NAME
    steps_path.unlink(missing_ok=True)
    return steps_path


@pytest.fixture
def studio_app(
    request: pytest.FixtureRequest,
    studio_binary: StudioBinary,
    backend: SmokeBackend,
    smoke_display: str | None,
    tmp_path: Path,
) -> Iterator[StudioAppSession]:
    """The build under test, launched on a throwaway data home with Playwright attached; one per test."""
    with launched_studio_app(
        studio_binary, backend, tmp_path / "studio-data-home", find_free_port(), smoke_display
    ) as session:
        request.node.stash[SMOKE_APP_STATE_DIR_KEY] = session.state_dir
        yield session


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    item.stash[_IS_BODY_RUNNING_KEY] = True
    try:
        return (yield)
    finally:
        item.stash[_IS_BODY_RUNNING_KEY] = False


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Keep a failed test's evidence: a screenshot of every page of the launched app, and the app's own logs.

    The app's data home is pytest's per-test temporary directory, which pytest
    prunes after a few runs, so its ``minds.log``, ``electron.log`` and
    ``minds-events.jsonl`` are copied beside the screenshots while they exist.

    The failure is also counted for the session's verdict here, where the
    report is still the test's own: a test whose only failures are failed
    subtests passes at this point, and pytest fails it only later, when it
    reports it. A subtest's report is neither: its failure is counted by
    label at the end of the session.
    """
    report = yield
    if item.stash.get(_IS_BODY_RUNNING_KEY, False) or not report.failed:
        return report
    item.config.stash.setdefault(_OWN_FAILURES_KEY, []).append(f"{report.nodeid} ({report.when})")
    if report.when == "call" and isinstance(item, pytest.Function):
        session = item.funcargs.get(_STUDIO_APP_FIXTURE_NAME)
        if isinstance(session, StudioAppSession):
            output_dir = _output_dir_for(item)
            screenshot_every_page(session.browser, output_dir, "failure")
            _copy_app_logs(session.log_dir, output_dir / _APP_LOGS_DIR_NAME)
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write ``.test_output/smoke/verdict.json`` and, beside it, the run's report, ``report.html``.

    The verdict says how many failures were default-workspace-template tests:
    a failed template test fails the same way on a rerun, so the workflow
    skips its rerun when those are the session's only failures. The report is
    the run one level below its verdict, for a person deciding what happened.
    """
    terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is None:
        return
    failed_subtest_labels = tuple(
        report.context.msg
        for report in terminal_reporter.stats.get("failed", [])
        if isinstance(report, pytest.SubtestReport)
    )
    verdict = smoke_run_verdict(len(session.config.stash.get(_OWN_FAILURES_KEY, [])), failed_subtest_labels)
    outputs_dir = _smoke_outputs_dir(session.config)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / _VERDICT_FILE_NAME).write_text(verdict.model_dump_json())
    # A session that only collected ran nothing, and must not replace the report of the last run that did.
    if session.config.getoption("collectonly"):
        return
    report = smoke_run_report_of_session(
        session,
        terminal_reporter=terminal_reporter,
        exit_status=int(exitstatus),
        outputs_dir=outputs_dir,
        verdict=verdict,
        environ=os.environ,
        finished_at=datetime.now(timezone.utc),
    )
    write_smoke_run_report(outputs_dir / SMOKE_REPORT_FILE_NAME, report)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name the run's report last, where a person reading the session's output looks for what to open."""
    report_path = _smoke_outputs_dir(terminalreporter.config) / SMOKE_REPORT_FILE_NAME
    if report_path.is_file():
        terminalreporter.write_sep("-", f"smoke run report: {report_path.as_uri()}")


def _copy_app_logs(log_dir: Path, destination: Path) -> None:
    if not log_dir.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for log_file in sorted(path for path in log_dir.iterdir() if path.is_file()):
        shutil.copy2(log_file, destination / log_file.name)
