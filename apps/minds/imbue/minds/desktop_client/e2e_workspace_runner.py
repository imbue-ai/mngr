"""Reusable end-to-end driver for "Electron app creates a Docker workspace".

The flow encoded here is the same one the apps/minds Electron e2e test
asserts on: launch the Electron app, drive its create form via Playwright
over CDP, and wait until the workspace's ``system_interface`` dockview UI
renders through the desktop client's subdomain proxy.

Two callers consume this module:

- ``apps/minds/test_desktop_client_e2e.py`` -- the pytest test wraps
  :func:`create_workspace_via_electron` and always cleans up the resulting
  mngr agent in its ``finally``.
- ``scripts/snapshot_minds_e2e_state.py`` -- the Modal-snapshot script
  calls the same function but deliberately *does not* destroy the agent,
  because the whole point of the snapshot is to capture a sandbox in
  which the workspace's Docker container is alive and ready to use.

Everything in this module is non-pytest -- callers pass plain arguments
and own the environment / cleanup story themselves.
"""

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from typing import IO

import httpx
from loguru import logger
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from imbue.minds.config.loader import repo_tier_client_config_path

# This file lives at apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py,
# so parents[5] hops up over desktop_client, minds, imbue, minds, apps to the repo
# root. (The original copy of this code lived two levels closer to the root in
# apps/minds/test_desktop_client_e2e.py, where parents[2] was correct.)
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
_FCT_EXTERNAL_WORKTREE: Final[Path] = _REPO_ROOT / ".external_worktrees" / "forever-claude-template"
_FCT_REMOTE: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FCT_FALLBACK_BRANCH: Final[str] = "main"

# The contentView page URL contains ``/_chrome`` only for the chrome
# (sidebar/title-bar) view; the main content view never does. We match the
# pure-localhost backend pages, not the ``agent-<id>.localhost`` proxy.
# The capturing group exposes the bare origin (``http://localhost:<port>``)
# so :func:`_backend_origin_from_page` can reuse the same pattern instead of
# re-encoding the localhost-origin contract a second time.
_BACKEND_ORIGIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(http://localhost:\d+)(?:/|$)")
_CHROME_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://localhost:\d+/_chrome(?:/|$|\?)")
# The agent subdomain URL the create flow redirects to once the workspace's
# ``system_interface`` is reachable. The desktop client wraps that origin in
# the mngr_forward plugin, so the port may differ from the bare backend.
_AGENT_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://agent-[a-f0-9]+\.localhost:\d+(?:/|$)")

# Default env tier when nothing is activated. Staging's ``client.toml`` is
# committed under apps/minds/imbue/minds/config/envs/staging/ so callers
# can boot the backend without an explicit ``minds env activate`` step.
_DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds-staging"
_DEFAULT_MINDS_TIER: Final[str] = "staging"

_ELECTRON_BINARY: Final[Path] = _REPO_ROOT / "apps" / "minds" / "node_modules" / ".bin" / "electron"
_ELECTRON_MAIN_JS: Final[Path] = _REPO_ROOT / "apps" / "minds" / "electron" / "main.js"

# Per-phase wall-clock budgets. Tight enough to fail with a useful
# "stuck in <phase>" error before a wrapping suite-level timeout fires.
_CDP_READY_TIMEOUT_SECONDS: Final[int] = 120
_BACKEND_READY_TIMEOUT_SECONDS: Final[int] = 120
_CREATE_FORM_TIMEOUT_SECONDS: Final[int] = 600
_SYSTEM_INTERFACE_TIMEOUT_SECONDS: Final[int] = 180

# The onboarding wizard's screen-advance is driven by creating.js, a deferred
# script that attaches its ``.js-next`` click handlers after the page renders.
# Playwright's ``click`` waits for the button to be visible/stable but not for
# that handler to be wired up, so an early click can silently no-op and leave
# the wizard on the same screen. We click and confirm the screen advanced,
# retrying the click if it was lost.
_ONBOARDING_ADVANCE_TIMEOUT_MS: Final[int] = 5_000
_ONBOARDING_CLICK_ATTEMPTS: Final[int] = 3

# Pre-tested CSS selector against the system_interface frontend at
# .external_worktrees/forever-claude-template/apps/system_interface/.
# `.dockview-workspace` is the wrapper div the DockviewWorkspace mithril
# component mounts on first render.
_DOCKVIEW_WORKSPACE_SELECTOR: Final[str] = "div.dockview-workspace"


def configure_logging() -> None:
    """Route loguru to stderr at DEBUG with a compact format for operator runs."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="{time:HH:mm:ss.SSS} | {level:<7} | {function}:{line} - {message}",
    )


# Deliberate duplicate of ``imbue.mngr.utils.testing.find_free_port``: this
# module ships in the ``imbue-minds`` wheel, but ``imbue.mngr.utils.testing``
# is excluded from the ``imbue-mngr`` wheel and imports ``pytest`` at module
# scope (a non-runtime dep). Importing from there would either break the
# wheel install (missing module) or force pytest into a runtime dep. Keep
# the two copies in sync if either ever changes.
def find_free_port() -> int:
    """Return a port the OS is currently willing to hand out for TCP.

    Used to allocate the ``--remote-debugging-port`` Electron exposes. There
    is a small race between us closing the socket and Electron binding the
    port; on a quiet host the window is negligible. If a flaky bind ever
    shows up, the retry should live in :func:`_wait_for_cdp` rather than
    here (this helper exists to surface a single number).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _current_mngr_branch() -> str | None:
    """Return the current branch name of the mngr repo, or None if detached.

    Returning ``None`` for a detached HEAD lets the FCT resolver skip the
    "branch matching" step rather than asking FCT for a ref named ``HEAD``.

    Any failure to invoke git (missing ``.git`` -- e.g. when the runner
    executes inside a Modal sandbox whose source tree was uploaded via
    ``add_local_dir`` and the worktree's ``.git`` file points at a
    gitdir that does not exist on the sandbox; ``CalledProcessError``;
    ``TimeoutExpired``) is logged at warning level and treated as
    "branch unknown", which routes the caller through the documented
    fall-back to FCT ``main`` rather than crashing the whole run.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Could not determine current mngr branch ({!r}); treating as unknown", exc)
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _fct_remote_has_branch(branch: str) -> bool:
    """Return True iff the FCT public remote currently has ``branch``.

    ``git ls-remote`` exits 0 either way; presence is signalled by stdout
    being non-empty. Network-level failures (DNS hiccup, GitHub 5xx,
    proxy block, timeout) are logged as a warning and treated the same as
    "no such branch" so the caller still falls back to ``main`` per the
    documented 3-step chain rather than crashing the whole run on a
    transient probe failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", _FCT_REMOTE, branch],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Failed to query FCT remote for branch {!r}; treating as absent so main fallback runs: {!r}",
            branch,
            exc,
        )
        return False
    return bool(result.stdout.strip())


def _shallow_clone_fct(branch: str, destination: Path) -> Path:
    """Shallow-clone ``branch`` of the FCT public remote into ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, _FCT_REMOTE, str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return destination


def resolve_fct_path(scratch_dir: Path) -> Path:
    """Return a local FCT working tree via the 3-step fallback chain.

    Step 1 (preferred): operator-managed ``.external_worktrees/forever-claude-template/``.
    Step 2: a shallow clone of the current mngr branch from the FCT remote
    if FCT has a branch by that name.
    Step 3: a shallow clone of FCT ``main``.

    ``scratch_dir`` is the directory in which the shallow clone is placed
    when steps 2 or 3 fire (e.g. ``pytest tmp_path`` for the test,
    ``$TMPDIR`` for the snapshot script).
    """
    if _FCT_EXTERNAL_WORKTREE.is_dir() and (_FCT_EXTERNAL_WORKTREE / ".git").exists():
        logger.info("Using FCT external worktree at {}", _FCT_EXTERNAL_WORKTREE)
        return _FCT_EXTERNAL_WORKTREE

    destination = scratch_dir / "fct"
    branch = _current_mngr_branch()
    if branch is not None and _fct_remote_has_branch(branch):
        logger.info("Shallow-cloning FCT branch {!r} into {}", branch, destination)
        return _shallow_clone_fct(branch, destination)

    logger.info(
        "FCT remote does not have a branch named {!r}; falling back to {!r}",
        branch,
        _FCT_FALLBACK_BRANCH,
    )
    return _shallow_clone_fct(_FCT_FALLBACK_BRANCH, destination)


def ensure_minds_env_defaults(setenv: Callable[[str, str], None]) -> None:
    """Set ``MINDS_ROOT_NAME`` / ``MINDS_CLIENT_CONFIG_PATH`` if unset.

    Callers must supply the mutation strategy via ``setenv`` -- the
    repo style guide forbids mutating ``os.environ`` of the current
    process, so this library never picks the strategy on the caller's
    behalf. The pytest wrapper in
    ``apps/minds/test_desktop_client_e2e.py`` passes
    ``monkeypatch.setenv`` so the env vars get reverted between tests;
    the snapshot script (which runs in a throwaway sandbox) passes a
    setter that writes to ``os.environ`` directly. Both options share
    the validation / logging logic below.
    """
    if os.environ.get("MINDS_ROOT_NAME"):
        logger.info("Using inherited MINDS_ROOT_NAME={}", os.environ["MINDS_ROOT_NAME"])
        return

    config_path = repo_tier_client_config_path(_DEFAULT_MINDS_TIER)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Default tier {_DEFAULT_MINDS_TIER!r} has no client.toml at {config_path}; "
            "either activate a minds env explicitly or restore the staging config."
        )
    setenv("MINDS_ROOT_NAME", _DEFAULT_MINDS_ROOT_NAME)
    setenv("MINDS_CLIENT_CONFIG_PATH", str(config_path))
    logger.info(
        "No MINDS_ROOT_NAME activated; defaulting to {} (config={})",
        _DEFAULT_MINDS_ROOT_NAME,
        config_path,
    )


def _build_electron_env(workspace_git_url: Path, workspace_name: str) -> dict[str, str]:
    """Return the env vars the Electron child process should inherit.

    Mirrors ``just minds-start``: passes the FCT path + agent name through
    the ``MINDS_WORKSPACE_*`` prefill vars (honored only in dev tiers --
    see ``_dev_only_workspace_default`` in templates.py), and scrubs any
    ANTHROPIC creds the operator's shell might have exported so they
    don't silently leak into every workspace we create.
    """
    env = dict(os.environ)
    env["MINDS_WORKSPACE_GIT_URL"] = str(workspace_git_url)
    env["MINDS_WORKSPACE_NAME"] = workspace_name
    # Pin MNGR_ROOT_NAME back to "mngr" for the Electron child so the
    # spawned `mngr create` subprocess finds FCT's .mngr/settings.toml
    # (which defines the `main` + `docker` create templates). The minds
    # project conftest sets MNGR_ROOT_NAME=mngr-test-<timestamp> for test
    # isolation, but that would make mngr look for
    # .mngr-test-<timestamp>/settings.toml inside the FCT clone -- a file
    # that does not exist, causing mngr to abort with
    # `Template 'main' not found. No templates are configured`. MNGR_PREFIX
    # (the tmux session prefix) stays test-isolated so the spawned tmux
    # session does not collide with other tests' sessions.
    env["MNGR_ROOT_NAME"] = "mngr"
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    return env


def _drain_byte_stream_to_loguru(stream: IO[bytes], prefix: str) -> None:
    """Read lines from ``stream`` and forward each non-empty one to loguru.

    Module-level so it can be the target of :class:`threading.Thread`
    without tripping the inline-functions ratchet on this file.
    """
    for raw_line in iter(stream.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if line:
            logger.debug("[{}] {}", prefix, line)


def _stream_electron_output(process: subprocess.Popen[bytes]) -> None:
    """Drain Electron's stdout+stderr into the loguru sink in a background thread.

    Electron is verbose; without draining the pipes the OS buffer fills and
    Electron blocks. We don't parse anything; the caller reads state from CDP.
    """
    # ``_launched_electron`` always opens both pipes with ``subprocess.PIPE``;
    # the explicit None check narrows ``Popen.stdout``/``stderr`` from
    # ``IO[bytes] | None`` to ``IO[bytes]`` and turns a future regression
    # (someone drops ``stdout=PIPE``) into an obvious assertion failure rather
    # than a silent thread crash on ``None.readline``.
    if process.stdout is None or process.stderr is None:
        raise AssertionError("Electron subprocess was launched without piped stdout/stderr")
    for stream, prefix in ((process.stdout, "electron-out"), (process.stderr, "electron-err")):
        thread = threading.Thread(target=_drain_byte_stream_to_loguru, args=(stream, prefix), daemon=True)
        thread.start()


_ELECTRON_SIGTERM_GRACE_SECONDS: Final[int] = 30


@contextmanager
def _launched_electron(
    workspace_git_url: Path,
    workspace_name: str,
    debug_port: int,
) -> Iterator[subprocess.Popen[bytes]]:
    """Start the Electron app, yield the process, and always tear it down.

    SIGTERM with a ``_ELECTRON_SIGTERM_GRACE_SECONDS`` grace, then
    SIGKILL. The Electron main process owns the backend subprocess and
    the renderer; clean termination cascades. The grace window is
    intentionally generous (30s) because the minds backend that Electron
    spawns has its own asyncio teardown path that needs ~5-10 seconds to
    drain mngr_forward streams cleanly -- shorter grace periods
    routinely escalate to SIGKILL and leave the workspace in a
    half-shutdown state.

    Note: tearing down Electron does NOT destroy the workspace's mngr
    agent / Docker container. Those persist as separate host-level
    processes; cleanup of them is the caller's responsibility.
    """
    if not _ELECTRON_BINARY.is_file():
        raise FileNotFoundError(
            f"Electron binary missing at {_ELECTRON_BINARY}. Run `cd apps/minds && pnpm install` first."
        )

    cmd = [
        str(_ELECTRON_BINARY),
        str(_ELECTRON_MAIN_JS),
        f"--remote-debugging-port={debug_port}",
        # GitHub Actions runners ship Electron's chrome-sandbox binary
        # without the setuid bit, so the renderer aborts on launch with
        # `FATAL:setuid_sandbox_host.cc -- The SUID sandbox helper
        # binary was found, but is not configured correctly`. Disabling
        # the sandbox sidesteps the chown/chmod dance and matches the
        # well-trodden CI pattern (Playwright's own electron docs ship
        # `--no-sandbox` for the same reason). Acceptable here because
        # the binary we drive is a dev-mode Electron launched against
        # our own backend, not a downloaded one.
        "--no-sandbox",
    ]
    logger.info("Launching Electron: {}", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        env=_build_electron_env(workspace_git_url, workspace_name),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _stream_electron_output(process)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_ELECTRON_SIGTERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Electron did not exit on SIGTERM within {}s; sending SIGKILL",
                    _ELECTRON_SIGTERM_GRACE_SECONDS,
                )
                process.kill()
                process.wait(timeout=5)


def _wait_for_cdp(debug_port: int, timeout_seconds: int) -> None:
    """Poll the Chrome DevTools Protocol HTTP endpoint until it responds.

    A 200 from ``/json/version`` means the Electron renderer's debugger is
    accepting connections; Playwright's ``connect_over_cdp`` will succeed
    immediately after.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"status={response.status_code}"
        except (httpx.HTTPError, OSError) as exc:
            last_error = repr(exc)
        threading.Event().wait(timeout=0.5)
    raise TimeoutError(f"CDP at port {debug_port} did not respond within {timeout_seconds}s (last: {last_error})")


def _pick_content_page(browser: Browser, timeout_seconds: int) -> Page:
    """Return the Electron WebContentsView that serves the main content.

    Electron's BaseWindow has multiple WebContentsView's (chrome view,
    content view, requests panel, sidebar). Each is its own CDP page. The
    content view is the one whose URL is on the backend origin but is NOT
    rooted at ``/_chrome``. We poll until that page exists because Electron
    spawns the backend asynchronously after launch.
    """
    deadline = time.monotonic() + timeout_seconds
    last_observed: list[str] = []
    while time.monotonic() < deadline:
        last_observed = []
        for context in browser.contexts:
            for page in context.pages:
                url = page.url
                last_observed.append(url)
                if not _BACKEND_ORIGIN_PATTERN.match(url):
                    continue
                if _CHROME_PATH_PATTERN.match(url):
                    continue
                logger.info("Picked Electron content page at {}", url)
                return page
        threading.Event().wait(timeout=0.5)
    raise TimeoutError(
        f"No Electron content page settled on a backend URL within {timeout_seconds}s; observed pages: {last_observed}"
    )


def _backend_origin_from_page(page: Page) -> str:
    """Extract ``http://localhost:<backend_port>`` from a content-view page URL.

    Reuses :data:`_BACKEND_ORIGIN_PATTERN` so the localhost-origin contract
    is encoded in exactly one place; the pattern's capturing group exposes
    the bare origin without re-parsing the URL.
    """
    match = _BACKEND_ORIGIN_PATTERN.match(page.url)
    if match is None:
        raise AssertionError(f"Content page URL is not on the backend origin: {page.url!r}")
    return match.group(1)


def _ensure_field_value(page: Page, selector: str, expected_value: str) -> None:
    """Type ``expected_value`` into the form field if it isn't already there.

    Handles both the prefilled-via-env-var case (dev tiers) and the
    blank-form case (shared tiers like ``minds-staging`` where
    ``_dev_only_workspace_default`` deliberately ignores the
    ``MINDS_WORKSPACE_*`` env vars).
    """
    current_value = page.input_value(selector)
    if current_value == expected_value:
        logger.debug("Field {} already has expected value {!r}", selector, expected_value)
        return
    logger.info("Typing {!r} into {}", expected_value, selector)
    page.fill(selector, expected_value)


def _advance_onboarding_screen(page: Page, screen_name: str) -> None:
    """Click an onboarding screen's Next button and confirm the wizard advanced.

    Waits for the screen's ``.js-next`` to be visible, clicks it, then waits for
    that button to become hidden (``creating.js``'s ``showScreen`` toggles the
    leaving screen's ``hidden`` class). Retries the click because Playwright's
    ``click`` can land before the deferred ``creating.js`` attaches its handlers,
    silently no-opping; without the retry the wizard appears stuck on the next
    screen. Raises ``AssertionError`` if the screen never advances.
    """
    next_button = f'[data-screen="{screen_name}"] .js-next'
    page.wait_for_selector(next_button, state="visible", timeout=10_000)
    for _attempt in range(_ONBOARDING_CLICK_ATTEMPTS):
        page.click(next_button)
        try:
            page.wait_for_selector(next_button, state="hidden", timeout=_ONBOARDING_ADVANCE_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            logger.warning("Onboarding screen {!r} did not advance after click; retrying", screen_name)
    raise AssertionError(
        f"Onboarding screen {screen_name!r} did not advance after {_ONBOARDING_CLICK_ATTEMPTS} "
        "clicks of its Next button (creating.js handlers may not have attached)"
    )


def destroy_agent_best_effort(workspace_name: str) -> None:
    """Tear down the mngr agent created during a run. Always survives.

    ``mngr destroy`` may legitimately fail (e.g. the run crashed before
    create succeeded, the docker daemon stopped). We log and swallow.

    The pytest test calls this in its ``finally`` so the test never leaks
    an agent into the host. The snapshot script does NOT call it -- the
    whole point of the snapshot is to capture the sandbox with the agent
    alive.
    """
    cmd = ["uv", "run", "mngr", "destroy", workspace_name, "--force"]
    logger.info("Cleanup: {}", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("mngr destroy {} raised {!r}", workspace_name, exc)
        return
    if completed.returncode != 0:
        logger.warning(
            "mngr destroy {} exited {} (stderr: {})",
            workspace_name,
            completed.returncode,
            completed.stderr.strip(),
        )


def create_workspace_via_electron(
    fct_path: Path,
    workspace_name: str,
    debug_port: int,
) -> None:
    """Drive Electron to create a local Docker workspace from ``fct_path``.

    Returns once the workspace's ``system_interface`` dockview UI has
    rendered through the desktop client proxy. Does NOT clean up the
    resulting mngr agent or its Docker container -- the caller decides
    whether to destroy or to capture the state.

    Caller contract:
    - ``fct_path`` must be a populated FCT working tree (use
      :func:`resolve_fct_path`).
    - ``workspace_name`` must be unique within the current mngr install.
    - ``debug_port`` must be an unused TCP port (use :func:`find_free_port`).
    - ``MINDS_ROOT_NAME`` must already be set in ``os.environ`` (call
      :func:`ensure_minds_env_defaults` first or activate a minds env).
    """
    with _launched_electron(fct_path, workspace_name, debug_port):
        _wait_for_cdp(debug_port, _CDP_READY_TIMEOUT_SECONDS)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            try:
                page = _pick_content_page(browser, _BACKEND_READY_TIMEOUT_SECONDS)
                backend_origin = _backend_origin_from_page(page)
                logger.info("Backend origin: {}", backend_origin)

                logger.info("Navigating to /create")
                page.goto(f"{backend_origin}/create", wait_until="domcontentloaded")
                page.wait_for_selector("#create-form", state="attached", timeout=10_000)

                # The repo field lives inside the collapsed "Configure..."
                # panel's nested "Show advanced settings" section; open both
                # so the field is visible (and to mirror what a user setting
                # a non-default repo would do). ``#host_name`` is top-level.
                page.click("#configure-toggle")
                page.wait_for_selector("#toggle-advanced:visible", timeout=5_000)
                page.click("#toggle-advanced")
                page.wait_for_selector("#git_url:visible", timeout=5_000)

                _ensure_field_value(page, "#host_name", workspace_name)
                _ensure_field_value(page, "#git_url", str(fct_path))
                # Explicitly select the DOCKER compute provider: with no
                # account selected the form now defaults to LIMA (a local VM
                # that isn't available on the CI runner), so this test --
                # which is specifically about local Docker -- must pin DOCKER
                # rather than relying on the default. The select lives in the
                # (now-open) "Configure..." panel. AI provider stays at its
                # no-account default of SUBSCRIPTION.
                page.select_option("#launch_mode", "DOCKER")

                logger.info("Submitting create form")
                page.click("#create-submit")

                # Submitting starts creation in the background and lands on
                # the onboarding question flow. Walk the three questions
                # accepting their pre-selected defaults; finishing the last
                # one enters the workspace (directly if creation already
                # finished, otherwise via the loading screen, which redirects
                # once creation completes).
                page.wait_for_selector("#onboarding", state="attached", timeout=10_000)
                # Walk the three onboarding questions, accepting each
                # pre-selected default. q1/q2 advance to the next question
                # screen; confirm each advance (retrying the click) to absorb
                # the creating.js handler-attach race. The final (q3) Next runs
                # finishQuestions(), which shows the loading screen or redirects
                # straight to the workspace -- the wait_for_url below covers that
                # transition, and by q3 creating.js has long since loaded.
                _advance_onboarding_screen(page, "q1")
                _advance_onboarding_screen(page, "q2")
                q3_next_button = '[data-screen="q3"] .js-next'
                page.wait_for_selector(q3_next_button, state="visible", timeout=10_000)
                page.click(q3_next_button)

                page.wait_for_url(
                    _AGENT_SUBDOMAIN_PATTERN,
                    timeout=_CREATE_FORM_TIMEOUT_SECONDS * 1000,
                )
                logger.info("Workspace ready at {}", page.url)

                page.wait_for_selector(
                    _DOCKVIEW_WORKSPACE_SELECTOR,
                    state="visible",
                    timeout=_SYSTEM_INTERFACE_TIMEOUT_SECONDS * 1000,
                )
                logger.info("system_interface dockview rendered; workspace creation complete")
            finally:
                browser.close()
