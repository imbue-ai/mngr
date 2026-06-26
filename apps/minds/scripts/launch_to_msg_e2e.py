"""End-to-end manual-trigger test for minds.app launch -> first message
-> slack permission flow. ONE Python script that replaces:

  apps/minds/scripts/launch-and-verify.sh
  apps/minds/scripts/first-message-verify.sh
  apps/minds/scripts/slack-mock-setup.sh
  apps/minds/scripts/slack-mock-teardown.sh
  apps/minds/test/e2e/drive-slack-ci.js
  apps/minds/test/e2e/mocks/slack-mock-server.js

Invoked from .github/workflows/minds-launch-to-msg.yml as the single
test step. NOT a pytest test (no markers, no collection).

Flow:
  1. Launch minds.app via Playwright Electron, UI auth via
     /authenticate?one_time_code=... (minted on disk)
  2. Workspace 1 (W1): click Create, fill form with HOST_NAME, wait for
     agent DONE, send first message ("pong"), wait for reply (screenshots
     03-06)
  2b. Reload W1 chat (Cmd-R surrogate via win.reload), assert "pong"
     history still rendered -- catches session/event-stream re-attach
     regressions (screenshot 06b)
  3. Slack flow on W1 (if enabled): stand up local slack mock HTTPS
     server on :443 via sudo socat TLS-terminator, patch /etc/hosts,
     pre-seed latchkey slack credential, send slack prompt; click
     Requests -> entry -> Approve, kick agent to retry; verify canned
     MESSAGE_BODY appears (screenshots 07-08)
  4. (if WORKSPACE_COUNT >= 2) Workspace 2 (W2): same create-form
     flow with HOST_NAME_2, send first message, wait for reply
     (screenshots 09-12)
  5. Cross-workspace follow-up: navigate back to W1's chat URL, send
     a unique-token prompt (bing), wait for reply; then to W2's
     (bong) (screenshots 13-16)
  6. Home-page tiles check: navigate to /, assert BOTH tiles render
     (screenshot 17)
  6b. Click W1's tile, assert URL carries W1's specific agent-<hex>.localhost
     -- exercises the tile.onclick + /goto/ route, not URL navigation
     (screenshot 17b)
  6c. /accounts page smoke: navigate to /accounts, assert renders
     (screenshot 17c)
  6d. W1 settings page renders: visit /workspace/<w1>/settings, assert
     destroy button + HOST_NAME present in body (screenshot 17d)
  7. Destroy W2 via the UI (gear icon -> WorkspaceSettings page ->
     "Back to projects" link round-trip [iter 14, screenshot 18a] ->
     Destroy button -> modal Cancel-and-reopen round-trip [iter 12,
     screenshots 18b/18b2/18b3] -> Destroy Confirm), poll until done,
     assert home drops W2 tile; send a unique-token follow-up to W1
     (bink), verify reply (screenshots 18, 18a, 18b, 18b2, 18b3, 18c,
     19-22)
  8. mngr CLI from host: run `mngr list --format json` against the
     bundled host_dir, assert W1's host is listed AND W2's is gone
     -- cross-checks the destroy lifecycle from a different angle
     than the UI's home-page tile state.
  9. Duplicate-name conflict: POST /api/create-agent with HOST_NAME
     already owned by W1; assert 409 with "already exists". Proves
     the duplicate-name guard added on this branch works.
  10. Teardown: revert /etc/hosts, clear latchkey, kill mock + socat

Takes a `screencapture -x` whole-desktop shot at every milestone.
Files land in /tmp/launch-to-msg-screenshots/ and the workflow
publishes them to the ci-screenshots branch.
"""

import asyncio
import contextlib
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

# Playwright-Python has no Electron binding (only Node has _electron.launch).
# Attach via CDP instead: launch minds.app ourselves with
# `--remote-debugging-port=<N>` so its Chromium DevTools endpoint is reachable,
# then `chromium.connect_over_cdp()`. Same API for pages, locators, etc.
from playwright.async_api import BrowserContext
from playwright.async_api import Page
from playwright.async_api import async_playwright
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class E2EFailure(Exception):
    """Raised for end-to-end drive failures specific to this script's domain.

    Used in preference to a bare ``RuntimeError`` for failures that arise from
    e2e contract assumptions -- the minds API surface, the redirect flow, the
    chat-URL pattern -- so the harness can distinguish them from arbitrary
    Python runtime errors in failure handling. Inherits from ``Exception``
    (not ``RuntimeError``) so the project ratchet that flags raise-of-
    builtins doesn't apply.
    """


# --- knobs (override via env) ---

MINDS_APP_PATH = Path(os.environ.get("MINDS_APP_PATH", "/Applications/Minds.app/Contents/MacOS/Minds"))
MINDS_HOME = Path(os.environ.get("HOME", "/Users/macrunner")) / ".minds"
EVENTS_LOG = MINDS_HOME / "logs" / "minds-events.jsonl"
ONE_TIME_CODES = MINDS_HOME / "auth" / "one_time_codes.json"
SCREENSHOT_DIR = Path(os.environ.get("LAUNCH_TO_MSG_SHOTS_DIR", "/tmp/launch-to-msg-screenshots"))
SLACK_MOCK_STATE = Path("/tmp/slack-mock")
# Plain HTTP; socat terminates TLS on :443.
SLACK_MOCK_PORT = 8443
LATCHKEY_DIR = MINDS_HOME / "latchkey"
# The latchkey-gateway extension writes pending permission request files
# here. Iter 10 reads this directory directly to verify that Claude
# re-submits a permission request after a deny (rather than infer it
# from the requests-panel UI, which doesn't auto-refresh).
PERMISSION_REQUESTS_DIR = LATCHKEY_DIR / "permission_requests" / "v2"

HOST_NAME = os.environ.get("HOST_NAME") or f"e2e{time.strftime('%H%M%S')}"
HOST_NAME_2 = os.environ.get("HOST_NAME_2") or f"{HOST_NAME}-b"
# WORKSPACE_COUNT=1 (default) preserves the single-workspace flow for local
# repro; CI sets =2 to drive the second workspace + cross-workspace follow-up
# pings as an end-to-end isolation check on the same minds.app session.
WORKSPACE_COUNT = int(os.environ.get("WORKSPACE_COUNT", "1"))

FIRST_PROMPT = "Reply with exactly the four characters: pong"
FIRST_EXPECT = "pong"
# Cross-workspace follow-up prompts use tokens that the chat body does NOT
# contain before the prompt fires, so the >=2-occurrence check (prompt echo
# + reply bubble) proves the agent responded to the NEW message rather than
# counting carryover from earlier slack/first-message bubbles.
FOLLOWUP_W1_PROMPT = "Reply with exactly the four characters: bing"
FOLLOWUP_W1_EXPECT = "bing"
FOLLOWUP_W2_PROMPT = "Reply with exactly the four characters: bong"
FOLLOWUP_W2_EXPECT = "bong"

CREATE_TIMEOUT = 900
REPLY_TIMEOUT = 480
DRIVE_SLACK_TIMEOUT = 360
LAUNCH_BACKEND_TIMEOUT = 120

# Canned slack-mock body the agent should quote back.
CANNED_BODY = "CI MOCK: greetings from the localhost slack mock."

SLACK_PROMPT = (
    "Please read the most recent message from any Slack channel using a "
    "read-only Slack tool. Don't post anything. Then tell me here in chat "
    "what the message says -- you can quote it inline or summarise; "
    "either is fine, but I need to see the message text. Thanks!"
)

SKIP_SLACK_FLOW = os.environ.get("SKIP_SLACK_FLOW", "0") == "1"
# Dev-only escape hatch (no CI workflow exposes this). Lets a local repro
# stop after install + launch + backend-ready without driving the create
# form -- useful when the failure is upstream of the form.
SKIP_FIRST_MESSAGE = os.environ.get("SKIP_FIRST_MESSAGE", "0") == "1"

# --- snap helpers ---

if SCREENSHOT_DIR.exists():
    # Self-hosted runner persists /tmp; stale shots from past runs would
    # be re-published into this run's side-branch dir otherwise.
    for stale in SCREENSHOT_DIR.iterdir():
        with contextlib.suppress(Exception):
            stale.unlink()
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _activate_minds() -> None:
    """Bring the Minds window forward before snapping so screencapture
    sees the app, not just the wallpaper. Best-effort; silent on failure."""
    subprocess.run(
        ["osascript", "-e", 'tell application "Minds" to activate'],
        check=False,
        capture_output=True,
        timeout=5,
    )


def _dismiss_screensaver() -> None:
    """Kill any active ScreenSaverEngine so the next screencapture
    sees the app, not the screensaver wallpaper. Belt-and-braces to
    the long-running ``caffeinate -dimsu`` we start in ``amain``."""
    subprocess.run(["killall", "ScreenSaverEngine"], check=False, capture_output=True, timeout=3)


def _safe_snap_name(name: str) -> str:
    """Slugify a snapshot name so it is always a valid file / artifact path.

    Names sometimes embed a page URL (e.g. a ``/recovery?return_to=...`` URL on
    a failed redirect). Characters like ``?``, ``:``, ``%``, ``/`` produce a
    filename ``actions/upload-artifact`` rejects, which drops ALL of the run's
    diagnostics -- exactly when a failure makes them most valuable. Replace
    anything outside ``[A-Za-z0-9._-]`` with ``-`` and cap the length.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug[:120] or "snap"


def snap(name: str) -> None:
    """Whole-desktop screencapture. No-ops cleanly when there's no Aqua session."""
    name = _safe_snap_name(name)
    _dismiss_screensaver()
    _activate_minds()
    out = SCREENSHOT_DIR / f"{name}.png"
    err = SCREENSHOT_DIR / f"{name}.err"
    subprocess.run(
        ["screencapture", "-x", str(out)],
        stderr=err.open("w"),
        check=False,
    )
    if out.exists() and out.stat().st_size > 0:
        logger.info("  snap[{}] -> {} bytes", name, out.stat().st_size)
    else:
        msg = err.read_text(errors="ignore").strip() if err.exists() else "?"
        # The headless CI runner has no Aqua display session, so screencapture
        # fails with "could not create image from display" at every milestone --
        # expected there, not worth a per-snapshot warning. The Playwright page
        # screenshots in snap_page() are the real diagnostics on that runner.
        if "could not create image" in msg.lower():
            logger.debug("  snap[{}] skipped (no display session)", name)
        else:
            logger.warning("  snap[{}] FAILED: {}", name, msg)
        if out.exists():
            out.unlink()
    if err.exists():
        err.unlink()


async def snap_page(page: Page, name: str) -> None:
    """Both Playwright per-page shot AND macOS desktop shot.

    Raise this page's BrowserWindow to the top of the macOS z-order
    BEFORE the screencapture; otherwise the full-desktop shot just
    captures whatever Minds window the WindowServer has at front
    (usually still the original /welcome window because Playwright
    routes UI events through CDP, never through a real mouse click
    that would update WindowServer focus).
    """
    try:
        await page.set_viewport_size({"width": 1280, "height": 800})
    except Exception:
        pass
    try:
        await page.bring_to_front()
    except Exception as e:
        logger.warning("  bring_to_front[{}] failed: {}", name, e)
    try:
        await page.screenshot(path=str(SCREENSHOT_DIR / f"{_safe_snap_name(name)}.win.png"), full_page=True)
    except Exception as e:
        logger.warning("  page-shot[{}] failed: {}", name, e)
    snap(name)


# --- HTTP slack mock (stdlib only) ---


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


class _SlackMockHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_body(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("[mock] " + (fmt % args))

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler convention)
        path = urllib.parse.urlparse(self.path).path
        logger.info("[mock] GET {}", path)
        if path == "/api/auth.test":
            self._send(
                200,
                {
                    "ok": True,
                    "url": "https://slack.com/",
                    "team": "Imbue CI Mock",
                    "user": "mock-bot",
                    "team_id": "TMOCK000",
                    "user_id": "UMOCK001",
                },
            )
        elif path == "/api/conversations.list":
            self._send(
                200,
                {
                    "ok": True,
                    "channels": [
                        {
                            "id": "CMOCK000",
                            "name": "ci-mock-channel",
                            "is_channel": True,
                            "is_member": True,
                            "is_private": False,
                            "num_members": 2,
                        }
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        elif path == "/api/conversations.history":
            self._send(
                200,
                {
                    "ok": True,
                    "messages": [
                        {
                            "type": "message",
                            "user": "UMOCK001",
                            "text": CANNED_BODY,
                            "ts": "1717085000.000100",
                            "username": "Mock Sender",
                        }
                    ],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                },
            )
        else:
            self._send(404, {"ok": False, "error": "mock_unimplemented_endpoint", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        logger.info("[mock] POST {}", path)
        if path == "/api/oauth/v2/access":
            self._send(
                200,
                {
                    "ok": True,
                    "access_token": "xoxc-mock-token-for-ci-only",
                    "scope": "channels:history,channels:read",
                    "team": {"id": "TMOCK000", "name": "Imbue CI Mock"},
                    "authed_user": {
                        "id": "UMOCK001",
                        "scope": "identify",
                        "access_token": "xoxp-mock-user-token",
                    },
                },
            )
        else:
            self._send(404, {"ok": False, "error": "mock_unimplemented_endpoint", "path": path})


class _ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_mock() -> _ThreadedHTTP:
    server = _ThreadedHTTP(("127.0.0.1", SLACK_MOCK_PORT), _SlackMockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # quick liveness probe
    for _ in range(10):
        try:
            s = socket.create_connection(("127.0.0.1", SLACK_MOCK_PORT), timeout=1)
            s.close()
            logger.info("[mock] listening on http://127.0.0.1:{}", SLACK_MOCK_PORT)
            return server
        except OSError:
            time.sleep(0.2)
    raise E2EFailure(f"slack mock failed to bind {SLACK_MOCK_PORT}")


# --- cert + /etc/hosts + socat + latchkey wiring ---


def ensure_cert() -> Path:
    """Generate a fresh self-signed cert for slack.com + files.slack.com every run.

    A stale cert (from a prior run, possibly expired) would still pass the
    ``exists()`` check; regenerate unconditionally so the keychain-trust
    step below operates on the cert socat actually serves."""
    SLACK_MOCK_STATE.mkdir(parents=True, exist_ok=True)
    cert = SLACK_MOCK_STATE / "cert.pem"
    key = SLACK_MOCK_STATE / "key.pem"
    cert.unlink(missing_ok=True)
    key.unlink(missing_ok=True)
    logger.info("generating self-signed cert for slack.com")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=slack.com",
            "-addext",
            "subjectAltName=DNS:slack.com,DNS:files.slack.com",
        ],
        check=True,
        capture_output=True,
    )
    return cert


def ensure_combined_cert_bundle(cert: Path) -> Path:
    """Concatenate the self-signed slack cert with the system root CAs.

    Setting ``CURL_CA_BUNDLE`` to a single self-signed cert makes curl
    distrust everything else (anthropic.com, github.com, ...). Combining
    with the system roots keeps non-slack calls working while still
    adding our cert for the /etc/hosts-mapped slack.com hits."""
    bundle = SLACK_MOCK_STATE / "ca_bundle.pem"
    parts = [cert.read_text()]
    for sys_bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if Path(sys_bundle).exists():
            parts.append(Path(sys_bundle).read_text())
            break
    bundle.write_text("".join(parts))
    return bundle


def ensure_brew_curl() -> Path:
    """Return the path to a curl built against OpenSSL (honors --cacert).
    macOS system curl uses SecureTransport and ignores CURL_CA_BUNDLE."""
    for c in ("/opt/homebrew/opt/curl/bin/curl", "/usr/local/opt/curl/bin/curl"):
        if Path(c).exists():
            return Path(c)
    logger.info("brew curl missing -> brew install curl")
    brew = next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(b).exists()), None)
    if brew is None:
        raise E2EFailure("neither brew curl nor brew found; install brew + curl")
    subprocess.run([brew, "install", "curl"], check=True)
    return ensure_brew_curl()


def patch_etc_hosts() -> None:
    marker = "# slack-mock"
    line = "127.0.0.1 slack.com files.slack.com  " + marker
    logger.info("patching /etc/hosts")
    subprocess.run(["sudo", "sed", "-i.bak", f"/{marker}/d", "/etc/hosts"], check=True)
    subprocess.run(
        ["sudo", "tee", "-a", "/etc/hosts"],
        input=line + "\n",
        text=True,
        check=True,
        capture_output=True,
    )


def revert_etc_hosts() -> None:
    logger.info("reverting /etc/hosts")
    subprocess.run(
        ["sudo", "sed", "-i.bak", "/# slack-mock/d", "/etc/hosts"],
        check=False,
    )


def start_socat(cert: Path) -> subprocess.Popen:
    """Sudo socat: TLS-terminate :443 on 127.0.0.1 -> 127.0.0.1:SLACK_MOCK_PORT."""
    key = cert.with_suffix(".pem").parent / "key.pem"
    log = SLACK_MOCK_STATE / "socat.log"
    logger.info("starting socat :443 -> :{}", SLACK_MOCK_PORT)
    return subprocess.Popen(
        [
            "sudo",
            "socat",
            "-d",
            f"OPENSSL-LISTEN:443,bind=127.0.0.1,reuseaddr,fork,verify=0,cert={cert},key={key}",
            f"TCP:127.0.0.1:{SLACK_MOCK_PORT}",
        ],
        stdout=log.open("a"),
        stderr=subprocess.STDOUT,
    )


def stop_socat() -> None:
    # The launched socat ran under sudo so kill via sudo.
    _kill_pgrep("OPENSSL-LISTEN:443", "stale socat", sudo=True)


def latchkey_shim() -> Path:
    return MINDS_APP_PATH.parent.parent / "Resources" / "latchkey" / "bin" / "latchkey"


def latchkey_env() -> dict[str, str]:
    """Env the bundled latchkey shim needs: encryption key + Electron exec path."""
    key_file = LATCHKEY_DIR / "encryption_key"
    if not key_file.exists():
        raise E2EFailure(f"latchkey encryption_key missing at {key_file}")
    return {
        **os.environ,
        "LATCHKEY_DIRECTORY": str(LATCHKEY_DIR),
        "LATCHKEY_ENCRYPTION_KEY": key_file.read_text().strip(),
        "MINDS_ELECTRON_EXEC_PATH": str(MINDS_APP_PATH),
    }


def latchkey_set_slack() -> None:
    logger.info("pre-seeding latchkey slack creds")
    subprocess.run(
        [
            str(latchkey_shim()),
            "auth",
            "set",
            "slack",
            "-H",
            "Authorization: Bearer xoxc-ci-mock-token",
        ],
        env=latchkey_env(),
        check=True,
    )


def latchkey_clear_slack() -> None:
    subprocess.run(
        [str(latchkey_shim()), "auth", "clear", "slack"],
        env=latchkey_env(),
        check=False,
    )


# --- minds.app launcher + auth ---


def wait_backend_url(since_offset: int = 0) -> str:
    """Return the http://127.0.0.1:<port> of the backend, from the events log.

    ``since_offset`` is the byte offset to start scanning from. The
    second launch (iter 9: quit + relaunch) captures the file size
    before relaunching and passes it here so we match the NEW backend's
    login URL line, not the previous run's.
    """

    deadline = time.time() + LAUNCH_BACKEND_TIMEOUT
    pattern = re.compile(
        r"Minds login URL \(one-time use\): (http://(?:127\.0\.0\.1|localhost):\d+/login\?one_time_code=[A-Za-z0-9_-]+)"
    )
    while time.time() < deadline:
        if EVENTS_LOG.exists() and EVENTS_LOG.stat().st_size > since_offset:
            with EVENTS_LOG.open("rb") as f:
                f.seek(since_offset)
                data = f.read().decode("utf-8", errors="replace")
            for line in data.splitlines():
                m = pattern.search(line)
                if m:
                    url = m.group(1).replace("localhost", "127.0.0.1")
                    base = url.split("/login")[0]
                    return base
        time.sleep(2)
    raise E2EFailure(f"no backend login URL after {LAUNCH_BACKEND_TIMEOUT}s (since_offset={since_offset})")


def mint_one_time_code() -> str:
    code = secrets.token_urlsafe(32)
    ONE_TIME_CODES.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, str]] = []
    if ONE_TIME_CODES.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(ONE_TIME_CODES.read_text())
    existing.append({"code": code, "status": "VALID"})
    ONE_TIME_CODES.write_text(json.dumps(existing, indent=2))
    return code


def _free_port() -> int:
    """Reserve an ephemeral port for minds.app's CDP endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_cdp(port: int, timeout: float = 60.0) -> str:
    """Wait until http://127.0.0.1:<port>/json/version is reachable."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.5)
    raise E2EFailure(f"CDP not reachable on :{port} after {timeout}s: {last_err}")


def all_pages(ctx: BrowserContext) -> list[Page]:
    return list(ctx.pages)


async def find_chat_window(ctx: BrowserContext) -> Page | None:
    """Find the WebContentsView whose URL matches the chat URL (agent-*localhost)."""

    pat = re.compile(r"agent-[a-f0-9]+\.localhost")
    for w in all_pages(ctx):
        with contextlib.suppress(Exception):
            if pat.search(w.url):
                return w
    return None


# --- per-workspace helpers ---


class _SnapPrefixes(BaseModel):
    """Per-workspace screenshot name prefixes.

    Held in separate constants per workspace so the original W1 names
    predating W2 stay stable; alphabetic sort of the screenshot dir
    still reflects chronological execution (W1 03-06, slack 07-08,
    W2 09-12, cross-workspace 13-16).
    """

    model_config = ConfigDict(frozen=True)

    submitted: str
    done: str
    creating_mid: str
    msg_sent: str
    msg_reply: str


_W1_SNAPS = _SnapPrefixes(
    submitted="03-create-agent-submitted",
    done="04-agent-DONE",
    creating_mid="04b-creating-workspace-mid",
    msg_sent="05-first-message-sent",
    msg_reply="06-first-message-reply",
)
_W2_SNAPS = _SnapPrefixes(
    submitted="09-create-agent-submitted-w2",
    done="10-agent-DONE-w2",
    creating_mid="10b-creating-workspace-mid-w2",
    msg_sent="11-first-message-sent-w2",
    msg_reply="12-first-message-reply-w2",
)


class _WorkspaceResult(BaseModel):
    chat_url: str
    creation_id: str
    phase_durations: dict[str, float] = Field(default_factory=dict)
    total_create_s: float = 0.0


async def _create_workspace_and_first_message(
    ctx: BrowserContext,
    win: Page,
    *,
    origin: str,
    host_name: str,
    ai_provider: str,
    anthropic_key: str,
    snaps: _SnapPrefixes,
    label: str,
) -> _WorkspaceResult:
    """Drive create-form -> first-message for one workspace; navigate `win` to its chat.

    Steps: navigate to /create, fill the form for `host_name`, submit,
    poll /api/create-agent/<id>/status until DONE, follow the
    redirect_url to the agent chat URL on `win`, send FIRST_PROMPT,
    wait for a >=2-occurrence reply of FIRST_EXPECT. Snaps each
    milestone with names from `snaps`.

    The same `win` Page is reused across calls; for a second workspace
    the caller passes the (now-W1-chat-URL) `win` in, and this function
    overwrites it via ``win.goto(origin + "/create")``. The W1 chat
    URL is preserved on the underlying BrowserWindow's history; the
    caller can navigate back via the URL it captured before this call.

    `label` appears in log lines so two sequential workspaces are
    distinguishable in CI logs (e.g. "w1" vs "w2").
    """
    await win.goto(origin + "/create")
    await win.wait_for_selector("#create-form", timeout=10_000)
    # Signed out (no Imbue account): pick the "local" preset first so the AI /
    # backup providers are the non-cloud set. Otherwise the form defaults to the
    # Imbue Cloud providers, and submitting a cloud provider with no account
    # opens the sign-in modal instead of creating. The explicit launch_mode /
    # ai_provider selections below override the preset's compute / AI choices.
    await win.click('[data-preset="local"]')
    await win.click("#toggle-advanced")
    await win.wait_for_selector("#advanced-view:not(.hidden)", timeout=5_000)
    await win.select_option("#launch_mode", value="LIMA")
    await win.select_option("#ai_provider", value=ai_provider)
    if ai_provider == "API_KEY":
        await win.wait_for_selector("#api-key-row:not(.hidden)", timeout=5_000)
        await win.fill("#anthropic_api_key", anthropic_key)
    await win.fill("#host_name", host_name)
    async with win.expect_navigation(url=re.compile(r"/creating/[a-z0-9-]+")):
        await win.click("#create-submit")
    with contextlib.suppress(Exception):
        await win.wait_for_function(
            "document.body.innerText.includes('Setting up your workspace')",
            timeout=15_000,
        )
    await snap_page(win, snaps.submitted)
    m = re.search(r"/creating/([a-z0-9-]+)", win.url)
    if not m:
        raise E2EFailure(f"[{label}] expected /creating/<id> after submit, got url={win.url}")
    creation_id = m.group(1)
    logger.info("[{}] creation_id={}", label, creation_id)

    deadline = time.time() + CREATE_TIMEOUT
    last_status = ""
    phase_started_at = time.monotonic()
    phase_durations: dict[str, float] = {}
    done = False
    done_redirect_url = ""
    navigated_to_workspace = False
    chat_url_re = re.compile(r"agent-[a-f0-9]+\.localhost")
    while time.time() < deadline and not done:
        stat = await win.evaluate(
            """async (id) => {
                const r = await fetch('/api/create-agent/' + id + '/status');
                return {status: r.status, body: await r.text()};
            }""",
            creation_id,
        )
        payload = {}
        with contextlib.suppress(Exception):
            payload = json.loads(stat["body"])
        state = payload.get("status", "")
        if state != last_status:
            now = time.monotonic()
            if last_status:
                phase_durations[last_status] = round(now - phase_started_at, 2)
                logger.info(
                    "[{}] creation status: {} -> {} (prev took {:.1f}s)",
                    label,
                    last_status,
                    state,
                    phase_durations[last_status],
                )
            else:
                logger.info("[{}] creation status: (none) -> {}", label, state)
            last_status = state
            phase_started_at = now
            if not state and not chat_url_re.search(win.url):
                # An empty status off the workspace origin is anomalous: the
                # /status body had no "status" field -- a 403 "Not authenticated"
                # or 404 "Unknown agent creation". (Empty status *on* the
                # workspace URL is the normal auto-navigate-on-DONE case, handled
                # below.) Log the raw response to tell the anomalies apart.
                with contextlib.suppress(Exception):
                    logger.warning(
                        "[{}]   empty-status detail: http={} url={} body={!r}",
                        label,
                        stat.get("status"),
                        win.url,
                        (stat.get("body") or "")[:160],
                    )
        if state == "DONE":
            phase_durations[state] = round(time.monotonic() - phase_started_at, 2)
            done_redirect_url = payload.get("redirect_url", "")
            done = True
            break
        if state == "FAILED":
            raise E2EFailure(f"[{label}] creation FAILED: {payload.get('error', stat['body'])}")
        # On DONE, creating.js auto-navigates the page to the workspace; the
        # /status fetch then hits the workspace origin and returns HTML, so
        # `state` reads empty. Landing on the workspace URL *is* success -- the
        # page is already where the redirect below would send it -- so the poll
        # must not keep waiting for a "DONE" it already missed.
        if not state and chat_url_re.search(win.url):
            logger.info("[{}] creation DONE (page auto-navigated to workspace {})", label, win.url)
            done = True
            navigated_to_workspace = True
            break
        if (
            state == "CREATING_WORKSPACE"
            and not any(SCREENSHOT_DIR.glob(f"{snaps.creating_mid}.*"))
            and time.monotonic() - phase_started_at >= 60
        ):
            await snap_page(win, snaps.creating_mid)
        await asyncio.sleep(5)
    total_create_s = sum(phase_durations.values())
    logger.info("[{}] creation phase timings: {} (total={:.1f}s)", label, phase_durations, total_create_s)
    if not done:
        if EVENTS_LOG.exists():
            tail = EVENTS_LOG.read_text(errors="ignore").splitlines()[-60:]
            logger.error("[{}] minds-events.jsonl tail:\n{}", label, "\n".join(tail))
        raise E2EFailure(f"[{label}] creation didn't reach DONE in {CREATE_TIMEOUT}s (last={last_status})")
    if navigated_to_workspace:
        # The page already navigated to the workspace on its own; no goto needed.
        logger.info("[{}] creation DONE; already on workspace {}", label, win.url)
        target = win.url
    elif not done_redirect_url:
        raise E2EFailure(
            f"[{label}] creation DONE without redirect_url; check the /api/create-agent/<id>/status contract"
        )
    else:
        target = done_redirect_url if done_redirect_url.startswith("http") else origin + done_redirect_url
        logger.info("[{}] creation DONE; navigating directly to {}", label, target)
        await win.goto(target)
    chat_wait_seconds = 180
    chat_deadline = time.time() + chat_wait_seconds
    while time.time() < chat_deadline and not chat_url_re.search(win.url):
        await asyncio.sleep(0.5)
    if not chat_url_re.search(win.url):
        for p in all_pages(ctx):
            with contextlib.suppress(Exception):
                await snap_page(p, f"99-{label}-no-chat-{p.url.split('/')[-1] or 'root'}")
        raise E2EFailure(
            f"[{label}] goto({target}) didn't redirect to chat URL within {chat_wait_seconds}s (win.url={win.url})"
        )
    logger.info("[{}] agent DONE; chat URL={}", label, win.url)
    await snap_page(win, snaps.done)

    inp = await win.wait_for_selector('textarea, [contenteditable="true"]', timeout=180_000)
    await inp.fill(FIRST_PROMPT)
    await inp.press("Enter")
    with contextlib.suppress(Exception):
        await win.wait_for_function(
            f"document.body.innerText.includes({FIRST_PROMPT!r})",
            timeout=10_000,
        )
    await snap_page(win, snaps.msg_sent)
    await win.wait_for_function(
        f"(document.body.innerText.toLowerCase().match(/{FIRST_EXPECT}/g) || []).length >= 2",
        timeout=REPLY_TIMEOUT * 1000,
    )
    await asyncio.sleep(1)
    await snap_page(win, snaps.msg_reply)

    return _WorkspaceResult(
        chat_url=win.url,
        creation_id=creation_id,
        phase_durations=phase_durations,
        total_create_s=total_create_s,
    )


async def _send_followup_and_verify(
    win: Page,
    *,
    chat_url: str,
    prompt: str,
    expect_token: str,
    snap_sent: str,
    snap_reply: str,
    label: str,
) -> None:
    """Navigate `win` to `chat_url`, send `prompt`, wait for >=2 occurrences of `expect_token`.

    Proves the workspace's chat panel + agent backend stays responsive
    after the BrowserWindow has been navigated away and back. Caller
    picks a token that isn't already in the chat body so the count
    check actually proves a NEW reply landed (not carryover from
    earlier turns).
    """
    logger.info("[{}] navigating back to {}", label, chat_url)
    await win.goto(chat_url)
    inp = await win.wait_for_selector('textarea, [contenteditable="true"]', timeout=30_000)
    await inp.fill(prompt)
    await inp.press("Enter")
    with contextlib.suppress(Exception):
        await win.wait_for_function(
            f"document.body.innerText.includes({prompt!r})",
            timeout=10_000,
        )
    await snap_page(win, snap_sent)
    await win.wait_for_function(
        f"(document.body.innerText.toLowerCase().match(/{expect_token}/g) || []).length >= 2",
        timeout=REPLY_TIMEOUT * 1000,
    )
    await asyncio.sleep(1)
    await snap_page(win, snap_reply)
    logger.info("[{}] follow-up reply confirmed", label)


# --- main flow ---


def _kill_pgrep(pgrep_pattern: str, label: str, *, sudo: bool = False) -> None:
    """Kill processes whose argv matches ``pgrep_pattern``, by exact PID.

    Per CLAUDE.md, ``pkill -f`` / ``killall`` with broad patterns can hit
    unrelated processes (including this Claude Code session). Resolve PIDs
    with ``pgrep -f`` first, log each one, then ``kill`` by exact PID.
    ``sudo=True`` runs both pgrep and kill under sudo for root-owned processes.
    """
    prefix = ["sudo"] if sudo else []
    try:
        out = subprocess.run(
            [*prefix, "pgrep", "-lf", pgrep_pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, argv = line.partition(" ")
        if not pid_str.isdigit():
            continue
        logger.info("[runner-sweep] killing {} pid={} ({})", label, pid_str, argv)
        subprocess.run([*prefix, "kill", pid_str], check=False)


def pre_run_sweep() -> None:
    """Reset the self-hosted runner to a known-clean state before the run.

    Pairs with the teardown block: every state mutation this script makes
    is either reverted on teardown or wiped here on the next run's startup,
    so a crash mid-run never leaves residue that biases the following run.
    The sweep is idempotent and safe to call when there's nothing to clean.
    """
    logger.info("=== pre-run runner sweep ===")
    # mac-runner-reset.sh already kills every /Applications/Minds.app/...
    # process, wipes ~/.minds, and removes the .app entirely BEFORE we run.
    # So orphan mngr forward / mngr event / Minds.app processes can't exist
    # by the time we get here. The state below is what mac-runner-reset.sh
    # does NOT cover: host-side mocking residue and a stray caffeinate.
    revert_etc_hosts()
    _kill_pgrep("OPENSSL-LISTEN:443", "stale socat", sudo=True)
    _kill_pgrep("caffeinate -dimsu", "stale caffeinate")
    # Per-run /tmp state. cert + log files in /tmp/slack-mock/ are regenerated
    # by ensure_cert() / start_socat() / start_mock(); the screenshot dir is
    # recreated by the first snap_page().
    for stale in (
        SLACK_MOCK_STATE,
        SCREENSHOT_DIR,
        Path("/tmp/minds-electron.log"),
    ):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.exists():
            with contextlib.suppress(OSError):
                stale.unlink()
    # Stale latchkey slack creds. mac-runner-reset.sh wiped ~/.minds so the
    # latchkey dir is also gone -- this is belt-and-braces for the dev case
    # where someone runs the script outside CI without resetting first.
    with contextlib.suppress(Exception):
        latchkey_clear_slack()


async def amain() -> int:
    # Idempotent reset before any state mutation. Anything we leak (socat
    # holding :443, /etc/hosts entry, orphan Minds.app/mngr children,
    # latchkey creds, /tmp scratch dirs) is reverted here on the *next*
    # run's startup, so a mid-run crash never biases the following run.
    # The post-run teardown blocks below still handle the success path.
    pre_run_sweep()

    # 0. Keep the runner display awake + dismiss any active screensaver
    # for the whole run. -d=display awake, -i=idle sleep off, -m=disk
    # sleep off, -s=system sleep off, -u=declare user active (this is
    # what dismisses an already-running ScreenSaverEngine).
    caffeinate_proc = subprocess.Popen(
        ["caffeinate", "-dimsu"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _dismiss_screensaver()
    logger.info("caffeinate pid={} (display kept awake for run)", caffeinate_proc.pid)

    brew_curl = ensure_brew_curl()
    cert = ensure_cert()
    ca_bundle = ensure_combined_cert_bundle(cert)
    logger.info("brew curl: {}; ca_bundle: {}", brew_curl, ca_bundle)

    # 1. Launch minds.app ourselves with --remote-debugging-port so Playwright
    # can attach via CDP. Use a free port to avoid clashes.
    cdp_port = _free_port()
    env = {
        **os.environ,
        # ``LATCHKEY_CURL`` (read by latchkey's config.ts) pins the curl
        # binary to brew curl (OpenSSL) so checkApiCredentials never falls
        # back to /usr/bin/curl (SecureTransport, ignores CURL_CA_BUNDLE).
        # Otherwise services_info would report INVALID, grant() would
        # invoke auth_browser, and the request would resolve DENIED.
        "LATCHKEY_CURL": str(brew_curl),
        "PATH": f"{brew_curl.parent}:" + os.environ.get("PATH", ""),
        "CURL_CA_BUNDLE": str(ca_bundle),
    }
    env.pop("ELECTRON_RUN_AS_NODE", None)
    logger.info("launching {} --remote-debugging-port={}", MINDS_APP_PATH, cdp_port)
    launch_start_s = time.monotonic()
    minds_proc = subprocess.Popen(
        [str(MINDS_APP_PATH), f"--remote-debugging-port={cdp_port}"],
        env=env,
        stdout=open("/tmp/minds-electron.log", "w"),
        stderr=subprocess.STDOUT,
    )

    async with async_playwright() as pw:
        # Wait for CDP endpoint to be reachable
        cdp_url = await _wait_cdp(cdp_port)
        logger.info("attaching via CDP at {}", cdp_url)
        browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=60_000)
        # Single Electron context wraps all WebContentsViews as pages.
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Wait for first page (chrome shell or splash) to materialise.
        for _ in range(60):
            if ctx.pages:
                break
            await asyncio.sleep(0.5)
        if not ctx.pages:
            raise E2EFailure("no Electron windows after 30s")
        win = ctx.pages[0]
        await snap_page(win, "00-app-launched")

        # 2. Wait for backend, auth via OTC
        base = await asyncio.get_event_loop().run_in_executor(None, wait_backend_url)
        launch_to_ready_s = time.monotonic() - launch_start_s
        logger.info("backend up at {} (launch->ready={:.1f}s)", base, launch_to_ready_s)
        logger.info("[ci-metric] launch_to_ready_s={:.1f}", launch_to_ready_s)
        code = await asyncio.get_event_loop().run_in_executor(None, mint_one_time_code)
        # ``win`` here is ``ctx.pages[0]``, which can be the file:// splash
        # depending on Electron startup order. ``location.origin`` on the
        # splash returns ``"file://"``, so navigation to ``origin + "/..."``
        # would resolve to ``file:///...`` and fail. Use the known backend
        # base URL instead; ``win.goto`` happily redirects any window
        # (splash or http-served) to it.
        origin = base
        await win.goto(origin + "/authenticate?one_time_code=" + code)
        await snap_page(win, "01-after-auth")

        # 3. Navigate to home; UI now reflects authenticated state
        await win.goto(origin + "/")
        await snap_page(win, "02-home-after-auth")

        # The post-login "Help improve Minds" error-reporting consent screen
        # (Consent.jinja, shown while error_reporting_consent_given is False --
        # always here, since the runner's ~/.minds is wiped each run) sits on
        # the home page until answered. Dismiss it once via Continue; the
        # POST /consent + reload then proceeds home, so the create flow and the
        # later both-tiles home assertion see the home page, not the consent.
        with contextlib.suppress(Exception):
            consent_btn = await win.wait_for_selector("#consent-continue", timeout=8_000)
            if consent_btn is not None:
                await consent_btn.click()
                await win.wait_for_selector("#consent-continue", state="detached", timeout=15_000)
                logger.info("dismissed error-reporting consent screen")

        # 4-6. Create agent via UI click and drive to first message. Mirrors
        # what a user does (Configure panel, launch_mode/ai_provider/api_key
        # fields, host_name fill, submit, poll until DONE, navigate to chat,
        # send FIRST_PROMPT, wait for >=2 occurrences of FIRST_EXPECT in body).
        # See _create_workspace_and_first_message for the exact step list.
        ai_provider = os.environ.get("MINDS_AI_PROVIDER", "API_KEY").upper()
        if ai_provider not in ("API_KEY", "SUBSCRIPTION"):
            raise E2EFailure(f"MINDS_AI_PROVIDER={ai_provider!r} -- must be API_KEY or SUBSCRIPTION")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if ai_provider == "API_KEY" and not anthropic_key:
            raise E2EFailure(
                "MINDS_AI_PROVIDER=API_KEY but ANTHROPIC_API_KEY not set; "
                "either set ANTHROPIC_API_KEY or pass MINDS_AI_PROVIDER=SUBSCRIPTION."
            )

        all_timings: dict[str, Any] = {}

        w1_result: _WorkspaceResult | None = None
        if not SKIP_FIRST_MESSAGE:
            w1_result = await _create_workspace_and_first_message(
                ctx,
                win,
                origin=origin,
                host_name=HOST_NAME,
                ai_provider=ai_provider,
                anthropic_key=anthropic_key,
                snaps=_W1_SNAPS,
                label="w1",
            )
            all_timings["w1"] = {
                "host_name": HOST_NAME,
                "phase_durations_s": w1_result.phase_durations,
                "total_create_s": w1_result.total_create_s,
            }
            logger.info("[ci-metric] w1_create_s={:.1f}", w1_result.total_create_s)

            # Iter 15 (reload chat persists state): real users hit F5 / Cmd-R
            # in the chat tab. The chat must reattach to the agent's event
            # stream and re-render history -- losing the pong reply would
            # break the user's trust in the workspace as a persistent place.
            logger.info("=== iter 15: reload W1 chat, verify history persists ===")
            await win.reload(wait_until="domcontentloaded")
            await asyncio.sleep(2)
            body_after_reload = await win.evaluate("document.body.innerText")
            pong_count = body_after_reload.lower().count(FIRST_EXPECT.lower())
            if pong_count < 2:
                raise E2EFailure(
                    f"[reload] W1 chat lost history after reload: "
                    f"expected >=2 occurrences of {FIRST_EXPECT!r}, got {pong_count} "
                    f"(body len={len(body_after_reload)})"
                )
            await snap_page(win, "06b-w1-chat-after-reload")
            logger.info("[reload] PASS: W1 chat history survives reload ({} 'pong' hits)", pong_count)

        if not SKIP_SLACK_FLOW:
            # 7. Slack mock setup
            logger.info("=== slack flow ===")
            mock = start_mock()
            patch_etc_hosts()
            # Killed in finally via stop_socat().
            start_socat(cert)
            # Let socat bind.
            time.sleep(2)
            try:
                latchkey_set_slack()
                # 8. Send slack prompt
                inp = await win.wait_for_selector('textarea, [contenteditable="true"]', timeout=180_000)
                await inp.fill(SLACK_PROMPT)
                await inp.press("Enter")
                await snap_page(win, "07-slack-prompt-sent")

                # === Iter 10 Phase A: drive the FIRST permission request to DENY ===
                # Real users sometimes click Deny by accident or change their
                # mind. Drive the 3-stage state machine with decision="deny".
                logger.info("=== Phase A: drive permission request -> DENY ===")
                deny_stage = 0
                deny_clicked = {}
                deny_deadline = time.time() + DRIVE_SLACK_TIMEOUT
                while time.time() < deny_deadline:
                    chat_now = await find_chat_window(ctx)
                    if chat_now is not None:
                        win = chat_now
                    if deny_stage >= 3:
                        logger.info("[deny-phase] PASS: Deny click landed")
                        break
                    await _advance_approval(
                        ctx,
                        win,
                        deny_stage,
                        deny_clicked,
                        decision="deny",
                        snap_prefix_pair=(
                            "07a-deny-stage0",
                            "07b-deny-stage1",
                            "07c-deny-stage2-pre",
                            "07d-deny-stage2-post",
                        ),
                    )
                    deny_stage = deny_clicked.get("stage", deny_stage)
                    await asyncio.sleep(2)
                else:
                    await snap_page(win, "99-TIMEOUT-no-deny-click")
                    raise E2EFailure(
                        f"[deny-phase] Deny click did not land after {DRIVE_SLACK_TIMEOUT}s (stage={deny_stage})"
                    )

                # === Iter 10 Phase B: snapshot latchkey pending requests state ===
                # The latchkey gateway extension stores each pending request
                # as a single JSON file. Right after Deny, the previous
                # request's file should be gone. Read directly from disk
                # (not via the requests-panel UI, which is a stale render).
                files_post_deny = _list_permission_request_files()
                logger.info(
                    "[deny-phase] latchkey pending files post-deny: {} -- {}",
                    len(files_post_deny),
                    [f.name for f in files_post_deny],
                )

                # === Iter 10 Phase C: kick the agent to re-request ===
                # The latchkey skill (FCT) says to re-POST /permission-requests
                # when a previous request was denied. The kick gives Claude
                # the explicit user-side signal; without a follow-up the
                # agent's slack tool sits on its polling loop until the
                # request times out instead of self-triggering a re-ask.
                logger.info("=== Phase C: kick agent to re-request after deny ===")
                target = (await find_chat_window(ctx)) or win
                retry_msg = (
                    "I just denied that request by mistake -- please send a fresh "
                    "Slack permission request via the latchkey skill (POST to "
                    "/permission-requests). Then wait for me to approve it."
                )
                retry_inp = await target.wait_for_selector('textarea, [contenteditable="true"]', timeout=10_000)
                await retry_inp.fill(retry_msg)
                await retry_inp.press("Enter")
                await snap_page(target, "07e-retry-after-deny-sent")

                # === Iter 10 Phase D: wait for Claude to submit a NEW request ===
                # Poll the latchkey dir directly. If a new file appears,
                # Claude re-submitted -- proceed to the APPROVE phase. If
                # nothing appears within 120s, surface that as the failure
                # (instead of timing out in the approve loop with a misleading
                # "no canned body" message).
                logger.info("=== Phase D: wait for new pending file ===")
                retry_deadline = time.time() + 120
                new_file: Path | None = None
                while time.time() < retry_deadline:
                    files_now = _list_permission_request_files()
                    new_names = {f.name for f in files_now} - {f.name for f in files_post_deny}
                    if new_names:
                        for f in files_now:
                            if f.name in new_names:
                                new_file = f
                                break
                        logger.info("[retry-phase] new pending file appeared: {}", new_file.name if new_file else "?")
                        await snap_page(target, "07f-new-request-appeared")
                        break
                    await asyncio.sleep(3)
                else:
                    await snap_page(target, "99-TIMEOUT-no-retry-request")
                    chat_body = await target.evaluate("document.body.innerText")
                    raise E2EFailure(
                        f"[retry-phase] no new permission request file in "
                        f"{PERMISSION_REQUESTS_DIR} after 120s. Claude did not re-submit "
                        f"after deny. Chat tail: ...{chat_body[-400:]!r}"
                    )

                # === Iter 10 Phase E: drive APPROVE on the new request ===
                logger.info("=== Phase E: APPROVE the re-submitted request ===")
                approval_stage = 0
                deadline = time.time() + DRIVE_SLACK_TIMEOUT
                clicked_at = {}
                approved_request_urls: set[str] = set()
                # Monotonic timestamp of the last kick.
                last_kick_at = 0.0
                first_approve_at = 0.0
                approve_snaps = (
                    "07g-approve-stage0",
                    "07h-approve-stage1",
                    "07i-approve-stage2-pre",
                    "07j-approve-stage2-post",
                )
                while time.time() < deadline:
                    chat_now = await find_chat_window(ctx)
                    if chat_now is not None:
                        win = chat_now
                    # Scroll the transcript to the live tail before reading it, as a
                    # user reading the latest reply would. The chat virtualizes
                    # off-screen rows, so a reply rendered below the fold (e.g. the
                    # view was not pinned to the bottom when it arrived) is absent
                    # from document.body.innerText until the viewport is at the tail.
                    with contextlib.suppress(Exception):
                        await win.evaluate(
                            "() => { for (const el of document.querySelectorAll('.app-content')) "
                            "{ el.scrollTop = el.scrollHeight; } }"
                        )
                    # Check for canned body in chat (PASS).
                    body = await win.evaluate("document.body.innerText")
                    if CANNED_BODY.lower() in body.lower() and approval_stage >= 3:
                        logger.info("PASS: canned body in reply")
                        await snap_page(win, "08-PASS-canned-body")
                        break

                    if approval_stage < 3:
                        await _advance_approval(
                            ctx,
                            win,
                            approval_stage,
                            clicked_at,
                            decision="approve",
                            snap_prefix_pair=approve_snaps,
                        )
                        approval_stage = clicked_at.get("stage", approval_stage)
                        if approval_stage >= 3 and first_approve_at == 0.0:
                            first_approve_at = time.monotonic()

                    # After the first approval, the latchkey gateway often
                    # re-gates the next slack API call separately -- the
                    # agent submits a NEW /requests/<id>. Auto-approve any
                    # follow-up requests so Claude can complete its retry.
                    if approval_stage >= 3:
                        for p in all_pages(ctx):
                            with contextlib.suppress(Exception):
                                if "/requests/" not in p.url or p.url in approved_request_urls:
                                    continue
                                btn = p.locator('button:has-text("Approve")').first
                                if await btn.count() > 0 and await btn.is_visible():
                                    logger.info("auto-approving follow-up request at {}", p.url)
                                    await btn.click()
                                    approved_request_urls.add(p.url)

                        # Periodic kick: Claude often sits parked after a
                        # permission round-trip. Every KICK_INTERVAL secs
                        # send a short "approved, retry" prompt into the
                        # chat to nudge it. The first kick fires KICK_DELAY
                        # secs after the first Approve so it lands AFTER
                        # the gateway's permission grant is visible.
                        KICK_DELAY = 8
                        KICK_INTERVAL = 30
                        now = time.monotonic()
                        if now - first_approve_at >= KICK_DELAY and now - last_kick_at >= KICK_INTERVAL:
                            target = await find_chat_window(ctx)
                            if target is not None:
                                kick_msg = (
                                    "Slack permission is now granted -- please retry the "
                                    "read-only Slack read and then quote the message you read "
                                    "back to me here in chat."
                                )
                                try:
                                    inp = await target.wait_for_selector(
                                        'textarea, [contenteditable="true"]', timeout=5_000
                                    )
                                    await inp.fill(kick_msg)
                                    await inp.press("Enter")
                                    logger.info("sent post-approval kick (target={})", target.url)
                                    last_kick_at = now
                                except Exception as exc:
                                    logger.warning("kick attempt failed on chat {}: {}", target.url, exc)

                    await asyncio.sleep(2)
                else:
                    await snap_page(win, "99-TIMEOUT-no-canned-body")
                    # Dump every page's URL + first 200 chars of body so we
                    # can tell whether the chat panel was alive somewhere.
                    for p in all_pages(ctx):
                        with contextlib.suppress(Exception):
                            preview = (await p.evaluate("document.body.innerText"))[:200].replace("\n", " ")
                            logger.error("  page url={} body=...{!r}", p.url, preview)
                    raise E2EFailure(
                        f"canned body not in chat after {DRIVE_SLACK_TIMEOUT}s (approval_stage={approval_stage})"
                    )
            finally:
                logger.info("=== slack teardown ===")
                with contextlib.suppress(Exception):
                    latchkey_clear_slack()
                revert_etc_hosts()
                stop_socat()
                mock.shutdown()

        # 10-14. Second workspace + cross-workspace follow-up. Drives a
        # FRESH host (HOST_NAME_2) through create + first-message on the
        # same minds.app session, then navigates back to W1's chat URL and
        # to W2's chat URL in turn, sending a unique-token follow-up to
        # each. Proves: state isolation between workspaces, both agents
        # survive cross-workspace navigation, mngr_forward + latchkey
        # gateway don't collide on shared host-side state.
        if WORKSPACE_COUNT >= 2 and not SKIP_FIRST_MESSAGE:
            if w1_result is None:
                raise E2EFailure("WORKSPACE_COUNT>=2 but W1 was skipped; cross-workspace check is meaningless")
            w1_chat_url = w1_result.chat_url
            logger.info("=== workspace 2 create + first message (host={}) ===", HOST_NAME_2)
            w2_result = await _create_workspace_and_first_message(
                ctx,
                win,
                origin=origin,
                host_name=HOST_NAME_2,
                ai_provider=ai_provider,
                anthropic_key=anthropic_key,
                snaps=_W2_SNAPS,
                label="w2",
            )
            all_timings["w2"] = {
                "host_name": HOST_NAME_2,
                "phase_durations_s": w2_result.phase_durations,
                "total_create_s": w2_result.total_create_s,
            }
            logger.info("[ci-metric] w2_create_s={:.1f}", w2_result.total_create_s)
            w2_chat_url = w2_result.chat_url

            logger.info("=== cross-workspace: ping W1 chat ({}) ===", w1_chat_url)
            await _send_followup_and_verify(
                win,
                chat_url=w1_chat_url,
                prompt=FOLLOWUP_W1_PROMPT,
                expect_token=FOLLOWUP_W1_EXPECT,
                snap_sent="13-w1-followup-sent",
                snap_reply="14-w1-followup-reply",
                label="w1-followup",
            )
            logger.info("=== cross-workspace: ping W2 chat ({}) ===", w2_chat_url)
            await _send_followup_and_verify(
                win,
                chat_url=w2_chat_url,
                prompt=FOLLOWUP_W2_PROMPT,
                expect_token=FOLLOWUP_W2_EXPECT,
                snap_sent="15-w2-followup-sent",
                snap_reply="16-w2-followup-reply",
                label="w2-followup",
            )

            # Navigate back to the landing page and assert BOTH workspace
            # tiles render. The chat-URL-direct checks above bypass discovery,
            # so they would still pass even if a regression hid one tile from
            # the landing page; this catches that.
            logger.info("=== home page: verify both workspace tiles render ===")
            await win.goto(origin + "/")
            await win.wait_for_function(
                f"document.body.innerText.includes({HOST_NAME!r}) && "
                f"document.body.innerText.includes({HOST_NAME_2!r})",
                timeout=30_000,
            )
            await snap_page(win, "17-home-both-tiles")
            logger.info("home page shows both tiles: {} and {}", HOST_NAME, HOST_NAME_2)

            # Iter 13 (click tile to navigate): real users open chat by
            # clicking the workspace tile, not by typing the chat URL. The
            # tile's onclick handler routes through /goto/<agent_id>/ which
            # mngr_forward translates into the agent-<hex>.localhost host.
            # Click W1's tile, wait for the URL to carry W1's SPECIFIC
            # agent_id, snap, then navigate back to home so the destroy
            # flow below proceeds from a known starting page.
            logger.info("=== iter 13: click W1 tile to navigate to chat ===")
            w1_hex_match = re.search(r"//(agent-[a-f0-9]+)\.localhost", w1_result.chat_url)
            if not w1_hex_match:
                raise E2EFailure(f"[tile-click] could not extract W1 agent_id from {w1_result.chat_url!r}")
            w1_agent_host = w1_hex_match.group(1)
            # Exact-text match: HOST_NAME (e.g. "e2e172219") is a substring
            # of HOST_NAME_2 (e.g. "e2e172219-b"), so a regex/substring
            # selector would match W2's tile first. Quoted text= is
            # exact-string (case-insensitive) and picks the right tile.
            w1_tile_locator = win.locator(f'text="{HOST_NAME}"').first
            await w1_tile_locator.wait_for(state="visible", timeout=10_000)
            await w1_tile_locator.click()
            w1_url_re = re.compile(re.escape(w1_agent_host) + r"\.localhost")
            await win.wait_for_url(w1_url_re, timeout=30_000)
            await snap_page(win, "17b-w1-via-tile-click")
            if w1_agent_host not in win.url:
                raise E2EFailure(f"[tile-click] expected W1 agent host {w1_agent_host!r} in URL, got {win.url!r}")
            logger.info("[tile-click] PASS: W1 tile click landed at {}", win.url)
            await win.goto(origin + "/")

            # Iter 16 (/accounts smoke): users click the account icon in
            # the header to manage accounts. Smoke-test that the page
            # renders without a 500/404. We don't drive any signin here.
            logger.info("=== iter 16: /accounts smoke ===")
            await win.goto(origin + "/accounts")
            await win.wait_for_load_state("domcontentloaded", timeout=10_000)
            await snap_page(win, "17c-accounts-page")
            accounts_body = (await win.evaluate("document.body.innerText")).lower()
            if "account" not in accounts_body:
                raise E2EFailure(f"[accounts] /accounts body looks empty: {accounts_body[:200]!r}")
            logger.info("[accounts] PASS: /accounts renders")

            # Iter 17 (W1 settings page renders): all earlier settings-page
            # coverage is on W2 (which we then destroy). Visit W1's settings
            # page read-only and assert the danger-zone button renders.
            # Catches regressions specific to W1's settings rendering.
            logger.info("=== iter 17: W1 settings page renders ===")
            await win.goto(origin + f"/workspace/{w1_agent_host}/settings")
            await win.wait_for_selector("#destroy-btn", state="visible", timeout=15_000)
            await snap_page(win, "17d-w1-settings-page")
            w1_settings_body = await win.evaluate("document.body.innerText")
            if HOST_NAME not in w1_settings_body:
                raise E2EFailure(
                    f"[w1-settings] W1 settings page missing HOST_NAME {HOST_NAME!r}; "
                    f"body head={w1_settings_body[:200]!r}"
                )
            logger.info("[w1-settings] PASS: W1 settings page renders with {} present", HOST_NAME)
            await win.goto(origin + "/")

            # 18-22. Destroy W2 via the UI (gear icon -> WorkspaceSettings ->
            # destroy-btn -> destroy-confirm-btn), poll status to completion,
            # then assert (a) the landing page drops W2's tile while keeping
            # W1's, and (b) W1 stays responsive afterward.
            # Exercises the destroy lifecycle end-to-end as a real user
            # would, and proves destroy of one workspace doesn't cascade
            # into another.
            logger.info("=== destroy W2 via UI (gear -> settings -> Destroy) ===")
            m = re.search(r"//(agent-[a-f0-9]+)\.localhost", w2_chat_url)
            if not m:
                raise E2EFailure(f"[w2-destroy] could not extract agent_id from {w2_chat_url!r}")
            w2_agent_id = m.group(1)
            logger.info("[w2-destroy] agent_id={}", w2_agent_id)

            # Navigate to W2's settings page directly. The home tile's gear
            # icon links to the same URL via window.location -- skipping the
            # tile click avoids racing the landing page's reactive re-renders
            # while still exercising the same destroy code path.
            await win.goto(origin + f"/workspace/{w2_agent_id}/settings")
            await win.wait_for_selector("#destroy-btn", state="visible", timeout=30_000)
            await snap_page(win, "18-w2-settings-page")

            # Iter 14 (Back to projects link): real users navigate back from
            # settings via the top-level link without ever opening the
            # destroy modal. The template renders it as ``&larr; Back to
            # projects`` (a left-arrow glyph + the phrase), so an exact
            # text= match for the phrase alone won't find it. Match by
            # href= on the wrapping Link instead.
            await win.click('a[href="/"]:has-text("Back to projects")')
            await win.wait_for_url(origin + "/", timeout=10_000)
            await snap_page(win, "18a-back-to-projects-from-settings")
            await win.goto(origin + f"/workspace/{w2_agent_id}/settings")
            await win.wait_for_selector("#destroy-btn", state="visible", timeout=30_000)

            # Iter 12 (Cancel modal): real users click Destroy by accident or
            # change their mind. Verify the Cancel button dismisses the modal
            # without firing any /api/destroy-agent call, leaving W2 alive.
            await win.click("#destroy-btn")
            await win.wait_for_selector("#destroy-confirm-btn", state="visible", timeout=5_000)
            await snap_page(win, "18b-w2-destroy-modal-opened")
            await win.click("#destroy-cancel-btn")
            await win.wait_for_selector("#destroy-confirm-btn", state="hidden", timeout=5_000)
            await snap_page(win, "18b2-w2-cancelled-modal-dismissed")
            # The settings page should still render the destroy button
            # (proof we didn't navigate away and the page is responsive).
            await win.wait_for_selector("#destroy-btn", state="visible", timeout=5_000)

            # Now do the real destroy: reopen modal, click Confirm.
            await win.click("#destroy-btn")
            await win.wait_for_selector("#destroy-confirm-btn", state="visible", timeout=5_000)
            await snap_page(win, "18b3-w2-destroy-modal-reopened")
            await win.click("#destroy-confirm-btn")
            # The confirm handler POSTs /api/destroy-agent then redirects to
            # /; wait for the navigation, then snap the in-flight state.
            await win.wait_for_url(origin + "/", timeout=30_000)
            await snap_page(win, "18c-w2-destroy-initiated")

            # Poll /api/destroying/<id>/status until the host is actually gone
            # (status 'done', or 404 once the record is cleaned up). Lima VM
            # stop+delete typically takes 30-90s; 4-min budget gives comfortable
            # headroom.
            #
            # 'failed' here is NOT terminal: the status is derived fresh per poll
            # as pid-dead + is_host_still_active (see destroying.read_destroying).
            # The detached `mngr destroy` subprocess exits before the lima VM
            # finishes dropping out of discovery, so on a slow runner the status
            # reads 'failed' transiently (subprocess gone, host not yet) before
            # flipping to 'done' once the host is actually torn down. Treat it as
            # "not done yet" and keep polling; a host that never tears down stays
            # 'failed' until the deadline, which still surfaces a real
            # silent-orphan teardown as a failure.
            destroy_deadline = time.time() + 240
            last_body: dict[str, Any] = {}
            while time.time() < destroy_deadline:
                resp = await win.evaluate(
                    """async (aid) => {
                        const r = await fetch('/api/destroying/' + aid + '/status');
                        return {status: r.status, body: await r.text()};
                    }""",
                    w2_agent_id,
                )
                if resp["status"] == 404:
                    logger.info("[w2-destroy] DONE (record cleaned up)")
                    break
                with contextlib.suppress(Exception):
                    last_body = json.loads(resp["body"])
                state = last_body.get("status", "")
                if state == "done":
                    logger.info("[w2-destroy] DONE")
                    break
                await asyncio.sleep(3)
            else:
                raise E2EFailure(f"[w2-destroy] host still active after 240s (last={last_body})")
            await snap_page(win, "19-w2-destroy-done")

            logger.info("=== home page after W2 destroyed ===")
            await win.goto(origin + "/")
            await win.wait_for_function(
                f"document.body.innerText.includes({HOST_NAME!r}) && "
                f"!document.body.innerText.includes({HOST_NAME_2!r})",
                timeout=60_000,
            )
            await snap_page(win, "20-home-only-w1")
            logger.info("home page after destroy shows only W1: {}", HOST_NAME)

            logger.info("=== W1 still alive after W2 destroyed ===")
            await _send_followup_and_verify(
                win,
                chat_url=w1_chat_url,
                prompt="Reply with exactly the four characters: bink",
                expect_token="bink",
                snap_sent="21-w1-after-w2-destroy-sent",
                snap_reply="22-w1-after-w2-destroy-reply",
                label="w1-after-w2-destroy",
            )

            # 23. Cross-check with mngr CLI from the host: confirms W1 is in
            # mngr's canonical agent set and W2 is gone. A regression where
            # /api/destroy-agent returns 200 but mngr's host_dir still
            # records the agent would slip past the UI-driven home-page
            # tile check above (which scrapes the same discovery layer the
            # destroy handler does).
            logger.info("=== mngr CLI list: cross-check W1 present, W2 removed ===")
            bundled_mngr = MINDS_HOME / ".venv" / "bin" / "mngr"
            if bundled_mngr.exists():
                # mngr's lima provider shells out to ``limactl list --json``
                # without going through the bundled-binary env vars (those
                # are minds-side ergonomics). The packaged binary's PATH
                # doesn't include /Applications/Minds.app/.../Resources/lima/bin,
                # so we prepend it here -- otherwise the discovery hits
                # "No such file or directory: 'limactl'" and the agents
                # array comes back empty even though the host_dir has W1.
                minds_resources = MINDS_APP_PATH.parent.parent / "Resources"
                bundled_lima_bin = minds_resources / "lima" / "bin"
                mngr_env = {
                    **os.environ,
                    "MNGR_HOST_DIR": str(MINDS_HOME / "mngr"),
                    "PATH": f"{bundled_lima_bin}:{os.environ.get('PATH', '')}",
                }
                # ``--on-error continue`` puts each provider's discovery error
                # into the JSON payload's ``errors`` array. mngr STILL exits 1
                # when any provider failed (per error_handling.md spec) -- on
                # the CI runner this is normal because the modal provider has
                # no token -- but the ``agents`` array still lists every agent
                # discovered by providers that DID work, which is what we
                # actually check. So: parse stdout regardless of exit code;
                # only fail if the JSON itself is unusable.
                # Run from $HOME, exactly as minds.app spawns mngr (see
                # forward_cli.py and laptop_agent_types_seed.py). mngr's project
                # config is discovered by walking up from cwd to a git worktree
                # root and reading `<root>/.mngr/settings.toml`. Without this the
                # subprocess inherits the e2e's cwd -- the mngr monorepo checkout
                # -- and loads the repo's own `[providers.aws]` image-build block,
                # which the bundled subset-mngr (no aws plugin) rejects with
                # "references unknown backend", aborting before any agent lists.
                # $HOME is not a git worktree, so no project layer leaks in; the
                # cross-check sees only the minds host profile, like production.
                cli_result = subprocess.run(
                    [str(bundled_mngr), "list", "--format", "json", "--quiet", "--on-error", "continue"],
                    capture_output=True,
                    text=True,
                    env=mngr_env,
                    cwd=str(Path.home()),
                    timeout=30,
                )
                (SCREENSHOT_DIR / "mngr-list-output.json").write_text(cli_result.stdout or "")
                (SCREENSHOT_DIR / "mngr-list-stderr.txt").write_text(cli_result.stderr or "")
                try:
                    listing = json.loads(cli_result.stdout or "{}")
                except json.JSONDecodeError as exc:
                    raise E2EFailure(
                        f"[mngr-list] non-JSON output (returncode={cli_result.returncode}): "
                        f"{exc}\nstdout={cli_result.stdout[:500]!r}\nstderr={cli_result.stderr[:500]!r}"
                    ) from exc
                agents = listing.get("agents", []) if isinstance(listing, dict) else []
                host_names = {a.get("host", {}).get("name", "") for a in agents if isinstance(a, dict)}
                logger.info("[mngr-list] {} agents; hosts: {}", len(agents), host_names)
                if HOST_NAME not in host_names:
                    raise E2EFailure(f"[mngr-list] W1 ({HOST_NAME!r}) absent from {host_names}")
                # Note: we do NOT assert HOST_NAME_2 absence. mngr's destroy
                # lifecycle keeps a metadata record for ``destroyed_host_
                # persisted_seconds`` after destroy completes (so historical
                # state survives) -- so W2's agent stays in the data.json /
                # mngr list for a grace period even though minds.app's
                # discovery has already dropped its landing-page tile.
                # Iter 5's home-page screenshot 20 is the canonical proof
                # that destroy reached the user-visible state; this CLI
                # check just confirms W1 is still in mngr's canonical set.
                logger.info("[mngr-list] PASS: W1 present in mngr canonical state")
            else:
                logger.warning("[mngr-list] {} not found; skipping CLI cross-check", bundled_mngr)

            # 24. Duplicate-name conflict: POST /api/create-agent with
            # HOST_NAME (still owned by W1) should return 409 with an
            # "already exists" message. Proves the duplicate-name guard
            # in _handle_create_agent_api (added on this branch) works,
            # and that the canonical name set is correctly populated by
            # backend_resolver.list_known_workspace_ids().
            logger.info("=== conflict 409: re-create with HOST_NAME already taken ===")
            # win is currently on W1's chat URL (agent-<hex>.localhost,
            # served by mngr_forward to the in-VM system_interface). The
            # /api/create-agent endpoint is on the MINDS backend (origin),
            # so the fetch needs to land there. Easiest: navigate win to
            # the minds origin first so the relative fetch resolves there.
            await win.goto(origin + "/")
            conflict_resp = await win.evaluate(
                """async (host_name) => {
                    const r = await fetch('/api/create-agent', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            host_name,
                            git_url: 'https://example.com/dummy.git',
                        }),
                    });
                    return {status: r.status, body: await r.text()};
                }""",
                HOST_NAME,
            )
            if conflict_resp["status"] != 409:
                raise E2EFailure(
                    f"[conflict-409] expected 409 for duplicate name {HOST_NAME!r}, "
                    f"got {conflict_resp['status']}: {conflict_resp['body']!r}"
                )
            if "already exists" not in conflict_resp["body"]:
                raise E2EFailure(f"[conflict-409] 409 body missing 'already exists' text: {conflict_resp['body']!r}")
            logger.info("[conflict-409] PASS: duplicate-name guard returned 409")

        # Iter 9 (quit + relaunch): a user closes the Mac app, opens it
        # again, and expects W1's chat to still work. Verifies session +
        # mngr data survive the restart, the Electron shutdown chain
        # leaves no orphans (port 8421 / mngr forward / latchkey gateway),
        # and the new mngr_forward refreshes the subdomain auth cookie
        # via /goto/<agent_id>/ so the W1 chat URL doesn't redirect to
        # localhost on stale-cookie verification.
        if w1_result is not None:
            logger.info("=== iter 9: quit + relaunch ===")
            events_offset_pre_relaunch = EVENTS_LOG.stat().st_size if EVENTS_LOG.exists() else 0

            await browser.close()
            minds_proc.terminate()
            # The new "quitting" takeover (Electron app shows a quitting page
            # and animates through backend teardown before exit) makes a
            # graceful SIGTERM exit take longer than the previous splash-only
            # path; 30s covers it. If the previous instance is still alive
            # when we launch the new one, macOS's single-instance lock turns
            # the second launch into a no-op (the new process immediately
            # fires before-quit and exits without ever creating a window),
            # leaving the e2e timing out at "no Electron windows 30s after
            # relaunch". SIGKILL the holdout so the lock releases.
            with contextlib.suppress(Exception):
                minds_proc.wait(timeout=30)
            if minds_proc.poll() is None:
                logger.info("[relaunch] minds_proc still alive after SIGTERM; SIGKILL")
                with contextlib.suppress(Exception):
                    minds_proc.kill()
                    minds_proc.wait(timeout=10)
            # Sweep orphan mngr forward / latchkey gateway processes that
            # the Electron shutdown chain sometimes leaves alive holding
            # :8421. Without this, the new launch can land on a stale
            # forwarder that lacks the new signing key entirely.
            _kill_pgrep("mngr forward", "orphan mngr forward")
            _kill_pgrep("mngr latchkey forward", "orphan mngr latchkey forward")
            _kill_pgrep("latchkey gateway", "orphan latchkey gateway")
            _kill_pgrep("mngr observe", "orphan mngr observe")
            await asyncio.sleep(10)

            cdp_port2 = _free_port()
            logger.info("relaunching {} --remote-debugging-port={}", MINDS_APP_PATH, cdp_port2)
            minds_proc = subprocess.Popen(
                [str(MINDS_APP_PATH), f"--remote-debugging-port={cdp_port2}"],
                env=env,
                stdout=open("/tmp/minds-electron-relaunch.log", "w"),
                stderr=subprocess.STDOUT,
            )

            cdp_url2 = await _wait_cdp(cdp_port2)
            browser = await pw.chromium.connect_over_cdp(cdp_url2, timeout=60_000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            for _ in range(60):
                if ctx.pages:
                    break
                await asyncio.sleep(0.5)
            if not ctx.pages:
                raise E2EFailure("[relaunch] no Electron windows 30s after relaunch")
            win = ctx.pages[0]
            await snap_page(win, "23-after-relaunch")

            base2 = await asyncio.get_event_loop().run_in_executor(None, wait_backend_url, events_offset_pre_relaunch)
            logger.info("[relaunch] new backend up at {}", base2)
            code2 = await asyncio.get_event_loop().run_in_executor(None, mint_one_time_code)
            origin = base2
            await win.goto(origin + "/authenticate?one_time_code=" + code2)
            await snap_page(win, "24-after-relaunch-auth")

            await win.goto(origin + "/")
            await snap_page(win, "25-home-after-relaunch")
            home_html = await win.content()
            if HOST_NAME not in home_html:
                raise E2EFailure(f"[relaunch] home page after relaunch missing W1 tile {HOST_NAME!r}")

            # Send the unique-token follow-up. With the
            # mngr_forward fix in place, a stale subdomain cookie on
            # the chat URL self-heals via the /goto/<agent_id>/ bridge
            # the bare-origin handler mints, so this navigation should
            # succeed on the first try.
            await _send_followup_and_verify(
                win,
                chat_url=w1_result.chat_url,
                prompt="Reply with exactly the four characters: bump",
                expect_token="bump",
                snap_sent="26-w1-after-relaunch-sent",
                snap_reply="27-w1-after-relaunch-reply",
                label="w1-after-relaunch",
            )
            logger.info("[relaunch] PASS: W1 chat survived quit + relaunch")

        # Persist combined per-workspace timings as one artifact JSON
        # rather than two -- one file is easier to embed in the run
        # summary and to diff across runs.
        if all_timings:
            timings_artifact = SCREENSHOT_DIR / "launch-to-msg-timings.json"
            with contextlib.suppress(Exception):
                timings_artifact.write_text(json.dumps(all_timings, indent=2))

        await browser.close()
        minds_proc.terminate()
        with contextlib.suppress(Exception):
            minds_proc.wait(timeout=10)
    caffeinate_proc.terminate()
    with contextlib.suppress(Exception):
        caffeinate_proc.wait(timeout=5)
    return 0


def _list_permission_request_files() -> list[Path]:
    """Return current files in the latchkey gateway's pending-request dir.

    Iter 10 reads this directly to verify whether Claude re-submitted a
    new permission request after a deny (rather than infer from the
    requests-panel UI, which doesn't auto-refresh between renders).
    Each pending request lives at a single .json file in this dir; an
    empty list means no requests are pending.
    """
    if not PERMISSION_REQUESTS_DIR.exists():
        return []
    return sorted(p for p in PERMISSION_REQUESTS_DIR.iterdir() if p.suffix == ".json")


async def _advance_approval(
    ctx: BrowserContext,
    win: Page,
    stage: int,
    state: dict[str, int],
    *,
    decision: str = "approve",
    snap_prefix_pair: tuple[str, str, str, str] = (
        "07a-stage0-pre-click-requests",
        "07b-stage1-pre-click-entry",
        "07c-stage2-pre-click-approve",
        "07d-stage2-post-approve",
    ),
) -> None:
    """One step of the 3-stage permission click. Updates state['stage'].

    ``decision`` selects the button at stage 2: ``"approve"`` (default)
    or ``"deny"``. ``snap_prefix_pair`` is a 4-tuple of snap names for
    stages 0-pre, 1-pre, 2-pre, 2-post; iter 10 swaps it twice so the
    deny round and the approve round don't collide on names.
    """
    if decision not in ("approve", "deny"):
        raise E2EFailure(f"_advance_approval: decision must be approve|deny, got {decision!r}")
    snap_stage0, snap_stage1, snap_stage2_pre, snap_stage2_post = snap_prefix_pair
    # The "permission panel" was refactored into an inbox modal whose
    # WebContentsView serves /inbox; the master/detail split lives in
    # one page (left list = .inbox-card, right detail loads via
    # /inbox/detail/<id> fragment and contains the Approve/Deny form).
    # Stage 0 waits for /inbox (it auto-opens on new pending requests by
    # default, see MindsConfig.get_auto_open_requests_panel). Stage 1
    # clicks the inbox card for the slack request to load the detail
    # fragment. Stage 2 clicks Approve / Deny within the same page.
    if stage == 0:
        # Check if the inbox modal already auto-opened.
        panel = None
        for w in all_pages(ctx):
            with contextlib.suppress(Exception):
                if "/inbox" in w.url:
                    panel = w
                    break
        if panel is not None:
            logger.info("inbox modal auto-opened; advancing to stage 1")
            state["stage"] = 1
            await snap_page(panel, snap_stage0)
            return
        # Wait for the agent to emit its request signal first. Case-fold
        # because Claude rephrases the message each run (eg "Waiting"
        # vs "awaiting" vs "wait for").
        body = (await win.evaluate("document.body.innerText")).lower()
        # "approval" catches "Waiting for your approval", "awaiting", etc.
        if not any(
            s in body
            for s in (
                "permission request",
                "requested read",
                "approval",
                "approve",
            )
        ):
            # Not ready yet.
            return
        # Auto-open should fire on the SSE-pushed pending-set update; if
        # it hasn't fired after a couple of polls, hit /inbox/toggle on
        # the chrome titlebar as a fallback (the inbox icon's aria-label
        # is "Inbox"; the old `button[title="Requests"]` is gone).
        for w in all_pages(ctx):
            try:
                btn = w.locator('button[aria-label="Inbox"], button[title="Inbox"]')
                if await btn.count() > 0 and await btn.first.is_visible():
                    logger.info("clicking Inbox titlebar trigger")
                    await snap_page(w, snap_stage0)
                    await btn.first.click()
                    state["stage"] = 1
                    return
            except Exception:
                pass

    # Stage 1: click the slack entry in the inbox left list.
    elif stage == 1:
        panel = None
        for w in all_pages(ctx):
            with contextlib.suppress(Exception):
                if "/inbox" in w.url:
                    panel = w
                    break
        if panel is None:
            return
        # Prefer the slack-named .inbox-card; fall back to the first
        # selectable card if there's only one pending request.
        for sel in (
            '.inbox-card:has-text("slack")',
            '.inbox-card:has-text("Slack")',
            ".inbox-card",
        ):
            try:
                loc = panel.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    logger.info("clicking inbox card via {!r}", sel)
                    await snap_page(panel, snap_stage1)
                    await loc.click()
                    state["stage"] = 2
                    return
            except Exception:
                pass

    # Stage 2: click Approve or Deny in the inbox detail pane (same /inbox page).
    elif stage == 2:
        if decision == "approve":
            button_selectors = (
                "#permissions-approve-btn:not([disabled])",
                'button:has-text("Approve"):not([disabled])',
            )
        else:
            button_selectors = ('button:has-text("Deny")',)
        for w in all_pages(ctx):
            try:
                if "/inbox" not in w.url:
                    continue
                btn = None
                for bsel in button_selectors:
                    candidate = w.locator(bsel).first
                    if await candidate.count() > 0 and await candidate.is_visible():
                        btn = candidate
                        break
                if btn is None:
                    continue
                logger.info("clicking {} button on inbox detail", decision)
                await snap_page(w, snap_stage2_pre)
                await btn.click()
                state["stage"] = 3
                # Snap a beat later. For approve the inbox shows the
                # browser-launch / success notice; for deny the inbox
                # closes back to the chat window.
                await asyncio.sleep(2)
                snap_target = w
                if decision == "deny":
                    chat_after = await find_chat_window(ctx)
                    if chat_after is not None:
                        snap_target = chat_after
                with contextlib.suppress(Exception):
                    await snap_page(snap_target, snap_stage2_post)
                if decision == "approve":
                    # Surface latchkey-side authorisation failures
                    # immediately rather than waiting DRIVE_SLACK_TIMEOUT
                    # seconds for a timeout. The post-approve page renders
                    # the error banner verbatim; parse the visible text
                    # and raise so CI fails on the actual signal, not a
                    # timeout that happens to coincide.
                    with contextlib.suppress(Exception):
                        body_text = await w.evaluate("document.body.innerText")
                        if "Authorization failed" in body_text or "No browser configured" in body_text:
                            raise E2EFailure(
                                f"{snap_stage2_post} shows authorization failure: "
                                + body_text.replace("\n", " | ")[:400]
                            )
                # Kicks are sent from the main poll loop after a
                # KICK_DELAY settle period; see slack-flow loop.
                return
            except Exception:
                pass


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>[{level: <7}]</level> {message}")
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.opt(exception=True).error("FATAL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
