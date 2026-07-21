#!/usr/bin/env python3
"""Titlebar first-paint (no-flash) checker for the chrome shell.

The titlebar invariant: every page paints complete on arrival -- the
server-seeded accent (``--titlebar-bg``) and breadcrumb are correct in the
very first painted frame, and in-place hub swaps repaint the bar at most once
(old state -> new state, never a neutral flash in between).

This harness renders REAL pages through the production render functions,
serves them (plus ``/_static``) over a local HTTP server on exact app paths
(so the chrome.js swap engine engages), then drives a CPU-throttled chromium
via CDP screencast and samples every captured frame:

- the titlebar strip's background color (is it neutral or the accent), and
- whether the page body below has painted yet (so an unpainted white frame
  is not mistaken for a neutral-titlebar flash on accent pages).

Scenarios (the Phase 4 acceptance patterns, browser-mode):
  cold_home            load "/" -- neutral bar, no accent frame ever
  workspace_load       load an accent-seeded workspace-scoped page -- every
                       painted frame carries the accent (no neutral flash)
  workspace_reload     reload the same page -- same invariant
  hub_swap             "/" -> "/create" in-place swap -- bar stays neutral and
                       the titlebar element survives
  workspace_home_hops  workspace-settings <-> home swaps -- exactly one
                       accent<->neutral transition per hop

Run before a titlebar-affecting change to record a baseline, and after to
compare:

    uv run apps/minds/scripts/titlebar_first_paint_check.py --output /tmp/titlebar-report.json

The JSON report carries the per-scenario frame color sequences (run-length
compressed) for side-by-side comparison; the process exits non-zero if any
invariant fails.
"""

import argparse
import json
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from playwright.sync_api import sync_playwright
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.chrome_state import ChromeBootState
from imbue.minds.desktop_client.chrome_state import ChromeProvidersPayload
from imbue.minds.desktop_client.chrome_state import ChromeRequestsPayload
from imbue.minds.desktop_client.chrome_state import ChromeWorkspaceEntry
from imbue.minds.desktop_client.chrome_state import ChromeWorkspacesPayload
from imbue.minds.desktop_client.chrome_state import LandingBootExtras
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_workspace_settings

APP_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
STATIC_DIR: Final[Path] = APP_ROOT / "imbue" / "minds" / "desktop_client" / "static"

AGENT_ID: Final[str] = "agent-" + "a" * 32
# A saturated purple no neutral surface uses, so frame sampling is unambiguous.
ACCENT: Final[str] = "#7c3aed"
CPU_THROTTLE_RATE: Final[int] = 6
VIEWPORT: Final[dict[str, int]] = {"width": 1280, "height": 800}
# Sample points: mid-titlebar (away from buttons), and a content strip below
# it that carries text on every scenario page.
TITLEBAR_PROBE: Final[tuple[int, int]] = (640, 10)
CONTENT_STRIP_Y: Final[int] = 120


def _landing_html() -> str:
    boot = ChromeBootState(
        workspaces=ChromeWorkspacesPayload(
            workspaces=(
                ChromeWorkspaceEntry(
                    id=AGENT_ID,
                    name="ws-alpha",
                    accent=ACCENT,
                    supports_shutdown="true",
                    liveness="RUNNING",
                    provider="Docker",
                ),
            ),
            destroying_agent_ids=(),
            destroying_status_by_agent_id={},
            has_accounts=False,
            restorable_workspace_ids=(),
            remote_workspace_states={},
        ),
        providers=ChromeProvidersPayload(providers=(), last_event_at=None, last_full_snapshot_at=None),
        requests=ChromeRequestsPayload(count=0, request_ids=(), cards=(), auto_open=True),
        system_interface_statuses=(),
    )
    extras = LandingBootExtras(
        mngr_forward_origin="https://localhost:8421",
        account_email="",
        extra_account_count=0,
        locked_account_emails=(),
        is_discovering=False,
    )
    return render_landing_page(boot, extras)


def _render_routes() -> dict[str, str]:
    return {
        "/": _landing_html(),
        "/create": render_create_form(),
        f"/workspace/{AGENT_ID}/settings": render_workspace_settings(
            agent_id=AGENT_ID,
            ws_name="ws-alpha",
            current_account=None,
            accounts=(),
            servers=(),
            current_color=ACCENT,
        ),
    }


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in self.routes:
            body = self.routes[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/_static/"):
            file_path = STATIC_DIR / path.removeprefix("/_static/")
            if file_path.is_file():
                body = file_path.read_bytes()
                self.send_response(200)
                content_type = "text/css" if path.endswith(".css") else "application/javascript"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # ty: ignore[invalid-method-override]
        pass


def _serve(routes: dict[str, str]) -> tuple[socketserver.TCPServer, int]:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    _Handler.routes = routes
    httpd = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# In-browser JPEG decoding: draw each screencast frame into a canvas and
# sample the titlebar probe pixel + whether the content strip has painted.
_SAMPLER_JS: Final[str] = """
async (framesBase64) => {
  const results = [];
  for (const b64 of framesBase64) {
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = reject;
      img.src = 'data:image/jpeg;base64,' + b64;
    });
    const canvas = document.createElement('canvas');
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const scaleX = img.width / %VIEW_W%;
    const scaleY = img.height / %VIEW_H%;
    const p = ctx.getImageData(Math.round(%PROBE_X% * scaleX), Math.round(%PROBE_Y% * scaleY), 1, 1).data;
    const stripY = Math.round(%STRIP_Y% * scaleY);
    const strip = ctx.getImageData(0, stripY, img.width, 1).data;
    let paintedCount = 0;
    for (let i = 0; i < strip.length; i += 4) {
      if (strip[i] < 245 || strip[i + 1] < 245 || strip[i + 2] < 245) paintedCount += 1;
    }
    const toHex = (v) => v.toString(16).padStart(2, '0');
    results.push({
      titlebar: '#' + toHex(p[0]) + toHex(p[1]) + toHex(p[2]),
      isContentPainted: paintedCount > 8,
    });
  }
  return results;
}
"""


def _classify(color: str) -> str:
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    if red > 235 and green > 235 and blue > 235:
        return "neutral"
    # The accent is a saturated purple; anything strongly blue-dominant with
    # sub-neutral green counts (JPEG compression shifts exact values).
    if blue > 150 and green < 150:
        return "accent"
    return f"other({color})"


def _run_length(labels: list[str]) -> list[str]:
    compressed: list[str] = []
    for label in labels:
        if not compressed or compressed[-1] != label:
            compressed.append(label)
    return compressed


class _CaptureSession(MutableModel):
    """Screencast capture over one CPU-throttled page: collects frames per
    scenario, drops the outgoing page's pre-commit frames for full
    navigations, and samples each frame's titlebar color in-browser."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    page: Any = Field(frozen=True, description="The Playwright page under capture")
    cdp: Any = Field(frozen=True, description="The page's CDP session")
    frames: list[tuple[float, str]] = Field(default_factory=list, description="(received_at, base64 jpeg) per frame")
    # Main-frame navigation commit time: full-navigation scenarios drop frames
    # of the OUTGOING page (the screencast starts before the commit, so the
    # first frame may still show the previous page -- harness noise, not a
    # flash of the new page).
    last_commit_time: float = Field(default=0.0, description="Epoch seconds of the last main-frame commit")

    def attach(self) -> None:
        self.cdp.send("Page.enable")
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.cdp.on("Page.frameNavigated", self._on_frame_navigated)

    def _on_frame(self, params: dict[str, Any]) -> None:
        self.frames.append((time.time(), params["data"]))
        self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        if params.get("frame", {}).get("parentId") is None:
            self.last_commit_time = time.time()

    def capture(self, action_name: str, action: Any, drops_precommit_frames: bool = False) -> list[dict[str, Any]]:
        self.frames.clear()
        self.last_commit_time = 0.0
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "everyNthFrame": 1})
        action()
        self.page.wait_for_timeout(1200)
        self.cdp.send("Page.stopScreencast")
        kept = [
            data
            for received_at, data in self.frames
            if not drops_precommit_frames or received_at >= self.last_commit_time
        ]
        sampler = (
            _SAMPLER_JS.replace("%VIEW_W%", str(VIEWPORT["width"]))
            .replace("%VIEW_H%", str(VIEWPORT["height"]))
            .replace("%PROBE_X%", str(TITLEBAR_PROBE[0]))
            .replace("%PROBE_Y%", str(TITLEBAR_PROBE[1]))
            .replace("%STRIP_Y%", str(CONTENT_STRIP_Y))
        )
        samples = self.page.evaluate(sampler, kept)
        logger.info("[{}] {} frames: {}", action_name, len(samples), samples)
        return samples


def _painted_titlebar_labels(samples: list[dict[str, Any]]) -> list[str]:
    return [_classify(sample["titlebar"]) for sample in samples if sample["isContentPainted"]]


def _run_scenarios(port: int) -> dict[str, Any]:
    origin = f"http://127.0.0.1:{port}"
    report: dict[str, Any] = {}
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT)  # ty: ignore[invalid-argument-type]
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": CPU_THROTTLE_RATE})
        session = _CaptureSession(page=page, cdp=cdp)
        session.attach()
        capture = session.capture
        painted_titlebar_labels = _painted_titlebar_labels

        # -- cold_home: neutral bar from the first painted frame ------------
        samples = capture("cold_home", lambda: page.goto(f"{origin}/", wait_until="load"), drops_precommit_frames=True)
        labels = painted_titlebar_labels(samples)
        report["cold_home"] = _run_length(labels)
        if any(label == "accent" for label in labels):
            failures.append(f"cold_home: accent frame on a neutral page ({_run_length(labels)})")

        # -- workspace_load + reload: accent from the first painted frame ---
        for name in ("workspace_load", "workspace_reload"):
            if name == "workspace_load":
                samples = capture(
                    name,
                    lambda: page.goto(f"{origin}/workspace/{AGENT_ID}/settings", wait_until="load"),
                    drops_precommit_frames=True,
                )
            else:
                samples = capture(name, lambda: page.reload(wait_until="load"), drops_precommit_frames=True)
            labels = painted_titlebar_labels(samples)
            report[name] = _run_length(labels)
            if any(label == "neutral" for label in labels):
                failures.append(f"{name}: neutral flash on an accent-seeded page ({_run_length(labels)})")

        # -- hub_swap: '/' -> '/create' stays neutral, titlebar survives ----
        page.goto(f"{origin}/", wait_until="load")
        page.evaluate("() => { document.getElementById('minds-titlebar').dataset.probe = 'kept'; }")
        samples = capture("hub_swap", lambda: page.evaluate("() => window.__mindsNavigateContent('/create')"))
        labels = painted_titlebar_labels(samples)
        report["hub_swap"] = _run_length(labels)
        if any(label == "accent" for label in labels):
            failures.append(f"hub_swap: accent frame during a neutral hub swap ({_run_length(labels)})")
        if page.evaluate("() => document.getElementById('minds-titlebar').dataset.probe") != "kept":
            failures.append("hub_swap: the titlebar element was rebuilt")
        if page.evaluate("() => location.pathname") != "/create":
            failures.append("hub_swap: swap did not navigate to /create")

        # -- workspace_home_hops: one transition per in-place hop -----------
        page.goto(f"{origin}/workspace/{AGENT_ID}/settings", wait_until="load")
        samples = capture("hop_to_home", lambda: page.evaluate("() => window.__mindsNavigateContent('/')"))
        labels = _run_length(painted_titlebar_labels(samples))
        report["hop_to_home"] = labels
        if [label for label in labels if label.startswith("other")] or labels not in (
            ["accent", "neutral"],
            ["neutral"],
            ["accent"],
        ):
            failures.append(f"hop_to_home: expected one accent->neutral transition, saw {labels}")
        samples = capture(
            "hop_to_workspace",
            lambda: page.evaluate(f"() => window.__mindsNavigateContent('/workspace/{AGENT_ID}/settings')"),
        )
        labels = _run_length(painted_titlebar_labels(samples))
        report["hop_to_workspace"] = labels
        if [label for label in labels if label.startswith("other")] or labels not in (
            ["neutral", "accent"],
            ["accent"],
            ["neutral"],
        ):
            failures.append(f"hop_to_workspace: expected one neutral->accent transition, saw {labels}")

        browser.close()

    report["failures"] = failures
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path to write the JSON frame report")
    arguments = parser.parse_args()

    routes = _render_routes()
    httpd, port = _serve(routes)
    try:
        report = _run_scenarios(port)
    finally:
        httpd.shutdown()
        httpd.server_close()

    Path(arguments.output).write_text(json.dumps(report, indent=2))
    logger.info("report written to {}", arguments.output)
    if report["failures"]:
        for failure in report["failures"]:
            logger.error("FAIL: {}", failure)
        sys.exit(1)
    logger.info("all titlebar first-paint invariants hold")


if __name__ == "__main__":
    main()
