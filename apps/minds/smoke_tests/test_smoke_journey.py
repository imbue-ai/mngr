"""The smoke journey: the build under test, as a user, against the deployed tier.

Launch the app and reach its landing page; sign in to Imbue Cloud through the
app's own web-login flow, completing the hosted page in the suite's browser;
create an Imbue Cloud workspace; run default-workspace-template's CI test
suites inside it; have its agent build and register a hello-world web app;
stop the workspace from the home page, start it again, and have the agent
bring the app back; share the workspace from the Share tab and open the app's
route through the share as the owner; turn sharing off; destroy the workspace.
Every resource created on the way is recorded in the run manifest, so the
teardown job releases it however this test ends.

The template's CI runs its suites in Modal sandboxes built from the workspace
image; a real workspace runs them in a gVisor container on a pool host, which
differs in ways CI cannot see. Every template test is reported as a subtest of
the journey, so each one that fails in the workspace is its own failed result.
"""

import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import pytest
from loguru import logger
from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page

from imbue.minds.desktop_client.e2e_workspace_runner import enter_workspace_from_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import start_workspace_from_home_page
from imbue.minds.desktop_client.workspace_lifecycle import HOST_START_TIMEOUT_SECONDS
from imbue.minds.desktop_client.workspace_lifecycle import HOST_STOP_TIMEOUT_SECONDS
from imbue.minds.smoke_tests.accounts import is_workspace_held_only_in_storage
from imbue.minds.smoke_tests.accounts import is_workspace_running
from imbue.minds.smoke_tests.accounts import is_workspace_stopped
from imbue.minds.smoke_tests.accounts import leased_host_db_id_for_host_id
from imbue.minds.smoke_tests.accounts import leased_template_ref_for_host_id
from imbue.minds.smoke_tests.accounts import list_leased_hosts
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.accounts import sign_in
from imbue.minds.smoke_tests.accounts import wait_for_workspace
from imbue.minds.smoke_tests.app_flows import adopt_imbue_credentials
from imbue.minds.smoke_tests.app_flows import agent_id_of_workspace_frame
from imbue.minds.smoke_tests.app_flows import create_imbue_cloud_workspace
from imbue.minds.smoke_tests.app_flows import destroy_workspace_from_settings
from imbue.minds.smoke_tests.app_flows import is_workspace_gone
from imbue.minds.smoke_tests.app_flows import list_workspaces
from imbue.minds.smoke_tests.app_flows import mint_workspace_llm_credentials
from imbue.minds.smoke_tests.app_flows import open_welcome_chat
from imbue.minds.smoke_tests.app_flows import publish_share_from_panel
from imbue.minds.smoke_tests.app_flows import read_sharing_status
from imbue.minds.smoke_tests.app_flows import screenshot_every_page
from imbue.minds.smoke_tests.app_flows import send_prompt_to_known_main_chat
from imbue.minds.smoke_tests.app_flows import send_prompt_to_main_chat
from imbue.minds.smoke_tests.app_flows import start_web_login
from imbue.minds.smoke_tests.app_flows import stop_workspace_from_account_home
from imbue.minds.smoke_tests.app_flows import unpublish_share_from_panel
from imbue.minds.smoke_tests.app_flows import wait_for_account_in_create_form
from imbue.minds.smoke_tests.app_flows import wait_for_destroy_done
from imbue.minds.smoke_tests.app_flows import wait_for_landing_page
from imbue.minds.smoke_tests.app_flows import wait_for_reply
from imbue.minds.smoke_tests.app_flows import wait_for_share_url
from imbue.minds.smoke_tests.browser import complete_hosted_login
from imbue.minds.smoke_tests.browser import is_hosted_login_page
from imbue.minds.smoke_tests.browser import visitor_browser_context
from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import SmokeResourceKind
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.dwt_suites import DWT_WORKSPACE_REPO_ROOT
from imbue.minds.smoke_tests.dwt_suites import DWT_WORKSPACE_RESULTS_ROOT
from imbue.minds.smoke_tests.dwt_suites import dwt_attempts_phrase
from imbue.minds.smoke_tests.dwt_suites import dwt_results_markdown
from imbue.minds.smoke_tests.dwt_suites import dwt_subtest_label
from imbue.minds.smoke_tests.dwt_suites import run_dwt_ci_suites_in_workspace
from imbue.minds.smoke_tests.manifest import record_created_resource
from imbue.minds.smoke_tests.primitives import SMOKE_EMAIL_PREFIX
from imbue.minds.smoke_tests.primitives import SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_SKIP_DWT_TESTS_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.steps import record_skipped_step
from imbue.minds.smoke_tests.steps import recorded_step
from imbue.minds.smoke_tests.workspace_exec import AppMngrWorkspaceShell
from imbue.minds.smoke_tests.workspace_exec import app_mngr_workspace_shell
from imbue.minds.smoke_tests.workspace_exec import workspace_exec_address
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.smoke]

# default-workspace-template's CI suites run one after another in the workspace, where CI spreads them over
# many sandboxes; the deadline covers waiting for the workspace's first-boot installs too.
_DWT_SUITES_DEADLINE_SECONDS: Final[float] = 75 * 60
_DWT_ENV_CONVERGE_WAIT_SECONDS: Final[int] = 20 * 60
_DWT_POLL_INTERVAL_SECONDS: Final[float] = 30.0
# Reading the configs, starting and stopping the runner, and fetching its results, around the run itself.
_DWT_STEP_OVERHEAD_SECONDS: Final[int] = 10 * 60
# The journey's steps, as its run report's step table names them.
_LAUNCH_STEP_LABEL: Final[str] = "Step 1: launch the app and reach its landing page"
_SIGN_IN_STEP_LABEL: Final[str] = "Step 2: sign in to Imbue Cloud through the app's web login"
_CREATE_STEP_LABEL: Final[str] = "Step 3: create an Imbue Cloud workspace"
_DWT_STEP_LABEL: Final[str] = "Step 3b: run default-workspace-template's CI suites in the workspace"
_BUILD_STEP_LABEL: Final[str] = "Step 4: have the workspace's agent build and register the hello app"
_STOP_START_STEP_LABEL: Final[str] = "Step 4b: stop and start the workspace from the home page"
_SHARE_STEP_LABEL: Final[str] = "Step 5: share the workspace and open the app through the share"
_UNSHARE_STEP_LABEL: Final[str] = "Step 6: turn sharing off"
_DESTROY_STEP_LABEL: Final[str] = "Step 7: destroy the workspace"
_DWT_RESULTS_DIR_NAME: Final[str] = "dwt"
# Beside the results; the smoke workflow adds it to its job's summary page.
_DWT_SUMMARY_FILE_NAME: Final[str] = "summary.md"
# An hour for every step but the template's CI suites, whose own deadline and overhead come on top.
_JOURNEY_TIMEOUT_SECONDS: Final[int] = 60 * 60 + int(_DWT_SUITES_DEADLINE_SECONDS) + _DWT_STEP_OVERHEAD_SECONDS
_HOSTED_LOGIN_REDIRECT_TIMEOUT_MS: Final[float] = 90_000.0
_ACCOUNT_LISTED_TIMEOUT_SECONDS: Final[float] = 120.0
_AGENT_REPLY_TIMEOUT_SECONDS: Final[float] = 12 * 60
# A fresh share needs a certificate before its link answers.
_SHARE_LINK_TIMEOUT_SECONDS: Final[float] = 300.0
_SHARE_VISIT_TIMEOUT_SECONDS: Final[float] = 300.0
_SHARE_VISIT_POLL_SECONDS: Final[float] = 5.0
_SHARE_PAGE_TIMEOUT_MS: Final[float] = 60_000.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 600.0
# Each press gets a minute past the app's own budget for the request it makes, so a request the app gives up
# on is reported in the app's words rather than as a wait that ran out here.
_STOPPED_BADGE_TIMEOUT_SECONDS: Final[float] = HOST_STOP_TIMEOUT_SECONDS + 60.0
_STARTED_ROW_TIMEOUT_SECONDS: Final[float] = HOST_START_TIMEOUT_SECONDS + 60.0
# The connector reports a machine stopped once its disks are uploaded and verified, which takes minutes;
# the rest is room for a server under load.
_STOP_UPLOAD_TIMEOUT_SECONDS: Final[float] = 20 * 60
# The app has just been told the machine runs, so the connector only has to agree.
_RUNNING_ON_CONNECTOR_TIMEOUT_SECONDS: Final[float] = 60.0
# The app reaches a started machine again, moving its pinned host keys first when a restore changed the
# machine's ports, and the workspace's services come back before its desktop renders.
_REENTER_TIMEOUT_SECONDS: Final[float] = 300.0
_HELLO_FILE_READ_TIMEOUT_SECONDS: Final[float] = 60.0
_RUNNING_HOST_STATE: Final[str] = "RUNNING"

# What the agent is asked to build: the cheapest server that proves the route
# answers through the share, registered as a workspace app under this name.
_HELLO_SERVICE: Final[str] = "hello"
_HELLO_PORT: Final[int] = 8765
_HELLO_PROMPT_TEMPLATE: Final[str] = (
    "Write hello.py: a Python stdlib http.server on port {port} that answers GET / with "
    '200 and the text "hello {nonce}". Start it in the background, register it with '
    "`python3 system/scripts/forward_port.py --url http://localhost:{port} --name {service} --no-icon`, "
    "curl it once, then reply with only: READY {nonce}"
)
_READY_REPLY_TEMPLATE: Final[str] = "READY {nonce}"
_HELLO_BODY_TEMPLATE: Final[str] = "hello {nonce}"
# The agent writes the file where it runs, which is the workspace's one work directory.
_READ_HELLO_FILE_COMMAND: Final[str] = "cat hello.py"
# A stop ends every process on the machine and keeps its disk, so the file survives and the server does not:
# the agent is asked to start the same file again, which the share step then serves.
_HELLO_RESTART_PROMPT_TEMPLATE: Final[str] = (
    "This machine was stopped and started again, so the server from hello.py is no longer running. Without "
    "changing hello.py, start it in the background again, curl http://localhost:{port}/ once, then reply "
    "with only: BACK {nonce}"
)
_BACK_REPLY_TEMPLATE: Final[str] = "BACK {nonce}"
# The web-login flow's loopback listener, where the hosted page lands once the sign-in is handed off.
_LOGIN_CALLBACK_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://127\.0\.0\.1:\d+/callback")
_SHARE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^https://[a-z0-9-]+\.[a-z0-9.-]+/$")


def _workspace_name(run_id: SmokeRunId) -> str:
    """``studio-smoke-<run id>-<random>``: the prefix teardown sweeps by, the id this run is known as, and a
    suffix so a rerun of the same run id never collides with a leftover."""
    return f"{SMOKE_EMAIL_PREFIX}{run_id}-{get_short_random_string()}"


def _sign_in_through_the_app(
    studio_app: StudioAppSession,
    visitor: BrowserContext,
    backend: SmokeBackend,
    account: SmokeAccountCredentials,
    landed_on: str,
    output_dir: Path,
) -> None:
    """Start the app's web-login flow and complete the hosted page it hands out in the suite's browser."""
    login_url = start_web_login(studio_app, landed_on)
    assert login_url.startswith(origin_of(backend.accounts_origin)), (
        f"The app's sign-in page {login_url} is not on {backend.env_name}'s accounts origin {backend.accounts_origin}"
    )
    screenshot_every_page(studio_app.browser, output_dir, "02-web-login-modal")
    login_page = visitor.new_page()
    login_page.goto(login_url, wait_until="domcontentloaded")
    complete_hosted_login(login_page, account)
    # The handoff lands on the app's loopback listener; whether it arrived is
    # what the app's own account list says below, so a redirect that did not
    # settle here (a reset while the listener answered) is only noted.
    try:
        login_page.wait_for_url(_LOGIN_CALLBACK_URL_PATTERN, timeout=_HOSTED_LOGIN_REDIRECT_TIMEOUT_MS)
        logger.info("The hosted login handed the sign-in back to the app at {}", login_page.url)
    except PlaywrightError as exc:
        logger.warning("The hosted login's redirect back to the app did not settle: {}", exc)
    login_page.close()
    wait_for_account_in_create_form(studio_app, str(account.email), _ACCOUNT_LISTED_TIMEOUT_SECONDS)


def _leased_pool_row(backend: SmokeBackend, account: SmokeAccountCredentials, host_id: str) -> tuple[str, str | None]:
    """The pool row the workspace leased, read as the account itself: its id (what the release routes take)
    and the template tag it was baked at (None when the row declares none)."""
    bearer = sign_in(connector_url=backend.connector_url, credentials=account)
    hosts = list_leased_hosts(connector_url=backend.connector_url, bearer=bearer)
    host_db_id = leased_host_db_id_for_host_id(hosts, host_id)
    assert host_db_id is not None, f"The connector lists no lease of {account.email} for host {host_id}: {hosts}"
    return host_db_id, leased_template_ref_for_host_id(hosts, host_id)


def _assert_workspace_is_at_expected_template(
    expected_template_ref: str, leased_template_ref: str | None, host_db_id: str
) -> None:
    """Fail unless the workspace's row was baked at the template the run is meant to exercise.

    The row's tag is the template the suites run against only on the fast
    path, so a run asked to certify a release's template must not pass on a
    row baked at another tag, however well the rebuilt workspace works.
    """
    assert leased_template_ref == expected_template_ref, (
        f"Pool row {host_db_id} was baked at template {leased_template_ref!r}, not at "
        f"{expected_template_ref!r} ({SMOKE_EXPECTED_TEMPLATE_REF_ENV_VAR}): either the pool had no available "
        f"row at that tag in the form's region and the create rebuilt another row's container (the slow path), "
        f"or the build under test asks for a different tag than this run expects."
    )


def _workspace_row(studio_app: StudioAppSession, agent_id: str) -> Mapping[str, Any]:
    rows = [row for row in list_workspaces(studio_app) if row.get("agent_id") == agent_id]
    assert len(rows) == 1, f"Expected one listing row for {agent_id}, got {rows}"
    return rows[0]


def _workspace_shell(studio_app: StudioAppSession, workspace_row: Mapping[str, Any]) -> AppMngrWorkspaceShell:
    return app_mngr_workspace_shell(
        studio_app,
        workspace_exec_address(
            agent_id=str(workspace_row["agent_id"]),
            host_id=workspace_row.get("host_id"),
            provider_name=workspace_row.get("provider_name"),
        ),
    )


def _assert_hello_file_holds_nonce(
    studio_app: StudioAppSession, workspace_row: Mapping[str, Any], nonce: str, moment: str
) -> None:
    """Fail unless the file the agent wrote still carries the run's nonce, read through the app's own mngr."""
    content = _workspace_shell(studio_app, workspace_row).run(
        _READ_HELLO_FILE_COMMAND, DWT_WORKSPACE_REPO_ROOT, _HELLO_FILE_READ_TIMEOUT_SECONDS
    )
    assert nonce in content, f"hello.py does not hold the nonce {nonce} {moment}: {content[:300]!r}"


def _stop_and_start_from_the_home_page(
    studio_app: StudioAppSession,
    backend: SmokeBackend,
    account: SmokeAccountCredentials,
    agent_id: str,
    host_db_id: str,
    # How long to wait for the stopped machine to leave its server before starting it; None starts it at once.
    storage_restore_wait_seconds: float | None,
    output_dir: Path,
) -> Frame:
    """Stop the workspace from the home page, start it again, and re-enter it; return its frame, desktop rendered.

    Between the two presses the connector's own status is what says the stop
    is finished, which is when the machine's disks are uploaded and verified.
    A start that follows at once restarts the machine in place. Given a wait,
    the journey first has the machine leave its server, after which a start
    can only restore it from that upload.
    """
    stop_workspace_from_account_home(studio_app, str(account.email), agent_id, _STOPPED_BADGE_TIMEOUT_SECONDS)
    stopped = wait_for_workspace(
        connector_url=backend.connector_url,
        credentials=account,
        host_db_id=host_db_id,
        is_reached=is_workspace_stopped,
        description="stopped, with its disks uploaded",
        timeout_seconds=_STOP_UPLOAD_TIMEOUT_SECONDS,
    )
    logger.info("The connector reports workspace {} stopped at {}", agent_id, stopped.get("stopped_at"))
    screenshot_every_page(studio_app.browser, output_dir, "05b-stopped")
    if storage_restore_wait_seconds is not None:
        wait_for_workspace(
            connector_url=backend.connector_url,
            credentials=account,
            host_db_id=host_db_id,
            is_reached=is_workspace_held_only_in_storage,
            description="off its server and held only in storage",
            timeout_seconds=storage_restore_wait_seconds,
        )
        logger.info("Workspace {} has left its server; the start restores it from storage", agent_id)
    start_workspace_from_home_page(studio_app.page, agent_id, _STARTED_ROW_TIMEOUT_SECONDS)
    started = wait_for_workspace(
        connector_url=backend.connector_url,
        credentials=account,
        host_db_id=host_db_id,
        is_reached=is_workspace_running,
        description="running",
        timeout_seconds=_RUNNING_ON_CONNECTOR_TIMEOUT_SECONDS,
    )
    logger.info(
        "The connector reports workspace {} running at {}:{} (it stopped at {}:{})",
        agent_id,
        started.get("vps_address"),
        started.get("ssh_port"),
        stopped.get("vps_address"),
        stopped.get("ssh_port"),
    )
    listed_host_state = _workspace_row(studio_app, agent_id).get("host_state")
    assert listed_host_state == _RUNNING_HOST_STATE, (
        f"The app lists the started workspace {agent_id} as {listed_host_state!r}"
    )
    return enter_workspace_from_home_page(studio_app.page, agent_id, _REENTER_TIMEOUT_SECONDS)


def _open_share_as_owner(visitor: BrowserContext, share_url: str, account: SmokeAccountCredentials, body: str) -> None:
    """Open the app's share link in the owner's browser until it answers 200 with ``body``.

    The share's certificate and tunnel can lag the link; the broker sends a
    browser without a session to the hosted login first, which the owner's
    browser may or may not still hold.
    """
    page: Page = visitor.new_page()
    deadline = time.monotonic() + _SHARE_VISIT_TIMEOUT_SECONDS
    last_outcome = "not attempted"
    while time.monotonic() < deadline:
        try:
            response = page.goto(share_url, wait_until="domcontentloaded", timeout=_SHARE_PAGE_TIMEOUT_MS)
        except PlaywrightError as exc:
            last_outcome = f"navigation failed: {exc}"
            logger.info("The share link {} is not answering yet ({}); retrying", share_url, exc)
            page.wait_for_timeout(_SHARE_VISIT_POLL_SECONDS * 1000)
            continue
        if is_hosted_login_page(page):
            complete_hosted_login(page, account)
            page.wait_for_load_state("domcontentloaded")
            response = None
        status = response.status if response is not None else None
        content = page.content()
        if status == 200 and body in content:
            logger.info("The share link {} answered 200 with {!r}", share_url, body)
            page.close()
            return
        last_outcome = f"status {status}, url {page.url}, body excerpt {content[:300]!r}"
        page.wait_for_timeout(_SHARE_VISIT_POLL_SECONDS * 1000)
    raise AssertionError(
        f"The share link {share_url} did not answer 200 with {body!r} within {_SHARE_VISIT_TIMEOUT_SECONDS:.0f}s; "
        f"last outcome: {last_outcome}"
    )


def _assert_dwt_result_passed(result: DwtTestResult) -> None:
    """Pass, skip, or fail the way the template test did in the workspace, with its own failure text."""
    match result.outcome:
        case DwtTestOutcome.PASSED:
            pass
        case DwtTestOutcome.SKIPPED:
            pytest.skip(result.detail or "skipped in the workspace")
        case DwtTestOutcome.FAILED:
            attempts = dwt_attempts_phrase(result.attempt_count)
            pytest.fail(f"Failed in the workspace on {attempts}:\n{result.detail}", pytrace=False)
        case _ as unreachable:
            assert_never(unreachable)


def _run_dwt_ci_suites_reporting_each_test(
    subtests: pytest.Subtests,
    studio_app: StudioAppSession,
    workspace_row: Mapping[str, Any],
    run_id: SmokeRunId,
    output_dir: Path,
    template_ref: str | None,
) -> None:
    """Run the template's CI suites in the fresh workspace and report each of their tests as a subtest.

    A failing template test fails its own subtest and the journey goes on: the
    rest of the journey does not depend on it. The step's own failure -- the
    suites could not be read, started or fetched -- is the step's subtest.
    """
    with subtests.test(msg=_DWT_STEP_LABEL):
        results = run_dwt_ci_suites_in_workspace(
            workspace=_workspace_shell(studio_app, workspace_row),
            repo_root=DWT_WORKSPACE_REPO_ROOT,
            workspace_results_dir=DWT_WORKSPACE_RESULTS_ROOT / str(run_id),
            local_results_dir=output_dir,
            env_converge_wait_seconds=_DWT_ENV_CONVERGE_WAIT_SECONDS,
            deadline_seconds=_DWT_SUITES_DEADLINE_SECONDS,
            poll_interval_seconds=_DWT_POLL_INTERVAL_SECONDS,
        )
        (output_dir / _DWT_SUMMARY_FILE_NAME).write_text(dwt_results_markdown(results, template_ref=template_ref))
        for result in results:
            with subtests.test(msg=dwt_subtest_label(result)):
                _assert_dwt_result_passed(result)
        failed_count = sum(1 for result in results if result.outcome == DwtTestOutcome.FAILED)
        logger.info(
            "Step 3b: ran {} default-workspace-template test results in the workspace; {} failed (results in {})",
            len(results),
            failed_count,
            output_dir,
        )


@pytest.mark.timeout(_JOURNEY_TIMEOUT_SECONDS)
def test_the_build_signs_in_creates_builds_stops_starts_shares_and_destroys_a_workspace(
    is_dwt_tests_skipped: bool,
    storage_restore_wait_seconds: float | None,
    expected_template_ref: str | None,
    backend: SmokeBackend,
    studio_app: StudioAppSession,
    persistent_account: SmokeAccountCredentials,
    run_manifest: Path,
    smoke_run_id: SmokeRunId,
    smoke_output_dir: Path,
    smoke_steps_path: Path,
    subtests: pytest.Subtests,
) -> None:
    """The first-shape journey, end to end, through the app's own UI."""
    owner_email = str(persistent_account.email)
    nonce = get_short_random_string()
    workspace_name = _workspace_name(smoke_run_id)

    with recorded_step(smoke_steps_path, _LAUNCH_STEP_LABEL):
        landed_on = wait_for_landing_page(studio_app)
        logger.info("Step 1: the app launched and landed on {}", landed_on)
        screenshot_every_page(studio_app.browser, smoke_output_dir, "01-landing")

    with visitor_browser_context(studio_app.browser.browser_type) as visitor:
        with recorded_step(smoke_steps_path, _SIGN_IN_STEP_LABEL):
            _sign_in_through_the_app(studio_app, visitor, backend, persistent_account, landed_on, smoke_output_dir)
            logger.info("Step 2: signed in as {}", owner_email)
            screenshot_every_page(studio_app.browser, smoke_output_dir, "03-signed-in")

        with recorded_step(smoke_steps_path, _CREATE_STEP_LABEL):
            workspace_frame = create_imbue_cloud_workspace(studio_app, workspace_name, owner_email)
            agent_id = agent_id_of_workspace_frame(studio_app, workspace_frame)
            row = _workspace_row(studio_app, agent_id)
            assert row.get("account_email") == owner_email, row
            assert row.get("host_id"), row
            host_db_id, leased_template_ref = _leased_pool_row(backend, persistent_account, str(row["host_id"]))
            # Recorded before any check of the row, so teardown releases it however the check comes out.
            record_created_resource(
                run_manifest, kind=SmokeResourceKind.WORKSPACE, identifier=host_db_id, owner_email=owner_email
            )
            logger.info(
                "Step 3: created workspace {} ({}, pool row {} baked at template {})",
                workspace_name,
                agent_id,
                host_db_id,
                leased_template_ref,
            )
            screenshot_every_page(studio_app.browser, smoke_output_dir, "04-workspace-ready")
            if expected_template_ref is not None:
                _assert_workspace_is_at_expected_template(expected_template_ref, leased_template_ref, host_db_id)

        if is_dwt_tests_skipped:
            skip_reason = f"{SMOKE_SKIP_DWT_TESTS_ENV_VAR}=1 skips default-workspace-template's CI tests"
            record_skipped_step(smoke_steps_path, _DWT_STEP_LABEL, skip_reason)
            with subtests.test(msg=_DWT_STEP_LABEL):
                pytest.skip(skip_reason)
        else:
            with recorded_step(smoke_steps_path, _DWT_STEP_LABEL):
                _run_dwt_ci_suites_reporting_each_test(
                    subtests,
                    studio_app,
                    row,
                    smoke_run_id,
                    smoke_output_dir / _DWT_RESULTS_DIR_NAME,
                    leased_template_ref,
                )

        with recorded_step(smoke_steps_path, _BUILD_STEP_LABEL):
            welcome_chat = open_welcome_chat(workspace_frame)
            adopt_imbue_credentials(welcome_chat, mint_workspace_llm_credentials(studio_app, agent_id))
            prompt = _HELLO_PROMPT_TEMPLATE.format(port=_HELLO_PORT, nonce=nonce, service=_HELLO_SERVICE)
            main_chat_id = send_prompt_to_main_chat(workspace_frame, welcome_chat, prompt, nonce)
            reply_seconds = wait_for_reply(
                workspace_frame, main_chat_id, _READY_REPLY_TEMPLATE.format(nonce=nonce), _AGENT_REPLY_TIMEOUT_SECONDS
            )
            logger.info("Step 4: the agent reported READY after {:.0f}s", reply_seconds)
            screenshot_every_page(studio_app.browser, smoke_output_dir, "05-agent-ready")

        with recorded_step(smoke_steps_path, _STOP_START_STEP_LABEL):
            _assert_hello_file_holds_nonce(studio_app, row, nonce, "before the stop")
            workspace_frame = _stop_and_start_from_the_home_page(
                studio_app,
                backend,
                persistent_account,
                agent_id,
                host_db_id,
                storage_restore_wait_seconds,
                smoke_output_dir,
            )
            _assert_hello_file_holds_nonce(studio_app, row, nonce, "after the start")
            # The desktop renders before the apps behind it are back, and the launcher hands a typed text to
            # the chat app the moment Enter is pressed, so the chat's page has to be framed again first.
            open_welcome_chat(workspace_frame)
            restart_nonce = get_short_random_string()
            restart_prompt = _HELLO_RESTART_PROMPT_TEMPLATE.format(port=_HELLO_PORT, nonce=restart_nonce)
            send_prompt_to_known_main_chat(workspace_frame, main_chat_id, restart_prompt, restart_nonce)
            restart_reply_seconds = wait_for_reply(
                workspace_frame,
                main_chat_id,
                _BACK_REPLY_TEMPLATE.format(nonce=restart_nonce),
                _AGENT_REPLY_TIMEOUT_SECONDS,
            )
            logger.info(
                "Step 4b: the workspace stopped and started ({}), and the agent reported BACK after {:.0f}s",
                "restored from storage" if storage_restore_wait_seconds is not None else "started at once",
                restart_reply_seconds,
            )
            screenshot_every_page(studio_app.browser, smoke_output_dir, "05c-started-again")

        with recorded_step(smoke_steps_path, _SHARE_STEP_LABEL):
            publish_share_from_panel(studio_app, agent_id)
            sharing = read_sharing_status(studio_app, agent_id)
            assert sharing.get("enabled") is True, sharing
            assert sharing.get("host_id"), sharing
            record_created_resource(
                run_manifest, kind=SmokeResourceKind.SHARE, identifier=str(sharing["host_id"]), owner_email=owner_email
            )
            share_url = wait_for_share_url(studio_app, agent_id, _HELLO_SERVICE, _SHARE_LINK_TIMEOUT_SECONDS)
            assert _SHARE_URL_PATTERN.match(share_url), share_url
            screenshot_every_page(studio_app.browser, smoke_output_dir, "06-shared")
            _open_share_as_owner(visitor, share_url, persistent_account, _HELLO_BODY_TEMPLATE.format(nonce=nonce))
            logger.info("Step 5: the share link {} served the hello app", share_url)

        with recorded_step(smoke_steps_path, _UNSHARE_STEP_LABEL):
            unpublish_share_from_panel(studio_app, agent_id)
            assert read_sharing_status(studio_app, agent_id).get("enabled") is False
            logger.info("Step 6: sharing is off")
            screenshot_every_page(studio_app.browser, smoke_output_dir, "07-unshared")

    with recorded_step(smoke_steps_path, _DESTROY_STEP_LABEL):
        destroy_workspace_from_settings(studio_app, agent_id)
        wait_for_destroy_done(studio_app, agent_id, _DESTROY_TIMEOUT_SECONDS)
        assert is_workspace_gone(list_workspaces(studio_app), agent_id)
        logger.info("Step 7: workspace {} is destroyed", agent_id)
        screenshot_every_page(studio_app.browser, smoke_output_dir, "08-destroyed")
