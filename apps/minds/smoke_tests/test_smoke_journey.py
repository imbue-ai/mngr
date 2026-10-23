"""The smoke journey: the build under test, as a user, against the deployed tier.

Launch the app and reach its landing page; sign in to Imbue Cloud through the
app's own web-login flow, completing the hosted page in the suite's browser;
create an Imbue Cloud workspace; have its agent build and register a
hello-world web app; share the workspace from the Share tab and open the app's
route through the share as the owner; turn sharing off; destroy the workspace.
Every resource created on the way is recorded in the run manifest, so the
teardown job releases it however this test ends.
"""

import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from loguru import logger
from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from imbue.minds.smoke_tests.accounts import leased_host_db_id_for_host_id
from imbue.minds.smoke_tests.accounts import list_leased_hosts
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.accounts import sign_in
from imbue.minds.smoke_tests.app_flows import adopt_imbue_credentials
from imbue.minds.smoke_tests.app_flows import agent_id_of_workspace_frame
from imbue.minds.smoke_tests.app_flows import create_imbue_cloud_workspace
from imbue.minds.smoke_tests.app_flows import destroy_workspace_from_settings
from imbue.minds.smoke_tests.app_flows import is_workspace_gone
from imbue.minds.smoke_tests.app_flows import list_workspaces
from imbue.minds.smoke_tests.app_flows import mint_workspace_llm_credentials
from imbue.minds.smoke_tests.app_flows import open_new_chat
from imbue.minds.smoke_tests.app_flows import open_welcome_chat
from imbue.minds.smoke_tests.app_flows import publish_share_from_panel
from imbue.minds.smoke_tests.app_flows import read_sharing_status
from imbue.minds.smoke_tests.app_flows import screenshot_every_page
from imbue.minds.smoke_tests.app_flows import send_prompt
from imbue.minds.smoke_tests.app_flows import start_web_login
from imbue.minds.smoke_tests.app_flows import unpublish_share_from_panel
from imbue.minds.smoke_tests.app_flows import wait_for_account_in_create_form
from imbue.minds.smoke_tests.app_flows import wait_for_destroy_done
from imbue.minds.smoke_tests.app_flows import wait_for_landing_page
from imbue.minds.smoke_tests.app_flows import wait_for_reply
from imbue.minds.smoke_tests.app_flows import wait_for_share_url
from imbue.minds.smoke_tests.browser import complete_hosted_login
from imbue.minds.smoke_tests.browser import is_hosted_login_page
from imbue.minds.smoke_tests.browser import visitor_browser_context
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.data_types import SmokeResourceKind
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.manifest import record_created_resource
from imbue.minds.smoke_tests.primitives import SMOKE_EMAIL_PREFIX
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.smoke]

# The whole journey: a pool lease, an agent building a server, a share
# certificate, and a destroy.
_JOURNEY_TIMEOUT_SECONDS: Final[int] = 60 * 60
_HOSTED_LOGIN_REDIRECT_TIMEOUT_MS: Final[float] = 90_000.0
_ACCOUNT_LISTED_TIMEOUT_SECONDS: Final[float] = 120.0
_AGENT_REPLY_TIMEOUT_SECONDS: Final[float] = 12 * 60
# A fresh share needs a certificate before its link answers.
_SHARE_LINK_TIMEOUT_SECONDS: Final[float] = 300.0
_SHARE_VISIT_TIMEOUT_SECONDS: Final[float] = 300.0
_SHARE_VISIT_POLL_SECONDS: Final[float] = 5.0
_SHARE_PAGE_TIMEOUT_MS: Final[float] = 60_000.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 600.0

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


def _leased_pool_row_id(backend: SmokeBackend, account: SmokeAccountCredentials, host_id: str) -> str:
    """The pool row the workspace leased, read as the account itself: what the release routes take."""
    bearer = sign_in(connector_url=backend.connector_url, credentials=account)
    hosts = list_leased_hosts(connector_url=backend.connector_url, bearer=bearer)
    host_db_id = leased_host_db_id_for_host_id(hosts, host_id)
    assert host_db_id is not None, f"The connector lists no lease of {account.email} for host {host_id}: {hosts}"
    return host_db_id


def _workspace_row(studio_app: StudioAppSession, agent_id: str) -> Mapping[str, Any]:
    rows = [row for row in list_workspaces(studio_app) if row.get("agent_id") == agent_id]
    assert len(rows) == 1, f"Expected one listing row for {agent_id}, got {rows}"
    return rows[0]


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


@pytest.mark.timeout(_JOURNEY_TIMEOUT_SECONDS)
def test_the_build_signs_in_creates_builds_shares_and_destroys_a_workspace(
    backend: SmokeBackend,
    studio_app: StudioAppSession,
    persistent_account: SmokeAccountCredentials,
    run_manifest: Path,
    smoke_run_id: SmokeRunId,
    smoke_output_dir: Path,
) -> None:
    """The first-shape journey, end to end, through the app's own UI."""
    owner_email = str(persistent_account.email)
    nonce = get_short_random_string()
    workspace_name = _workspace_name(smoke_run_id)

    landed_on = wait_for_landing_page(studio_app)
    logger.info("Step 1: the app launched and landed on {}", landed_on)
    screenshot_every_page(studio_app.browser, smoke_output_dir, "01-landing")

    with visitor_browser_context(studio_app.browser.browser_type) as visitor:
        _sign_in_through_the_app(studio_app, visitor, backend, persistent_account, landed_on, smoke_output_dir)
        logger.info("Step 2: signed in as {}", owner_email)
        screenshot_every_page(studio_app.browser, smoke_output_dir, "03-signed-in")

        workspace_frame = create_imbue_cloud_workspace(studio_app, workspace_name, owner_email)
        agent_id = agent_id_of_workspace_frame(studio_app, workspace_frame)
        row = _workspace_row(studio_app, agent_id)
        assert row.get("account_email") == owner_email, row
        assert row.get("host_id"), row
        host_db_id = _leased_pool_row_id(backend, persistent_account, str(row["host_id"]))
        record_created_resource(
            run_manifest, kind=SmokeResourceKind.WORKSPACE, identifier=host_db_id, owner_email=owner_email
        )
        logger.info("Step 3: created workspace {} ({}, pool row {})", workspace_name, agent_id, host_db_id)
        screenshot_every_page(studio_app.browser, smoke_output_dir, "04-workspace-ready")

        adopt_imbue_credentials(
            open_welcome_chat(workspace_frame), mint_workspace_llm_credentials(studio_app, agent_id)
        )
        chat = open_new_chat(workspace_frame)
        send_prompt(chat, _HELLO_PROMPT_TEMPLATE.format(port=_HELLO_PORT, nonce=nonce, service=_HELLO_SERVICE))
        reply_seconds = wait_for_reply(chat, _READY_REPLY_TEMPLATE.format(nonce=nonce), _AGENT_REPLY_TIMEOUT_SECONDS)
        logger.info("Step 4: the agent reported READY after {:.0f}s", reply_seconds)
        screenshot_every_page(studio_app.browser, smoke_output_dir, "05-agent-ready")

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

        unpublish_share_from_panel(studio_app, agent_id)
        assert read_sharing_status(studio_app, agent_id).get("enabled") is False
        logger.info("Step 6: sharing is off")
        screenshot_every_page(studio_app.browser, smoke_output_dir, "07-unshared")

    destroy_workspace_from_settings(studio_app, agent_id)
    wait_for_destroy_done(studio_app, agent_id, _DESTROY_TIMEOUT_SECONDS)
    assert is_workspace_gone(list_workspaces(studio_app), agent_id)
    logger.info("Step 7: workspace {} is destroyed", agent_id)
    screenshot_every_page(studio_app.browser, smoke_output_dir, "08-destroyed")
