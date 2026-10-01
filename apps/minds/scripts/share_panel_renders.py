"""Render the share panel in the seven frames of the B2 mock, from the real components.

Each frame is a fixture state of the panel (``blueprint/share-panel/plan-share-panel.md``,
"Frames to model state"). The built SPA bundle is served with the same fixture
bootstrap ``visual_diff.py capture-spa`` uses, the workspace options and sharing
routes are answered from fixture JSON built out of the real wire models, and the
panel is then driven the way a granter would drive it (throw the switch, type an
address, press Add, retry a failed row) until it stands in the frame's state.
The result is screenshotted so the build can be compared by eye with
``attachments/panel-mocks/b21.png`` through ``b27.png``.

A sibling of ``visual_diff.py`` rather than a mode of it: that tool captures one
screenshot per route from one bootstrap, and these frames differ by what the API
answers and by what is then done to the panel.

    uv run apps/minds/scripts/share_panel_renders.py
    uv run apps/minds/scripts/share_panel_renders.py --skip-build --only b24

Captures land in ``blueprint/share-panel/renders/`` as ``b21.png`` ... ``b27.png``.
"""

import json
import shutil
import socket
import threading
from collections.abc import Callable
from collections.abc import Mapping
from enum import auto
from pathlib import Path
from typing import Final

import click
from loguru import logger
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import Field
from visual_diff import REPO_ROOT
from visual_diff import STATIC_DIR
from visual_diff import SpaCaptureServer
from visual_diff import SpaRouteHandler
from visual_diff import build_spa_bundle
from visual_diff import build_spa_fixture_bootstrap
from visual_diff import launch_chromium
from visual_diff import render_spa_index_html

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.api_models import IdentityRecordResponse
from imbue.minds.desktop_client.api_models import MachineSharingResponse
from imbue.minds.desktop_client.api_models import SharingGrantList
from imbue.minds.desktop_client.api_models import SharingGrantsDocument
from imbue.minds.desktop_client.api_models import SharingReadinessResponse
from imbue.minds.desktop_client.share_grant_validation import PUBLIC_EMAIL_DOMAINS
from imbue.minds.desktop_client.ui_api_options import WorkspaceOptionsAccount
from imbue.minds.desktop_client.ui_api_options import WorkspaceOptionsData
from imbue.minds.errors import MindError

# Wide enough for the options card with room around it, tall enough that the
# panel is never the thing that clips a long grant list.
VIEWPORT_W: Final[int] = 1280
VIEWPORT_H: Final[int] = 1000

DEFAULT_OUTPUT_DIR: Final[Path] = REPO_ROOT / "blueprint" / "share-panel" / "renders"

# The workspace the shared fixture bootstrap calls "alpha", which the mock draws.
AGENT_ID: Final[str] = "agent-00000000000000000000000000000001"
HOST_ID: Final[str] = "host-00000000000000000000000000000001"
GRANTER_EMAIL: Final[str] = "alice@example.com"

WHOLE_SERVICE: Final[str] = "system_interface"
# The mock's third nav entry, chat, is an interface service that
# split_share_targets excludes, so no options payload can carry it.
APP_SERVICES: Final[tuple[str, ...]] = ("notes", "kanban")

SHARE_ORIGIN: Final[str] = "https://wp7k2m8b.us1.minds.example/"
# kanban is deliberately absent: an app that has never run registered no
# address, which is what the nav's "no link yet" reports.
LIVE_LABELS: Final[dict[str, str]] = {
    WHOLE_SERVICE: "system_interface-elm7wydc",
    "notes": "notes-x9q2rt",
}
# Mid-provisioning the workspace's own origin has no label yet, so the whole
# workspace's Link section is still waiting while the apps' nav entries are not.
PROVISIONING_LABELS: Final[dict[str, str]] = {"notes": "notes-x9q2rt"}

CERT_NOT_AFTER: Final[str] = "2027-01-01T00:00:00Z"

IDENTITIES: Final[dict[str, IdentityRecordResponse]] = {
    "user-carol": IdentityRecordResponse(user_id="user-carol", email="carol@example.net", display_name="Carol Reyes"),
    "user-frank": IdentityRecordResponse(user_id="user-frank", email="frank@example.net", display_name="Frank Ito"),
    "user-grace": IdentityRecordResponse(user_id="user-grace", email="grace@example.net", display_name="Grace Lin"),
    "user-hugo": IdentityRecordResponse(user_id="user-hugo", email="hugo@example.net", display_name="Hugo Blum"),
    "user-iris": IdentityRecordResponse(user_id="user-iris", email="iris@example.net", display_name="Iris Nakamura"),
}

# The five grants of B2.4 once they have all settled, which B2.5 inherits and
# B2.6 keeps while publishing is off.
SETTLED_FIVE: Final[SharingGrantsDocument] = SharingGrantsDocument(
    workspace=SharingGrantList(
        users=("user-carol",),
        emails=("bob@example.org", "dan@example.org", "erin@example.org"),
        email_domains=("acme.example",),
    ),
    services={"notes": SharingGrantList(users=("user-frank",))},
)

EMPTY_GRANTS: Final[SharingGrantsDocument] = SharingGrantsDocument()

# Two domains and eleven individuals: more rows than the pane has height for.
THIRTEEN_GRANTS: Final[SharingGrantsDocument] = SharingGrantsDocument(
    workspace=SharingGrantList(
        users=("user-carol", "user-frank", "user-grace", "user-hugo", "user-iris"),
        emails=(
            "bob@example.org",
            "dan@example.org",
            "erin@example.org",
            "hana@example.org",
            "ivan@example.org",
            "jo@example.org",
        ),
        email_domains=("acme.example", "partner.example"),
    ),
    services={"notes": SharingGrantList(users=("user-frank",))},
)

# Grant row keys, as SharePanelModel builds them (kind plus normalized value,
# or the account id for a grant already stored under one).
CAROL_ROW: Final[str] = "user:user-carol"
ACME_ROW: Final[str] = "email_domain:acme.example"
BOB_ROW: Final[str] = "email:bob@example.org"
DAN_ROW: Final[str] = "email:dan@example.org"
ERIN_ROW: Final[str] = "email:erin@example.org"

SAVING_TEXT: Final[str] = "Securely granting access"
# A refused write is reported in the workspace's own words, so the words the stub
# refuses with are the ones a failed row will be waited for.
FAILED_TEXT: Final[str] = "The workspace could not be reached"


class GrantsReply(UpperCaseStrEnum):
    """How the stubbed grants route answers one write."""

    # Store what was sent and answer with the status document, as the route does.
    ECHO = auto()
    # Refuse with a 500, which marks every row the write carried "Could not save".
    SERVER_ERROR = auto()
    # Answer nothing, which leaves every row the write carries saying it is granting.
    HANG = auto()


class SharePanelRenderError(MindError):
    """Raised when a frame's fixture state could not be reached through the stubs."""

    ...


class ShareRenderArguments(FrozenModel):
    """Parsed command line arguments for the share panel render capture."""

    output_dir: Path = Field(description="Directory the seven captures are written to")
    copy_dir: Path | None = Field(description="Extra directory each capture is copied into, prefixed 'built-'")
    is_build_skipped: bool = Field(description="Whether to reuse the bundle already in static/ui")
    slugs: tuple[str, ...] = Field(description="Which frame slugs to capture; empty means all seven")


class ShareFrame(FrozenModel):
    """One frame of the mock: what the routes answer, and what is then done to the panel."""

    slug: str = Field(description="The capture's file name, b21 through b27")
    title: str = Field(description="The frame's heading in the mock")
    sharing: MachineSharingResponse = Field(description="What GET /workspace-sharing/<id> answers")
    readiness: SharingReadinessResponse = Field(description="What the readiness poll answers")
    published: MachineSharingResponse | None = Field(
        description="The status document a publish answers with; None for a frame that never publishes"
    )
    grants_replies: tuple[GrantsReply, ...] = Field(
        description="How each grants write in turn is answered; the last entry answers the rest"
    )
    drive: Callable[[Page], None] = Field(description="What is done to the panel before the shot")


def _options_payload(labels: Mapping[str, str]) -> str:
    """The options payload the panel opens with, as ``ui_api_options.py`` serves it."""
    data = WorkspaceOptionsData(
        agent_id=AGENT_ID,
        host_id=HOST_ID,
        name="alpha",
        color="#7c9885",
        palette={"sage": "#7c9885"},
        is_stale=False,
        is_leased_imbue_cloud=False,
        has_account=True,
        account_email=GRANTER_EMAIL,
        account_display_name="Alice Nguyen",
        current_account=WorkspaceOptionsAccount(
            user_id="user-alice", email=GRANTER_EMAIL, display_name="Alice Nguyen"
        ),
        accounts=(WorkspaceOptionsAccount(user_id="user-alice", email=GRANTER_EMAIL, display_name="Alice Nguyen"),),
        app_services=APP_SERVICES,
        service_labels=dict(labels),
        whole_service=WHOLE_SERVICE,
        public_email_domains=tuple(sorted(PUBLIC_EMAIL_DOMAINS)),
        ssh_command="",
    )
    return data.model_dump_json()


def _sharing(
    *,
    is_published: bool,
    grants: SharingGrantsDocument,
    labels: Mapping[str, str],
) -> MachineSharingResponse:
    return MachineSharingResponse(
        host_id=HOST_ID,
        enabled=is_published,
        workspace_domain="wp7k2m8b.us1.minds.example" if is_published else None,
        url=SHARE_ORIGIN if is_published else None,
        service_labels=dict(labels),
        grants=grants,
        identities=dict(IDENTITIES),
    )


class ShareRouteStubs(MutableModel):
    """The panel's routes, answered per frame."""

    frame: ShareFrame = Field(description="The frame being captured")
    grants_write_count: int = Field(default=0, description="How many grants writes have been answered so far")
    is_published: bool = Field(description="Whether the stubbed workspace is published right now")
    labels: dict[str, str] = Field(description="The origin label per share target the stub currently reports")

    def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0]
        if path == f"/ui/api/workspaces/{AGENT_ID}/options":
            _fulfill(route, _options_payload(self.frame.sharing.service_labels))
            return
        if path == "/ui/api/users/resolve":
            self._handle_resolve(route)
            return
        share_base = f"/api/v1/workspace-sharing/{AGENT_ID}"
        if path == f"{share_base}/readiness":
            _fulfill(route, self.frame.readiness.model_dump_json())
            return
        if path == f"{share_base}/grants":
            self._handle_grants_write(route)
            return
        if path == share_base:
            self._handle_share_state(route)
            return
        if route.request.resource_type == "document" and path != f"/workspace/{AGENT_ID}/options":
            # The workspace surface behind the panel iframes an origin that is
            # not here; without this its failure page reads through the backdrop.
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><html><body></body></html>")
            return
        route.continue_()

    def _handle_resolve(self, route: Route) -> None:
        """No account has any address these frames type, which is the 404 the panel keeps as an invite.

        Every account the mock draws is already a stored grant, so it arrives
        with the status document's identities rather than through this lookup.
        """
        body = json.loads(route.request.post_data or "{}")
        email = str(body.get("email", "")).strip()
        _fulfill(route, json.dumps({"error": f"No Imbue account has the verified email {email}"}), status=404)

    def _handle_share_state(self, route: Route) -> None:
        method = route.request.method
        if method == "PUT":
            published = self.frame.published
            if published is None:
                raise SharePanelRenderError(f"{self.frame.slug} published with no publish response in its fixture")
            self.is_published = True
            self.labels = dict(published.service_labels)
            _fulfill(route, published.model_dump_json())
            return
        if method == "DELETE":
            self.is_published = False
            _fulfill(route, self._status_document(self.frame.sharing.grants or EMPTY_GRANTS).model_dump_json())
            return
        _fulfill(route, self.frame.sharing.model_dump_json())

    def _handle_grants_write(self, route: Route) -> None:
        reply = self.frame.grants_replies[min(self.grants_write_count, len(self.frame.grants_replies) - 1)]
        self.grants_write_count += 1
        if reply == GrantsReply.HANG:
            return
        if reply == GrantsReply.SERVER_ERROR:
            _fulfill(route, json.dumps({"error": FAILED_TEXT}), status=500)
            return
        body = json.loads(route.request.post_data or "{}")
        stored = SharingGrantsDocument.model_validate(body["grants"])
        _fulfill(route, self._status_document(stored).model_dump_json())

    def _status_document(self, grants: SharingGrantsDocument) -> MachineSharingResponse:
        return _sharing(is_published=self.is_published, grants=grants, labels=self.labels)


def _fulfill(route: Route, body: str, *, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=body)


def _hover_for_tooltip(page: Page, selector: str) -> None:
    """Hover a trigger and wait out the delegated bubble's hover delay and fade."""
    page.hover(selector)
    page.wait_for_function(
        "() => { const bubble = document.querySelector('div.minds-tooltip');"
        " return bubble !== null && getComputedStyle(bubble).opacity === '1'; }",
        timeout=5000,
    )


def _add_grant(page: Page, value: str) -> None:
    page.fill("#ws-share-add-value", value)
    page.press("#ws-share-add-value", "Enter")


def _wait_for_row(page: Page, key: str, needle: str, *, is_present: bool = True) -> None:
    """Wait until row ``key`` does or does not say ``needle``, and say what it said if it never does.

    The wait is the only thing holding a frame to a state, so a needle that has drifted
    from the view's wording would otherwise pass vacuously in the absent direction and
    time out opaquely in the present one. Reporting the row's own text names which.
    """
    try:
        page.wait_for_function(
            "([key, needle, isPresent]) => {"
            "  const row = document.querySelector('[data-grant-row=\"' + key + '\"]');"
            "  if (!row) return false;"
            "  return ((row.textContent || '').includes(needle)) === isPresent;"
            "}",
            arg=[key, needle, is_present],
            timeout=15000,
        )
    except PlaywrightTimeoutError as exc:
        row = page.query_selector(f'[data-grant-row="{key}"]')
        said = "no such row is drawn" if row is None else f"it says {(row.text_content() or '').strip()!r}"
        wanted = "say" if is_present else "stop saying"
        raise SharePanelRenderError(f"Row {key} did not {wanted} {needle!r}; {said}") from exc


def _drive_first_open(page: Page) -> None:
    page.wait_for_selector("#ws-share-empty")
    _hover_for_tooltip(page, "#ws-share-add-kind")


def _drive_confirm(page: Page) -> None:
    page.wait_for_selector("#ws-share-empty")
    page.click("#ws-share-publish-switch")
    page.wait_for_selector("#ws-share-publish-confirm")


def _drive_provisioning(page: Page) -> None:
    page.click("#ws-share-publish-switch")
    page.click("#ws-share-publish-confirm")
    # The third step is under way once the readiness poll has answered, which
    # the model asks for two seconds after the publish lands.
    page.wait_for_function(
        "() => (document.querySelector('#ws-share-provisioning')?.textContent ?? '').includes('Connecting to the relay')",
        timeout=20000,
    )
    _add_grant(page, "bob@example.org")
    _wait_for_row(page, BOB_ROW, SAVING_TEXT)


def _drive_published(page: Page) -> None:
    """Every state a row can be in at once: settled, just added, saving, failed.

    The order is forced by the model's one-write-at-a-time queue: a row is only
    left saving by a write that is still in flight, and only one write is, so
    the failures have to be taken first and the saving row made by retrying one
    of them. The whole sequence is also a race against the six-second highlight
    on the just-added row.
    """
    page.wait_for_selector(f'[data-grant-row="{CAROL_ROW}"]')
    _add_grant(page, "bob@example.org")
    _wait_for_row(page, BOB_ROW, SAVING_TEXT, is_present=False)
    _add_grant(page, "dan@example.org")
    _wait_for_row(page, DAN_ROW, FAILED_TEXT)
    _add_grant(page, "erin@example.org")
    _wait_for_row(page, ERIN_ROW, FAILED_TEXT)
    page.click(f'[data-grant-row="{DAN_ROW}"] button:has-text("Retry")')
    _wait_for_row(page, DAN_ROW, SAVING_TEXT)
    _hover_for_tooltip(page, f'[data-grant-row="{ACME_ROW}"]')


def _drive_app_pane(page: Page) -> None:
    page.click('[data-share-target="notes"]')
    page.wait_for_selector("#ws-share-inherited")
    page.select_option("#ws-share-add-kind", "email_domain")
    _add_grant(page, "gmail.com")
    page.wait_for_selector("#ws-share-refusal")
    # The copy confirmation fades after 1.2s, so it is the last thing done.
    page.click("#ws-share-copy")
    page.wait_for_selector("#ws-share-link .border-success", timeout=3000)


def _drive_off_with_grants(page: Page) -> None:
    page.wait_for_selector("#ws-share-off-notice")
    _hover_for_tooltip(page, "#ws-share-add-kind")


def _drive_long_list(page: Page) -> None:
    """Scroll the list and come back, since the mock's frame is drawn at the top.

    The wheel proves the list is its own scroll region rather than the pane
    growing; the capture is then taken where the mock draws it, thumb at the top.
    """
    page.wait_for_selector("#ws-share-grants")
    page.hover("#ws-share-grants")
    page.mouse.wheel(0, 600)
    page.wait_for_function("() => document.getElementById('ws-share-grants').scrollTop > 0", timeout=5000)
    scrolled = page.evaluate("() => document.getElementById('ws-share-grants').scrollTop")
    page.mouse.wheel(0, -600)
    page.wait_for_function("() => document.getElementById('ws-share-grants').scrollTop === 0", timeout=5000)
    metrics = page.evaluate(
        "() => { const list = document.getElementById('ws-share-grants');"
        " return [list.scrollHeight, list.clientHeight]; }"
    )
    if float(scrolled) <= 0 or metrics[0] <= metrics[1]:
        raise SharePanelRenderError(f"the grant list is not its own scroll region: scrolled {scrolled}, {metrics}")
    logger.info(
        "[b27] grant list scrolls to {} of scrollHeight {} in a region {} tall", scrolled, metrics[0], metrics[1]
    )


def _frames() -> tuple[ShareFrame, ...]:
    off_empty = _sharing(is_published=False, grants=EMPTY_GRANTS, labels=LIVE_LABELS)
    live_five = _sharing(is_published=True, grants=SETTLED_FIVE, labels=LIVE_LABELS)
    idle_readiness = SharingReadinessResponse(ready=True, service_labels=dict(LIVE_LABELS))
    return (
        ShareFrame(
            slug="b21",
            title="B2.1 First open, publishing off",
            sharing=off_empty,
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO,),
            drive=_drive_first_open,
        ),
        ShareFrame(
            slug="b22",
            title="B2.2 Confirming the first publish",
            sharing=off_empty,
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO,),
            drive=_drive_confirm,
        ),
        ShareFrame(
            slug="b23",
            title="B2.3 Provisioning",
            sharing=_sharing(is_published=False, grants=EMPTY_GRANTS, labels=PROVISIONING_LABELS),
            readiness=SharingReadinessResponse(
                ready=False,
                cert_not_after=CERT_NOT_AFTER,
                service_labels=dict(PROVISIONING_LABELS),
            ),
            published=_sharing(is_published=True, grants=EMPTY_GRANTS, labels=PROVISIONING_LABELS),
            grants_replies=(GrantsReply.HANG,),
            drive=_drive_provisioning,
        ),
        ShareFrame(
            slug="b24",
            title="B2.4 Published, whole workspace",
            sharing=_sharing(
                is_published=True,
                grants=SharingGrantsDocument(
                    workspace=SharingGrantList(users=("user-carol",), email_domains=("acme.example",)),
                    services={"notes": SharingGrantList(users=("user-frank",))},
                ),
                labels=LIVE_LABELS,
            ),
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO, GrantsReply.SERVER_ERROR, GrantsReply.SERVER_ERROR, GrantsReply.HANG),
            drive=_drive_published,
        ),
        ShareFrame(
            slug="b25",
            title="B2.5 The notes app",
            sharing=live_five,
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO,),
            drive=_drive_app_pane,
        ),
        ShareFrame(
            slug="b26",
            title="B2.6 Publishing turned off again",
            sharing=_sharing(is_published=False, grants=SETTLED_FIVE, labels=LIVE_LABELS),
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO,),
            drive=_drive_off_with_grants,
        ),
        ShareFrame(
            slug="b27",
            title="B2.7 A long list",
            sharing=_sharing(is_published=True, grants=THIRTEEN_GRANTS, labels=LIVE_LABELS),
            readiness=idle_readiness,
            published=None,
            grants_replies=(GrantsReply.ECHO,),
            drive=_drive_long_list,
        ),
    )


def _panel_clip(page: Page) -> dict[str, float]:
    """The card's width, from the top of the window down past the card's bottom edge.

    A clip rather than an element screenshot of the card, because the
    confirmation is a fixed overlay outside it and B2.2 is a shot of exactly
    that.
    """
    box = page.evaluate(
        "() => {"
        "  const boxes = [document.getElementById('ws-options-panel'),"
        "                 document.querySelector('.modal-viewport > div')]"
        "    .filter((el) => el !== null)"
        "    .map((el) => el.getBoundingClientRect());"
        "  if (boxes.length === 0) return null;"
        "  return [Math.min(...boxes.map((b) => b.left)),"
        "          Math.max(...boxes.map((b) => b.right)),"
        "          Math.max(...boxes.map((b) => b.bottom))];"
        "}"
    )
    if box is None:
        raise SharePanelRenderError("the options panel is not on screen")
    # Flush with the card: the workspace chrome behind it (an update banner
    # across the surface) would otherwise show in the margin.
    left = max(float(box[0]), 0)
    right = min(float(box[1]), VIEWPORT_W)
    return {
        "x": left,
        "y": 0,
        "width": right - left,
        "height": min(float(box[2]) + 24, VIEWPORT_H),
    }


def _capture_frame(page: Page, frame: ShareFrame, port: int, output_dir: Path) -> None:
    stubs = ShareRouteStubs(
        frame=frame,
        is_published=frame.sharing.enabled,
        labels=dict(frame.sharing.service_labels),
    )
    page.route("**/*", stubs.handle)
    try:
        page.goto(
            f"http://127.0.0.1:{port}/workspace/{AGENT_ID}/options?tab=share&visual-diff=1",
            wait_until="load",
            timeout=20000,
        )
        page.wait_for_selector("#ws-share-publish-switch", timeout=20000)
        frame.drive(page)
        page.screenshot(path=str(output_dir / f"{frame.slug}.png"), clip=_panel_clip(page))
        logger.info("[shot] {} -- {}", frame.slug, frame.title)
    finally:
        page.unroute("**/*", stubs.handle)


def _capture(arguments: ShareRenderArguments) -> None:
    if not arguments.is_build_skipped:
        build_spa_bundle()

    serve_dir = REPO_ROOT / "apps" / "minds" / ".visual-diff" / "share-panel-serve"
    if serve_dir.exists():
        shutil.rmtree(serve_dir)
    serve_dir.mkdir(parents=True)
    (serve_dir / "_static").symlink_to(STATIC_DIR)
    route = f"/workspace/{AGENT_ID}/options"
    index_path = serve_dir / "index.html"
    index_path.write_text(render_spa_index_html(build_spa_fixture_bootstrap()))

    arguments.output_dir.mkdir(parents=True, exist_ok=True)

    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    httpd = SpaCaptureServer(("127.0.0.1", port), SpaRouteHandler)
    httpd.spa_html_by_route = {route: index_path}
    httpd.spa_root_dir = serve_dir
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    frames = [f for f in _frames() if not arguments.slugs or f.slug in arguments.slugs]
    try:
        with sync_playwright() as pw:
            browser, _is_full_page_reliable = launch_chromium(pw)
            try:
                # Motion is left on: the just-added highlight IS an animation,
                # and prefers-reduced-motion turns it off entirely.
                context = browser.new_context(
                    viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                for frame in frames:
                    _capture_frame(page, frame, port, arguments.output_dir)
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    if arguments.copy_dir is not None:
        arguments.copy_dir.mkdir(parents=True, exist_ok=True)
        for frame in frames:
            shutil.copyfile(arguments.output_dir / f"{frame.slug}.png", arguments.copy_dir / f"built-{frame.slug}.png")
    logger.info("[share-panel] {} frames -> {}", len(frames), arguments.output_dir)


@click.command()
@click.option(
    "--output",
    default=str(DEFAULT_OUTPUT_DIR),
    type=click.Path(),
    help="Directory the seven captures are written to",
)
@click.option(
    "--copy-to",
    default=None,
    type=click.Path(),
    help="Extra directory to copy each capture into, named built-<slug>.png",
)
@click.option(
    "--skip-build/--build",
    default=False,
    help="Reuse the bundle already in static/ui instead of rebuilding the frontend",
)
@click.option(
    "--only",
    default="",
    help="Comma-separated frame slugs to capture (b21..b27); default is all seven",
)
def capture_share_panel_renders(output: str, copy_to: str | None, skip_build: bool, only: str) -> None:
    """Screenshot the share panel in the seven fixture states of the B2 mock."""
    setup_logging()
    arguments = ShareRenderArguments(
        output_dir=Path(output),
        copy_dir=Path(copy_to) if copy_to else None,
        is_build_skipped=skip_build,
        slugs=tuple(slug.strip() for slug in only.split(",") if slug.strip()),
    )
    _capture(arguments)


if __name__ == "__main__":
    capture_share_panel_renders()
