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
from imbue.minds.smoke_tests.handoff import read_handoff_state_file_or_none
from imbue.minds.smoke_tests.handoff import resolve_test_job_handoff_or_none
from imbue.minds.smoke_tests.manifest import default_manifest_path
from imbue.minds.smoke_tests.manifest import resolve_manifest_path_or_none
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_HANDOFF_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RUN_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId

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


@pytest.fixture(scope="session")
def backend() -> SmokeBackend:
    """The deployed environment under test, named by ``MINDS_SMOKE_TIER``."""
    tier = os.environ.get(SMOKE_TIER_ENV_VAR, "")
    if not tier:
        pytest.skip(f"{SMOKE_TIER_ENV_VAR} is not set; run the smoke suite with `just minds-smoke <tier>`.")
    env_name = DevEnvName(tier)
    return resolve_smoke_backend(env_name, resolve_smoke_client_config_path(env_name))


@pytest.fixture(scope="session")
def studio_binary(backend: SmokeBackend) -> StudioBinary:
    """The Imbue Studio build under test, resolved once per run (a channel's build is downloaded here)."""
    if not is_studio_binary_selected(os.environ):
        pytest.skip(
            f"No Imbue Studio build is named; set one of {', '.join(BINARY_SELECTION_ENV_VARS)} "
            "(MINDS_SMOKE_DEV_CHECKOUT=1 runs this checkout's Electron)."
        )
    return resolve_studio_binary(backend, os.environ)


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
def smoke_run_id() -> SmokeRunId:
    """This run's id: the environment's, else the handoff state file's, else one minted now.

    Names the run's workspaces and its manifest, so a local run without a
    handoff still gets a manifest of its own.
    """
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
    if from_env is not None:
        return from_env
    return default_manifest_path(_smoke_outputs_dir(request.config), smoke_run_id)


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
def studio_app(
    studio_binary: StudioBinary, backend: SmokeBackend, smoke_display: str | None, tmp_path: Path
) -> Iterator[StudioAppSession]:
    """The build under test, launched on a throwaway data home with Playwright attached; one per test."""
    with launched_studio_app(
        studio_binary, backend, tmp_path / "studio-data-home", find_free_port(), smoke_display
    ) as session:
        yield session


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Keep a failed test's evidence: a screenshot of every page of the launched app, and the app's own logs.

    The app's data home is pytest's per-test temporary directory, which pytest
    prunes after a few runs, so its ``minds.log``, ``electron.log`` and
    ``minds-events.jsonl`` are copied beside the screenshots while they exist.
    """
    report = yield
    if report.when == "call" and report.failed and isinstance(item, pytest.Function):
        session = item.funcargs.get(_STUDIO_APP_FIXTURE_NAME)
        if isinstance(session, StudioAppSession):
            output_dir = _output_dir_for(item)
            screenshot_every_page(session.browser, output_dir, "failure")
            _copy_app_logs(session.log_dir, output_dir / _APP_LOGS_DIR_NAME)
    return report


def _copy_app_logs(log_dir: Path, destination: Path) -> None:
    if not log_dir.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for log_file in sorted(path for path in log_dir.iterdir() if path.is_file()):
        shutil.copy2(log_file, destination / log_file.name)
