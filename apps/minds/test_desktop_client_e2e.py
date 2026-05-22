"""End-to-end test that drives the real Electron minds app to create a
local Docker workspace from the forever-claude-template (FCT) repo.

The test launches ``apps/minds/electron/main.js`` via the project-local
``electron`` binary with ``--remote-debugging-port``, lets Electron
spawn the Python backend exactly the way ``just minds-start`` does,
connects to the Electron renderer via Playwright over CDP, types into
the create form, submits it, and waits for the workspace's
``system_interface`` dockview UI to render through the desktop client's
subdomain proxy.

The FCT source is resolved in three steps (first match wins):

1. ``<repo-root>/.external_worktrees/forever-claude-template/`` if that
   directory is a populated git working tree (operator-managed local
   worktree; matches the convention enforced by
   ``apps/minds/scripts/test_deployments.py``).
2. Otherwise, a shallow clone of the branch on the FCT public remote
   that matches the current mngr branch, into ``tmp_path``.
3. Otherwise, a shallow clone of FCT ``main`` into ``tmp_path``.

The test inherits whatever minds env the runner already activated. When
``MINDS_ROOT_NAME`` is unset, it defaults to the shared ``minds-staging``
tier (matches the repo-committed ``client.toml`` under
``apps/minds/imbue/minds/config/envs/staging/``).

Run locally:

    just minds-test-electron

Linux CI requires ``xvfb`` (the recipe wraps the invocation with
``xvfb-run -a``).
"""

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from typing import IO

import httpx
import pytest
from loguru import logger
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.mngr.utils.testing import get_short_random_string

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_FCT_EXTERNAL_WORKTREE: Final[Path] = _REPO_ROOT / ".external_worktrees" / "forever-claude-template"
_FCT_REMOTE: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FCT_FALLBACK_BRANCH: Final[str] = "main"

# The contentView page URL contains ``/_chrome`` only for the chrome
# (sidebar/title-bar) view; the main content view never does. We match the
# pure-localhost backend pages, not the ``agent-<id>.localhost`` proxy.
# The capturing group exposes the bare origin (``http://localhost:<port>``)
# so :func:`_backend_origin_from_page` can reuse the same pattern instead
# of re-encoding the localhost-origin contract a second time.
_BACKEND_ORIGIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(http://localhost:\d+)(?:/|$)")
_CHROME_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://localhost:\d+/_chrome(?:/|$|\?)")
# The agent subdomain URL the create flow redirects to once the workspace's
# ``system_interface`` is reachable. The desktop client wraps that origin in
# the mngr_forward plugin, so the port may differ from the bare backend.
_AGENT_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://agent-[a-f0-9]+\.localhost:\d+(?:/|$)")

# Default env tier when nothing is activated. Staging's ``client.toml`` is
# committed under apps/minds/imbue/minds/config/envs/staging/ so the test
# can boot the backend without an explicit ``minds env activate`` step.
_DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds-staging"
_DEFAULT_MINDS_TIER: Final[str] = "staging"

_ELECTRON_BINARY: Final[Path] = _REPO_ROOT / "apps" / "minds" / "node_modules" / ".bin" / "electron"
_ELECTRON_MAIN_JS: Final[Path] = _REPO_ROOT / "apps" / "minds" / "electron" / "main.js"

# Per-phase wall-clock budgets. ``@pytest.mark.timeout(900)`` is the
# absolute cap; the per-phase budgets below are tight enough to fail with
# a useful "stuck in <phase>" error before the overall timeout fires.
_CDP_READY_TIMEOUT_SECONDS: Final[int] = 120
_BACKEND_READY_TIMEOUT_SECONDS: Final[int] = 120
_CREATE_FORM_TIMEOUT_SECONDS: Final[int] = 600
_SYSTEM_INTERFACE_TIMEOUT_SECONDS: Final[int] = 180

# Pre-tested CSS selectors against the system_interface frontend at
# `.external_worktrees/forever-claude-template/apps/system_interface/`.
# `.dockview-workspace` is the wrapper div the DockviewWorkspace mithril
# component mounts on first render.
_DOCKVIEW_WORKSPACE_SELECTOR: Final[str] = "div.dockview-workspace"


def _configure_test_logging() -> None:
    """Route loguru to stderr at DEBUG so the test's tracing shows up in pytest -s."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="{time:HH:mm:ss.SSS} | {level:<7} | {function}:{line} - {message}",
    )


def _find_free_port() -> int:
    """Return a port the OS is currently willing to hand out for TCP.

    Used to allocate the ``--remote-debugging-port`` Electron exposes. There
    is a small race between us closing the socket and Electron binding the
    port; on a quiet CI host the window is negligible. If a flaky bind ever
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
    """
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _fct_remote_has_branch(branch: str) -> bool:
    """Return True iff the FCT public remote currently has ``branch``.

    ``git ls-remote`` exits 0 either way; presence is signalled by stdout
    being non-empty.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--heads", _FCT_REMOTE, branch],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
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


def _resolve_fct_path(tmp_path: Path) -> Path:
    """Return a local FCT working tree via the 3-step fallback chain.

    Step 1 (preferred): operator-managed ``.external_worktrees/forever-claude-template/``.
    Step 2: a shallow clone of the current mngr branch from the FCT remote
    if FCT has a branch by that name.
    Step 3: a shallow clone of FCT ``main``.
    """
    if _FCT_EXTERNAL_WORKTREE.is_dir() and (_FCT_EXTERNAL_WORKTREE / ".git").exists():
        logger.info("Using FCT external worktree at {}", _FCT_EXTERNAL_WORKTREE)
        return _FCT_EXTERNAL_WORKTREE

    destination = tmp_path / "fct"
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


def _resolve_minds_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``MINDS_ROOT_NAME`` and ``MINDS_CLIENT_CONFIG_PATH`` are set.

    Uses whatever the test runner has already activated. When
    ``MINDS_ROOT_NAME`` is unset, falls back to the shared
    ``minds-staging`` tier whose ``client.toml`` is committed in this
    repo (so no external setup is required to run the test).
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
    monkeypatch.setenv("MINDS_ROOT_NAME", _DEFAULT_MINDS_ROOT_NAME)
    monkeypatch.setenv("MINDS_CLIENT_CONFIG_PATH", str(config_path))
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
    don't silently leak into every workspace the test creates.
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


def _stream_electron_output(process: subprocess.Popen[bytes]) -> None:
    """Drain Electron's stdout+stderr into the loguru sink in a background thread.

    Electron is verbose; without draining the pipes the OS buffer fills and
    Electron blocks. We don't parse anything; the test reads state from CDP.
    """

    def _drain(stream: IO[bytes], prefix: str) -> None:
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.debug("[{}] {}", prefix, line)

    # ``_launched_electron`` always opens both pipes with ``subprocess.PIPE``;
    # the explicit None check narrows ``Popen.stdout``/``stderr`` from
    # ``IO[bytes] | None`` to ``IO[bytes]`` and turns a future regression
    # (someone drops ``stdout=PIPE``) into an obvious assertion failure rather
    # than a silent thread crash on ``None.readline``.
    if process.stdout is None or process.stderr is None:
        raise AssertionError("Electron subprocess was launched without piped stdout/stderr")
    for stream, prefix in ((process.stdout, "electron-out"), (process.stderr, "electron-err")):
        thread = threading.Thread(target=_drain, args=(stream, prefix), daemon=True)
        thread.start()


@contextmanager
def _launched_electron(
    workspace_git_url: Path,
    workspace_name: str,
    debug_port: int,
) -> Iterator[subprocess.Popen[bytes]]:
    """Start the Electron app, yield the process, and always tear it down.

    SIGTERM with a 5s grace, then SIGKILL. The Electron main process owns
    the backend subprocess and the renderer; clean termination cascades.
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
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Electron did not exit on SIGTERM; sending SIGKILL")
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


def _destroy_agent_best_effort(workspace_name: str) -> None:
    """Tear down the mngr agent created during the test. Always survives.

    ``mngr destroy`` may legitimately fail (e.g. the test crashed before
    create succeeded, the docker daemon stopped). We log and swallow.
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


# Carrying only the resource marks the *test process* sees a host-side
# invocation of, after the test passes end-to-end:
#
# - `docker` (CLI) is invoked by the spawned `mngr create` subprocess to
#   start the container; the PATH-injected resource-guard wrapper catches it.
# - `rsync` is invoked by `mngr create` to overlay the FCT worktree onto
#   the internal clone; same PATH wrapper.
#
# Marks we deliberately do *not* carry, and why:
#
# - `tmux` -- the workspace agent's tmux session lives *inside* the docker
#   container, never on the host, so the host's tmux wrapper never ticks
#   the counter and the guard fires post-hoc with "marked tmux but never
#   invoked tmux".
# - `docker_sdk` -- the Python `docker` SDK guard is a wrapper around the
#   in-process SDK import, not a PATH wrapper, so it only sees uses from
#   *this* pytest process. mngr's docker SDK calls happen in the spawned
#   subprocess and never reach our SDK wrapper, so the mark fires the
#   same "marked but never invoked" check.
@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.minds_electron
@pytest.mark.timeout(900)
def test_create_local_docker_workspace_via_electron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the Electron app to create a local Docker workspace from FCT.

    Asserts the workspace's ``system_interface`` dockview UI renders
    through the desktop client proxy. Cleans up the mngr agent in
    ``finally`` regardless of outcome.
    """
    _configure_test_logging()
    _resolve_minds_env(monkeypatch)

    fct_path = _resolve_fct_path(tmp_path)
    workspace_name = f"forever-{get_short_random_string()}"
    debug_port = _find_free_port()
    logger.info("Workspace name: {}; CDP debug port: {}", workspace_name, debug_port)

    try:
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

                    # The fields are inside the collapsed "Advanced options"
                    # section; opening the section first lets us see typed
                    # values during debugging and matches what a user would do.
                    page.click("#toggle-advanced")
                    page.wait_for_selector("#git_url:visible", timeout=5_000)

                    _ensure_field_value(page, "#host_name", workspace_name)
                    _ensure_field_value(page, "#git_url", str(fct_path))
                    # The create form defaults to the LIMA compute provider;
                    # this job's runner has Docker but not limactl, so select
                    # LOCAL (Docker) explicitly. The ai_provider select is
                    # left at its SUBSCRIPTION default.
                    page.select_option("#launch_mode", "LOCAL")

                    logger.info("Submitting create form")
                    page.click("#create-submit")
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
                    logger.info("system_interface dockview rendered; test passed")
                finally:
                    browser.close()
    finally:
        _destroy_agent_best_effort(workspace_name)
