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
  1. Launch minds.app via Playwright Electron
  2. UI auth via /authenticate?one_time_code=... (minted on disk)
  3. Click Create, fill form, wait for agent DONE
  4. Send first message ("pong"), wait for reply
  5. (if slack flow enabled) stand up local slack mock HTTPS server on
     :443 via sudo socat TLS-terminator, patch /etc/hosts, pre-seed
     latchkey slack credential, send slack prompt
  6. Click Requests -> entry -> Approve, kick agent to retry
  7. Verify canned MESSAGE_BODY appears in chat
  8. Teardown: destroy agent, revert /etc/hosts, clear latchkey, kill
     mock + socat

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
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
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

# --- knobs (override via env) ---

MINDS_APP_PATH = Path(os.environ.get("MINDS_APP_PATH", "/Applications/Minds.app/Contents/MacOS/Minds"))
MINDS_HOME = Path(os.environ.get("HOME", "/Users/macrunner")) / ".minds"
EVENTS_LOG = MINDS_HOME / "logs" / "minds-events.jsonl"
ONE_TIME_CODES = MINDS_HOME / "auth" / "one_time_codes.json"
SCREENSHOT_DIR = Path(os.environ.get("LAUNCH_TO_MSG_SHOTS_DIR", "/tmp/launch-to-msg-screenshots"))
SLACK_MOCK_STATE = Path("/tmp/slack-mock")
SLACK_MOCK_PORT = 8443  # plain HTTP; socat terminates TLS on :443
LATCHKEY_DIR = MINDS_HOME / "latchkey"

GIT_URL = os.environ.get("GIT_URL") or "https://github.com/imbue-ai/forever-claude-template"
GIT_BRANCH = os.environ.get("GIT_BRANCH") or "pilot"
HOST_NAME = os.environ.get("HOST_NAME") or f"e2e{time.strftime('%H%M%S')}"
FIRST_PROMPT = "Reply with exactly the four characters: pong"
FIRST_EXPECT = "pong"
CREATE_TIMEOUT = 900
REPLY_TIMEOUT = 480
DRIVE_SLACK_TIMEOUT = 360
LAUNCH_BACKEND_TIMEOUT = 120

# Canned slack-mock body the agent should quote back.
CANNED_BODY = "CI MOCK: greetings from the localhost slack mock."

NONCE = secrets.token_urlsafe(6)
SLACK_PROMPT = (
    "Read-only Slack task. DO NOT post, send, or write any message anywhere. "
    "Use only read-style Slack tool calls. Read one message from any channel "
    "and respond ONLY here in this chat panel (no Slack post) with the prefix "
    f'"TOK {NONCE}:" followed by the EXACT text of the message you read, '
    "character-for-character."
)

SKIP_FIRST_MESSAGE = os.environ.get("SKIP_FIRST_MESSAGE", "0") == "1"
SKIP_SLACK_FLOW = os.environ.get("SKIP_SLACK_FLOW", "0") == "1"

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


def snap(name: str) -> None:
    """Whole-desktop screencapture. Silently no-ops when no Aqua session."""
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
        logger.warning("  snap[{}] FAILED: {}", name, msg)
        if out.exists():
            out.unlink()
    if err.exists():
        err.unlink()


async def snap_page(page: Page, name: str) -> None:
    """Both Playwright per-page shot AND macOS desktop shot."""
    try:
        await page.set_viewport_size({"width": 1280, "height": 800})
    except Exception:
        pass
    try:
        await page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.win.png"), full_page=True)
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
    raise RuntimeError(f"slack mock failed to bind {SLACK_MOCK_PORT}")


# --- cert + /etc/hosts + socat + latchkey wiring ---


def ensure_cert() -> Path:
    """Generate self-signed cert for slack.com + files.slack.com once."""
    SLACK_MOCK_STATE.mkdir(parents=True, exist_ok=True)
    cert = SLACK_MOCK_STATE / "cert.pem"
    key = SLACK_MOCK_STATE / "key.pem"
    if cert.exists() and key.exists():
        return cert
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


def ensure_brew_curl() -> Path:
    """Return the path to a curl built against OpenSSL (honors --cacert).
    macOS system curl uses SecureTransport and ignores CURL_CA_BUNDLE."""
    for c in ("/opt/homebrew/opt/curl/bin/curl", "/usr/local/opt/curl/bin/curl"):
        if Path(c).exists():
            return Path(c)
    logger.info("brew curl missing -> brew install curl")
    brew = next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(b).exists()), None)
    if brew is None:
        raise RuntimeError("neither brew curl nor brew found; install brew + curl")
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
    # The launched socat ran under sudo so kill via sudo pkill.
    subprocess.run(["sudo", "pkill", "-f", "OPENSSL-LISTEN:443"], check=False)


def latchkey_shim() -> Path:
    return MINDS_APP_PATH.parent.parent / "Resources" / "latchkey" / "bin" / "latchkey"


def latchkey_env() -> dict[str, str]:
    """Env the bundled latchkey shim needs: encryption key + Electron exec path."""
    key_file = LATCHKEY_DIR / "encryption_key"
    if not key_file.exists():
        raise RuntimeError(f"latchkey encryption_key missing at {key_file}")
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


def wait_backend_url() -> str:
    """Return the http://127.0.0.1:<port> of the backend, from the events log."""

    deadline = time.time() + LAUNCH_BACKEND_TIMEOUT
    pattern = re.compile(
        r"Minds login URL \(one-time use\): (http://(?:127\.0\.0\.1|localhost):\d+/login\?one_time_code=[A-Za-z0-9_-]+)"
    )
    while time.time() < deadline:
        if EVENTS_LOG.exists():
            for line in EVENTS_LOG.read_text().splitlines():
                m = pattern.search(line)
                if m:
                    url = m.group(1).replace("localhost", "127.0.0.1")
                    base = url.split("/login")[0]
                    return base
        time.sleep(2)
    raise RuntimeError(f"no backend login URL after {LAUNCH_BACKEND_TIMEOUT}s")


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
    import urllib.request

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
    raise RuntimeError(f"CDP not reachable on :{port} after {timeout}s: {last_err}")


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


# --- main flow ---


async def amain() -> int:
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
    logger.info("brew curl: {}", brew_curl)

    # 1. Launch minds.app ourselves with --remote-debugging-port so Playwright
    # can attach via CDP. Use a free port to avoid clashes.
    cdp_port = _free_port()
    env = {
        **os.environ,
        "PATH": f"{brew_curl.parent}:" + os.environ.get("PATH", ""),
        "CURL_CA_BUNDLE": str(cert),
    }
    env.pop("ELECTRON_RUN_AS_NODE", None)
    logger.info("launching {} --remote-debugging-port={}", MINDS_APP_PATH, cdp_port)
    minds_proc = subprocess.Popen(
        [str(MINDS_APP_PATH), f"--remote-debugging-port={cdp_port}"],
        env=env,
        stdout=open("/tmp/minds-electron.log", "w"),
        stderr=subprocess.STDOUT,
    )

    async with async_playwright() as p:
        # Wait for CDP endpoint to be reachable
        cdp_url = await _wait_cdp(cdp_port)
        logger.info("attaching via CDP at {}", cdp_url)
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=60_000)
        # Single Electron context wraps all WebContentsViews as pages.
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Wait for first page (chrome shell or splash) to materialise.
        for _ in range(60):
            if ctx.pages:
                break
            await asyncio.sleep(0.5)
        if not ctx.pages:
            raise RuntimeError("no Electron windows after 30s")
        win = ctx.pages[0]
        await snap_page(win, "00-app-launched")

        # 2. Wait for backend, auth via OTC
        base = await asyncio.get_event_loop().run_in_executor(None, wait_backend_url)
        logger.info("backend up at {}", base)
        code = await asyncio.get_event_loop().run_in_executor(None, mint_one_time_code)
        origin = await win.evaluate("location.origin")
        await win.goto(origin + "/authenticate?one_time_code=" + code)
        await snap_page(win, "01-after-auth")

        # 3. Navigate to home; UI now reflects authenticated state
        await win.goto(origin + "/")
        await snap_page(win, "02-home-after-auth")

        if not SKIP_FIRST_MESSAGE:
            # 4. Create agent via API (not the form): the prod-tier form
            # defaults to compute=DOCKER without an Imbue Cloud account,
            # which a vanilla mac runner can't provision. POSTing /api
            # /create-agent directly with launch_mode=LIMA mirrors the
            # old bash flow (first-message-verify.sh) and is deterministic.
            # fetch() runs inside the auth'd page so the session cookie is
            # attached automatically.
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not anthropic_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set; can't create LIMA agent")
            create_body = {
                "agent_name": HOST_NAME,
                "host_name": HOST_NAME,
                "git_url": GIT_URL,
                "branch": GIT_BRANCH,
                "launch_mode": "LIMA",
                "ai_provider": "API_KEY",
                "anthropic_api_key": anthropic_key,
                "include_env_file": False,
            }
            resp = await win.evaluate(
                """async (body) => {
                    const r = await fetch('/api/create-agent', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                    return {status: r.status, body: await r.text()};
                }""",
                create_body,
            )
            if resp["status"] != 200:
                raise RuntimeError(f"create-agent HTTP {resp['status']}: {resp['body']}")
            creation_id = json.loads(resp["body"]).get("agent_id", "")
            if not creation_id:
                raise RuntimeError(f"no agent_id in create-agent response: {resp['body']}")
            logger.info("creation_id={}", creation_id)
            await snap_page(win, "03-create-agent-submitted")

            # 5. Poll /api/create-agent/<id>/status until DONE. The chat
            # panel does NOT auto-open when creation completes -- the
            # user normally clicks the tile on the home page. Once
            # status=DONE we do the same: navigate home, click the
            # tile, then wait for the chat-URL page to materialise.
            # Wall-clock per phase transition gets stitched into the
            # /tmp/launch-to-msg-timings.json artifact at end of script.
            deadline = time.time() + CREATE_TIMEOUT
            last_status = ""
            phase_started_at = time.monotonic()
            phase_durations: dict[str, float] = {}
            done = False
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
                            "creation status: {} -> {} (prev took {:.1f}s)",
                            last_status,
                            state,
                            phase_durations[last_status],
                        )
                    else:
                        logger.info("creation status: (none) -> {}", state)
                    last_status = state
                    phase_started_at = now
                if state == "DONE":
                    phase_durations[state] = round(time.monotonic() - phase_started_at, 2)
                    done = True
                    break
                if state == "FAILED":
                    raise RuntimeError(f"creation FAILED: {payload.get('error', stat['body'])}")
                await asyncio.sleep(5)
            # Emit a per-phase summary line + persist to artifact JSON.
            total_create_s = sum(phase_durations.values())
            logger.info("creation phase timings: {} (total={:.1f}s)", phase_durations, total_create_s)
            timings_artifact = SCREENSHOT_DIR / "launch-to-msg-timings.json"
            with contextlib.suppress(Exception):
                timings_artifact.write_text(
                    json.dumps({"phase_durations_s": phase_durations, "total_create_s": total_create_s}, indent=2)
                )
            if not done:
                if EVENTS_LOG.exists():
                    tail = EVENTS_LOG.read_text(errors="ignore").splitlines()[-60:]
                    logger.error("minds-events.jsonl tail:\n{}", "\n".join(tail))
                raise RuntimeError(f"creation didn't reach DONE in {CREATE_TIMEOUT}s (last={last_status})")
            logger.info("creation DONE; opening chat panel by clicking the tile")
            await win.goto(origin + "/")
            with contextlib.suppress(Exception):
                await win.click(f"text={HOST_NAME}", timeout=15_000)
            # Tile click may navigate `win` to the chat URL OR open a new
            # WebContentsView. Look at both.
            chat_win: Page | None = None
            chat_deadline = time.time() + 60
            chat_url_re = re.compile(r"agent-[a-f0-9]+\.localhost")
            while time.time() < chat_deadline:
                if chat_url_re.search(win.url):
                    chat_win = win
                    break
                cand = await find_chat_window(ctx)
                if cand is not None:
                    chat_win = cand
                    break
                await asyncio.sleep(1)
            if chat_win is None:
                for p in all_pages(ctx):
                    with contextlib.suppress(Exception):
                        await snap_page(p, f"99-no-chat-{p.url.split('/')[-1] or 'root'}")
                raise RuntimeError(
                    f"agent DONE but no chat-URL page opened after tile click (pages={[p.url for p in all_pages(ctx)]})"
                )
            win = chat_win
            logger.info("agent DONE; chat URL={}", win.url)
            await snap_page(win, "04-agent-DONE")

            # 6. Send first message, wait for "pong" reply
            inp = await win.wait_for_selector('textarea, [contenteditable="true"]', timeout=60_000)
            await inp.fill(FIRST_PROMPT)
            await inp.press("Enter")
            await snap_page(win, "05-first-message-sent")
            await win.wait_for_function(
                f"document.body.innerText.toLowerCase().includes('{FIRST_EXPECT}')",
                timeout=REPLY_TIMEOUT * 1000,
            )
            await snap_page(win, "06-first-message-reply")

        if not SKIP_SLACK_FLOW:
            # 7. Slack mock setup
            logger.info("=== slack flow ===")
            mock = start_mock()
            patch_etc_hosts()
            start_socat(cert)  # killed in finally via stop_socat()
            time.sleep(2)  # let socat bind
            try:
                latchkey_set_slack()
                # 8. Send slack prompt
                inp = await win.wait_for_selector('textarea, [contenteditable="true"]', timeout=60_000)
                await inp.fill(SLACK_PROMPT)
                await inp.press("Enter")
                await snap_page(win, "07-slack-prompt-sent")

                # 9. Wait for agent to emit permission request; click
                # Requests button -> entry -> Approve.
                #
                # After Approve, the requests-panel window closes and
                # Electron may shuffle the BrowserWindow z-order; ``win``
                # can end up pointing at the Projects page. Re-resolve
                # the chat panel each iteration so the canned-body check
                # always reads from the right window.
                approval_stage = 0
                deadline = time.time() + DRIVE_SLACK_TIMEOUT
                clicked_at = {}
                approved_request_urls: set[str] = set()
                while time.time() < deadline:
                    chat_now = await find_chat_window(ctx)
                    if chat_now is not None:
                        win = chat_now
                    # Check for canned body in chat (PASS).
                    body = await win.evaluate("document.body.innerText")
                    if CANNED_BODY.lower() in body.lower() and approval_stage >= 3:
                        logger.info("PASS: canned body in reply")
                        await snap_page(win, "08-PASS-canned-body")
                        break

                    if approval_stage < 3:
                        await _advance_approval(ctx, win, approval_stage, clicked_at)
                        approval_stage = clicked_at.get("stage", approval_stage)

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

                    await asyncio.sleep(2)
                else:
                    await snap_page(win, "99-TIMEOUT-no-canned-body")
                    # Dump every page's URL + first 200 chars of body so we
                    # can tell whether the chat panel was alive somewhere.
                    for p in all_pages(ctx):
                        with contextlib.suppress(Exception):
                            preview = (await p.evaluate("document.body.innerText"))[:200].replace("\n", " ")
                            logger.error("  page url={} body=...{!r}", p.url, preview)
                    raise RuntimeError(
                        f"canned body not in chat after {DRIVE_SLACK_TIMEOUT}s (approval_stage={approval_stage})"
                    )
            finally:
                logger.info("=== slack teardown ===")
                with contextlib.suppress(Exception):
                    latchkey_clear_slack()
                revert_etc_hosts()
                stop_socat()
                mock.shutdown()

        await browser.close()
        minds_proc.terminate()
        with contextlib.suppress(Exception):
            minds_proc.wait(timeout=10)
    caffeinate_proc.terminate()
    with contextlib.suppress(Exception):
        caffeinate_proc.wait(timeout=5)
    return 0


async def _advance_approval(ctx: BrowserContext, win: Page, stage: int, state: dict) -> None:
    """One step of the 3-stage approval click. Updates state['stage']."""
    # Stage 0: click Requests button to open the panel (skipped if
    # panel already auto-opened).
    if stage == 0:
        # Check if requests-panel window already exists.
        panel = None
        for w in all_pages(ctx):
            with contextlib.suppress(Exception):
                if "/_chrome/requests-panel" in w.url:
                    panel = w
                    break
        if panel is not None:
            logger.info("requests-panel auto-opened; advancing to stage 1")
            state["stage"] = 1
            return
        # Wait for the agent to emit its request signal first. Case-fold
        # because Claude rephrases the message each run (eg "Waiting"
        # vs "awaiting" vs "wait for").
        body = (await win.evaluate("document.body.innerText")).lower()
        if not any(
            s in body
            for s in (
                "permission request",
                "requested read",
                "approval",  # catches "Waiting for your approval", "awaiting", etc.
                "approve",
            )
        ):
            return  # not ready yet
        # Find the Requests button on any window.
        for w in all_pages(ctx):
            try:
                btn = w.locator('button[title="Requests"]')
                if await btn.count() > 0 and await btn.first.is_visible():
                    logger.info("clicking Requests button")
                    await snap_page(w, "07a-stage0-pre-click-requests")
                    await btn.first.click()
                    state["stage"] = 1
                    return
            except Exception:
                pass

    # Stage 1: click the slack entry in the requests-panel.
    elif stage == 1:
        panel = None
        for w in all_pages(ctx):
            with contextlib.suppress(Exception):
                if "/_chrome/requests-panel" in w.url:
                    panel = w
                    break
        if panel is None:
            return
        for sel in ("text=/slack/i", "text=/permission/i", "li", "button"):
            try:
                loc = panel.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = (await loc.inner_text()).strip().lower()
                    if txt in ("close", "cancel", "back", "requests"):
                        continue
                    logger.info("clicking permission entry via {!r}", sel)
                    await snap_page(panel, "07b-stage1-pre-click-entry")
                    await loc.click()
                    state["stage"] = 2
                    return
            except Exception:
                pass

    # Stage 2: click Approve in the per-request detail window.
    elif stage == 2:
        for w in all_pages(ctx):
            try:
                if "/requests/" not in w.url:
                    continue
                btn = w.locator('button:has-text("Approve")').first
                if await btn.count() > 0 and await btn.is_visible():
                    logger.info("clicking Approve")
                    await snap_page(w, "07c-stage2-pre-click-approve")
                    await btn.click()
                    state["stage"] = 3
                    await asyncio.sleep(2)
                    # Kick the agent to retry.
                    chat = await find_chat_window(ctx)
                    if chat is not None:
                        kick = await chat.wait_for_selector('textarea, [contenteditable="true"]', timeout=15_000)
                        await kick.fill(
                            "Permission approved. Please retry the read-only "
                            f'Slack read now and respond with the prefix "TOK {NONCE}:" '
                            "followed by the message text."
                        )
                        await kick.press("Enter")
                        logger.info("sent post-approval kick")
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
        logger.exception("FATAL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
