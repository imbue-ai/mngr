"""Reusable end-to-end driver for "Electron app creates a Docker workspace".

The flow encoded here is the same one the apps/minds Electron e2e test
asserts on: launch the Electron app, drive its create form via Playwright
over CDP, and wait until the workspace's ``system_interface`` desktop
renders through the desktop client's subdomain proxy.

Everything in this module is non-pytest -- callers pass plain arguments
and own the environment / cleanup story themselves.
"""

import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from loguru import logger
from playwright.sync_api import Browser
from playwright.sync_api import Dialog
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import Playwright
from playwright.sync_api import Position
from playwright.sync_api import Response
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.bootstrap import CI_SNAPSHOT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import STUDIO_ROOTS_OVERRIDE_ENV_VAR
from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.desktop_client.default_workspace_template_worktree import DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE
from imbue.minds.desktop_client.default_workspace_template_worktree import current_worktree_branch
from imbue.minds.desktop_client.welcome_chat import SEED_TIMEOUT_SECONDS

# This file lives at apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py,
# so parents[5] hops up over desktop_client, minds, imbue, minds, apps to the repo
# root. (The original copy of this code lived two levels closer to the root in
# apps/minds/test_desktop_client_e2e.py, where parents[2] was correct.)
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]

# Every SPA route (the hub pages and the ``/workspace/<id>`` workspace
# surface) is served from the backend's bare-localhost origin. We match those
# backend pages, not the ``agent-<id>.localhost`` proxy.
# The capturing group exposes the bare origin (``http://localhost:<port>``)
# so :func:`backend_origin_from_page` can reuse the same pattern instead of
# re-encoding the localhost-origin contract a second time.
_BACKEND_ORIGIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(http://localhost:\d+)(?:/|$)")
# The agent subdomain URL the create flow redirects to once the workspace's
# ``system_interface`` is reachable. The desktop client wraps that origin in
# the mngr_forward plugin, so the port may differ from the bare backend. The
# scheme is ``https`` when the proxy serves TLS + HTTP/2 (the default) and
# ``http`` otherwise, so accept both. (The bare Imbue Studio backend origin stays
# plain ``http`` -- see ``_BACKEND_ORIGIN_PATTERN``.) New origins carry the
# workspace id (``agent-<hex>``); ``host-<hex>`` covers pre-existing
# workspaces still on the legacy machine-keyed origin.
_AGENT_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:[a-z0-9_-]+\.)*(?:host|agent)-[a-f0-9]+\.localhost:\d+(?:/|$)"
)

# Default env identity when nothing is activated: the dedicated, inert
# ``ci-snapshot`` tier, so callers can boot the backend without an explicit
# ``minds-admin env activate`` step and without pointing at any real environment.
_DEFAULT_MINDS_ROOT_NAME: Final[str] = CI_SNAPSHOT_MINDS_ROOT_NAME
# The throwaway root the Linux harnesses point STUDIO_ROOTS_OVERRIDE at. Named here
# so test_snapshot_resume.py can find what a baked image actually contains.
_E2E_DATA_HOME_DIR_NAME: Final[str] = ".minds-e2e-data-home"
_DEFAULT_MINDS_TIER: Final[str] = "ci-snapshot"

_ELECTRON_BINARY: Final[Path] = _REPO_ROOT / "apps" / "minds" / "node_modules" / ".bin" / "electron"
_ELECTRON_MAIN_JS: Final[Path] = _REPO_ROOT / "apps" / "minds" / "electron" / "main.js"
# Set (presence only) on every Electron this harness launches; read by
# electron/sentry.js, which tags the run's Sentry events with it.
E2E_HARNESS_MARKER_ENV_VAR: Final[str] = "MINDS_LAUNCHED_BY_E2E_HARNESS"
# Electron output lines forwarded at info instead of debug: main.js's [gpu] lines
# and termination-signal lines, Chromium's own "GPU process isn't usable"
# abort, and the electron launcher's report that the app died by a signal (the
# launcher exits 1 in that case, so the returncode alone does not say which).
_ELECTRON_OUTPUT_INFO_MARKERS: Final[tuple[str, ...]] = (
    "[gpu] ",
    "GPU process",
    "[lifecycle] SIG",
    "exited with signal",
)


class ElectronLaunchTarget(FrozenModel):
    """How to start the Imbue Studio app: a program, plus the main script a bare Electron runtime needs."""

    executable: Path = Field(description="The program to run: a packaged build, or this checkout's Electron binary.")
    main_js: Path | None = Field(
        description="The Electron main script handed to a bare runtime; None for a packaged build, which has its own."
    )
    extra_arguments: tuple[str, ...] = Field(description="Chromium switches appended to every launch of this target.")


# This checkout's Electron running this checkout's app.
DEV_CHECKOUT_LAUNCH_TARGET: Final[ElectronLaunchTarget] = ElectronLaunchTarget(
    executable=_ELECTRON_BINARY, main_js=_ELECTRON_MAIN_JS, extra_arguments=()
)

# Per-phase wall-clock budgets. Tight enough to fail with a useful
# "stuck in <phase>" error before a wrapping suite-level timeout fires.
_CDP_READY_TIMEOUT_SECONDS: Final[int] = 120
_BACKEND_READY_TIMEOUT_SECONDS: Final[int] = 120
# ``connect_over_cdp`` occasionally hangs in its CDP handshake under
# Electron-in-CI even after ``/json/version`` is up (GPU/sandbox/dbus quirks):
# the WebSocket connects but target negotiation stalls, and it stays wedged for
# that Electron instance (retrying the connect against the same process does not
# recover it). So we bound a single connect attempt and instead relaunch Electron
# from scratch -- a fresh process gets a fresh CDP endpoint. Only the launch +
# connect is retried; once a page is obtained the create flow runs once so real
# failures still surface.
_CDP_CONNECT_TIMEOUT_MS: Final[float] = 60_000.0
_ELECTRON_LAUNCH_ATTEMPTS: Final[int] = 3
# A cross-origin main-frame navigation that lands while a CDP session is
# already attached (Electron process-swaps the renderer -- the chrome view's
# file://shell.html -> http://<backend>/ boot handoff) can leave the connected
# Playwright client holding a page object frozen on the pre-swap URL forever.
# A FRESH connection re-enumerates targets with their current URLs, so the
# attach phase waits for the backend page in short rounds and reconnects
# between rounds instead of trusting one session for the full budget.
_PICK_ROUND_SECONDS: Final[int] = 20
# Budget for the whole create flow after submitting the form, which includes a
# full docker build of the workspace image inside the CI sandbox -- legitimately
# ~8-10.5 minutes there. The build's duration is network-bound (apt/pip
# mirrors, the pi extension npm installs), so headroom -- not a tighter
# deadline -- is what keeps this signal meaningful. The snapshot script gives
# `docker build` itself 900 seconds (MNGR__PROVIDERS__DOCKER__BUILD_TIMEOUT_SECONDS);
# this budget sits above that so the container boot after the build still fits.
_CREATE_FORM_TIMEOUT_SECONDS: Final[int] = 1200
_SYSTEM_INTERFACE_TIMEOUT_SECONDS: Final[int] = 180
_CREATE_OUTCOME_POLL_INTERVAL_MS: Final[int] = 500

# The desktop's backdrop, which the system_interface frontend renders once it has its desktops
# (docs/system/blueprint/desktop-interface/contracts.md section 12 in default-workspace-template).
DESKTOP_BACKDROP_SELECTOR: Final[str] = "[data-desktop-id]"

# The creating page's welcome-chat hand-over, whose statuses ``_handle_seed_welcome_chat`` documents
# (``ui_api_create.py``).
_WELCOME_CHAT_HAND_OVER_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"/ui/api/create/attempts/[^/]+/welcome-chat$"
)
_WELCOME_CHAT_HELD_STATUS: Final[int] = 202
_WELCOME_CHAT_SEEDED_STATUS: Final[int] = 200
# Past the desktop client's own bound on the seed, so a slow seed ends in its verdict rather than this wait's.
_WELCOME_CHAT_SEED_TIMEOUT_SECONDS: Final[float] = SEED_TIMEOUT_SECONDS + 30.0


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


def resolve_default_workspace_template_path() -> Path:
    """Return the DEFAULT_WORKSPACE_TEMPLATE working tree that workspace-create-attempt tests build from.

    The tree is produced ahead of time by
    :func:`imbue.minds.desktop_client.default_workspace_template_worktree.materialize_paired_default_workspace_template_worktree`
    -- on the CI runner before the snapshot image is staged, or by the local
    test recipe -- and either lives at ``.external_worktrees/default-workspace-template``
    or is baked into the snapshot image there. Consumers only *use* it; an absent
    worktree means the materialize step did not run, which is a setup error we
    surface loudly rather than silently cloning the released DEFAULT_WORKSPACE_TEMPLATE tag.
    """
    if (
        DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE.is_dir()
        and (DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE / ".git").exists()
    ):
        logger.info("Using DEFAULT_WORKSPACE_TEMPLATE worktree at {}", DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE)
        return DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE
    raise WorkspaceFlowError(
        f"No DEFAULT_WORKSPACE_TEMPLATE worktree at {DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE}. Run materialize_paired_default_workspace_template_worktree() first "
        "(the CI snapshot bake materializes it before staging; local runs go through the test recipe)."
    )


def ensure_minds_env_defaults(setenv: Callable[[str, str], None]) -> None:
    """Set ``MINDS_ROOT_NAME`` / ``MINDS_CLIENT_CONFIG_PATH`` if unset.

    Callers must supply the mutation strategy via ``setenv`` -- the
    repo style guide forbids mutating ``os.environ`` of the current
    process, so this library never picks the strategy on the caller's
    behalf. The pytest wrapper in
    ``apps/minds/test_snapshot_resume.py`` passes
    ``monkeypatch.setenv`` so the env vars get reverted between tests;
    the snapshot script (which runs in a throwaway sandbox) passes a
    setter that writes to ``os.environ`` directly. Both options share
    the validation / logging logic below.

    Also points the create form at the present paired DEFAULT_WORKSPACE_TEMPLATE worktree (the same
    ``MINDS_WORKSPACE_*`` env vars ``just minds-start`` sets), so ``mngr create``
    builds from that worktree's branch (which pins its own mngr) rather than
    the released ``FALLBACK_BRANCH`` tag. That step runs regardless of the
    ``MINDS_ROOT_NAME`` early return below.
    """
    _ensure_paired_workspace_env(setenv)
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


def _ensure_paired_workspace_env(setenv: Callable[[str, str], None]) -> None:
    """Point the create form at the present DEFAULT_WORKSPACE_TEMPLATE worktree on its own branch.

    When the materialized worktree exists, set the ``just minds-start`` env vars
    so ``mngr create`` builds from that worktree's branch (not the released
    ``FALLBACK_BRANCH`` tag). No-op when the worktree is absent (the consumer
    surfaces that) or when a var is already set (an explicit override wins).
    """
    if not (
        DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE.is_dir()
        and (DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE / ".git").exists()
    ):
        return
    if not os.environ.get("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"):
        setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    if not os.environ.get("MINDS_WORKSPACE_GIT_URL"):
        setenv("MINDS_WORKSPACE_GIT_URL", str(DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE))
    if not os.environ.get("MINDS_WORKSPACE_BRANCH"):
        branch = current_worktree_branch(DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE)
        if branch is not None:
            setenv("MINDS_WORKSPACE_BRANCH", branch)
        else:
            logger.warning(
                "DEFAULT_WORKSPACE_TEMPLATE worktree at {} has no resolvable branch; leaving MINDS_WORKSPACE_BRANCH unset",
                DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE,
            )


def _build_electron_env(
    workspace_git_url: Path | None,
    extra_env: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the env vars the Electron child process should inherit.

    Mirrors ``just minds-start``: passes the DEFAULT_WORKSPACE_TEMPLATE path through the
    ``MINDS_WORKSPACE_GIT_URL`` prefill var (honored only when the explicit
    opt-in ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is also set -- see
    ``_operator_workspace_default`` in workspace_defaults.py), and scrubs any
    ANTHROPIC creds the operator's shell might have exported so they
    don't silently leak into every workspace we create. A ``None`` template
    path leaves the create form on the app's own published defaults, which is
    what a run that tests the shipped product wants.

    ``extra_env`` is layered over the inherited environment first, so a caller
    that must not mutate its own ``os.environ`` (the demo harness selecting a
    data root and display) can still address the child; the prefill and scrub
    below win over it. ``base_env`` replaces that inherited environment
    outright, for a caller that composes the child's environment itself rather
    than handing down its own (the smoke suite, whose app under test is a
    shipped build that must not see this process's pytest variables).

    The workspace name is not prefilled: ``drive_create_flow`` types it into
    the create form's "Name" field directly, which is what pins the name the
    test later destroys by.
    """
    env = dict(os.environ if base_env is None else base_env)
    if extra_env is not None:
        env.update(extra_env)
    if workspace_git_url is not None:
        env["MINDS_WORKSPACE_GIT_URL"] = str(workspace_git_url)
        # Opt into the local-worktree create-form defaults (see just minds-start).
        env["MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"] = "1"
    # Pin MNGR_ROOT_NAME back to "mngr" for the Electron child so the
    # spawned `mngr create` subprocess finds DEFAULT_WORKSPACE_TEMPLATE's .mngr/settings.toml
    # (which defines the `main` + `docker` create templates). The Imbue Studio
    # project conftest sets MNGR_ROOT_NAME=mngr-test-<timestamp> for test
    # isolation, but that would make mngr look for
    # .mngr-test-<timestamp>/settings.toml inside the DEFAULT_WORKSPACE_TEMPLATE clone -- a file
    # that does not exist, causing mngr to abort with
    # `Template 'main' not found. No templates are configured`. MNGR_PREFIX
    # (the tmux session prefix) stays test-isolated so the spawned tmux
    # session does not collide with other tests' sessions.
    env["MNGR_ROOT_NAME"] = "mngr"
    # This harness runs the real Electron shell on Linux (the snapshot job builds
    # its image in a Modal container), where the shell would otherwise write to
    # the user's dotfolder. STUDIO_ROOTS_OVERRIDE gives it one throwaway root per run,
    # which also keeps the container reproducible.
    env.setdefault(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(Path(env.get("HOME", "/root")) / _E2E_DATA_HOME_DIR_NAME))
    env[E2E_HARNESS_MARKER_ENV_VAR] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_BASE_URL", None)
    return env


def _drain_byte_stream_to_loguru(stream: IO[bytes], prefix: str) -> None:
    """Read lines from ``stream`` and forward each non-empty one to loguru.

    Lines carrying one of ``_ELECTRON_OUTPUT_INFO_MARKERS`` go out at info
    rather than debug, so a run's GPU-process deaths, termination signals and
    the electron launcher's signal-death report are visible without debug
    logging.

    Module-level so it can be the target of :class:`threading.Thread`
    without tripping the inline-functions ratchet on this file.
    """
    for raw_line in iter(stream.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        if any(marker in line for marker in _ELECTRON_OUTPUT_INFO_MARKERS):
            logger.info("[{}] {}", prefix, line)
        else:
            logger.debug("[{}] {}", prefix, line)


def _stream_electron_output(process: subprocess.Popen[bytes]) -> None:
    """Drain Electron's stdout+stderr into the loguru sink in a background thread.

    Electron is verbose; without draining the pipes the OS buffer fills and
    Electron blocks. We don't parse anything; the caller reads state from CDP.
    """
    if process.stdout is None or process.stderr is None:
        raise AssertionError("Electron subprocess was launched without piped stdout/stderr")
    for stream, prefix in ((process.stdout, "electron-out"), (process.stderr, "electron-err")):
        thread = threading.Thread(target=_drain_byte_stream_to_loguru, args=(stream, prefix), daemon=True)
        thread.start()


_ELECTRON_SIGTERM_GRACE_SECONDS: Final[int] = 30


def _signal_process_group(process_group_id: int, sig: int) -> None:
    """Send ``sig`` to a whole process group, ignoring an already-dead group."""
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        pass


def _terminate_electron_process_tree(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM (then SIGKILL) the Electron process group, so no child survives.

    Electron is launched as a session leader (``start_new_session=True``), so
    its renderer/GPU/utility children and the backend it spawns share its
    process group. Signalling the group -- rather than just ``process.pid`` --
    guarantees the whole tree dies; a leftover child would otherwise keep the
    profile's single-instance lock held and wedge the next relaunch.
    """
    # Whether Electron was still alive when teardown began, and how it exited,
    # separates "the app was killed mid-teardown" from "the app had already died
    # on its own" (e.g. a browser-process abort that made the CDP attach fail).
    early_returncode = process.poll()
    if early_returncode is not None:
        logger.info(
            "Electron (pid {}) had already exited before teardown (returncode={})", process.pid, early_returncode
        )
        return
    try:
        process_group_id = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    logger.info("Electron (pid {}) still running at teardown; sending SIGTERM to process group", process.pid)
    _signal_process_group(process_group_id, signal.SIGTERM)
    try:
        returncode = process.wait(timeout=_ELECTRON_SIGTERM_GRACE_SECONDS)
        logger.info("Electron (pid {}) exited after SIGTERM (returncode={})", process.pid, returncode)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Electron did not exit on SIGTERM within {}s; sending SIGKILL",
            _ELECTRON_SIGTERM_GRACE_SECONDS,
        )
        _signal_process_group(process_group_id, signal.SIGKILL)
        try:
            returncode = process.wait(timeout=5)
            logger.info("Electron (pid {}) exited after SIGKILL (returncode={})", process.pid, returncode)
        except subprocess.TimeoutExpired:
            logger.warning("Electron process group did not exit within 5s of SIGKILL")


@contextmanager
def _user_data_dir_for_launch(persistent_dir: Path | None) -> Generator[str, None, None]:
    """The profile directory a launch runs with: the persistent one when given, else a fresh temporary one."""
    if persistent_dir is not None:
        persistent_dir.mkdir(parents=True, exist_ok=True)
        yield str(persistent_dir)
        return
    with tempfile.TemporaryDirectory(prefix="minds-electron-userdata-") as temporary_dir:
        yield temporary_dir


@contextmanager
def _launched_electron(
    workspace_git_url: Path | None,
    debug_port: int,
    host_config_dir: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    # A profile to keep between launches (cookies, the desktop's client identity); a throwaway one per launch when None.
    user_data_dir: Path | None = None,
    # Which build to start.
    launch_target: ElectronLaunchTarget = DEV_CHECKOUT_LAUNCH_TARGET,
    # The environment to launch from, in place of this process's own (see ``_build_electron_env``).
    base_env: Mapping[str, str] | None = None,
) -> Generator[subprocess.Popen[bytes], None, None]:
    """Start the Electron app, yield the process, and always tear it down.

    ``host_config_dir`` becomes the Electron process's cwd, so the
    host-side ``mngr`` invocations the app spawns (e.g. the ``mngr auth
    list`` account-discovery poll, ``mngr forward``) resolve their
    project config by walking up from there instead of the mngr repo
    root. The pytest wrapper points this at an isolated, opted-in config
    tree so the real repo ``.mngr/`` (which carries ``is_allowed_in_pytest
    = false`` plus a developer's untracked ``settings.local.toml``) is
    never loaded under the pytest config guard. ``None`` keeps the mngr
    repo root, which is what the snapshot script wants.

    SIGTERM with a ``_ELECTRON_SIGTERM_GRACE_SECONDS`` grace, then
    SIGKILL -- delivered to the whole process group (Electron is launched
    as a session leader via ``start_new_session=True``), not just the main
    PID. The Electron main process owns the backend subprocess and the
    renderer/GPU children; signalling the group ensures they all die
    instead of being orphaned. That matters for the retry path: a SIGKILL
    that left renderer/GPU children alive would keep them holding the
    profile's single-instance lock, so the next relaunch would bind its
    debug port, fail ``requestSingleInstanceLock()``, and quit immediately
    (the CDP port then refusing every connection). The grace window is
    intentionally generous (30s) because the Imbue Studio backend that Electron
    spawns needs a few seconds to drain mngr_forward streams cleanly --
    shorter grace periods routinely escalate to SIGKILL and leave the
    workspace in a half-shutdown state.

    Each launch also gets its own throwaway ``--user-data-dir`` so that,
    even if a prior attempt's teardown was imperfect, this instance never
    collides with a stale single-instance lock from the default profile --
    unless the caller passes ``user_data_dir`` to keep one profile across
    launches (the demo harness does, so the desktop keeps seeing the same
    browser client), in which case releasing that profile's lock before a
    relaunch rests on the whole-group teardown above.

    Note: tearing down Electron does NOT destroy the workspace's mngr
    agent / Docker container. Those persist as separate host-level
    processes; cleanup of them is the caller's responsibility.
    """
    if not launch_target.executable.is_file():
        install_hint = (
            " Run `cd apps/minds && pnpm install` first." if launch_target == DEV_CHECKOUT_LAUNCH_TARGET else ""
        )
        raise FileNotFoundError(f"Electron executable missing at {launch_target.executable}.{install_hint}")

    with _user_data_dir_for_launch(user_data_dir) as launch_user_data_dir:
        cmd = [
            str(launch_target.executable),
            *([str(launch_target.main_js)] if launch_target.main_js is not None else []),
            f"--remote-debugging-port={debug_port}",
            # A fresh, throwaway profile per launch unless the caller keeps one.
            # Electron's single-instance lock is keyed on the user-data-dir;
            # isolating it guarantees a relaunch (after a prior attempt was
            # SIGKILLed) cannot fail ``requestSingleInstanceLock()`` against a
            # lock a surviving child of the previous attempt might still hold.
            f"--user-data-dir={launch_user_data_dir}",
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
            *launch_target.extra_arguments,
        ]
        logger.info("Launching Electron: {}", " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            cwd=str(host_config_dir or _REPO_ROOT),
            env=_build_electron_env(workspace_git_url, extra_env, base_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Own session/process group so teardown can signal the whole tree.
            start_new_session=True,
        )
        _stream_electron_output(process)
        try:
            yield process
        finally:
            _terminate_electron_process_tree(process)


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
            response = httpx2.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"status={response.status_code}"
        except (httpx2.HTTPError, OSError) as exc:
            last_error = repr(exc)
        threading.Event().wait(timeout=0.5)
    raise TimeoutError(f"CDP at port {debug_port} did not respond within {timeout_seconds}s (last: {last_error})")


class _ElectronConnectError(RuntimeError):
    """Raised when launching Electron or attaching Playwright to its CDP endpoint fails.

    Signals a wedged Electron launch (the connect-and-attach phase), which the
    caller recovers by relaunching a fresh Electron process -- as opposed to a
    failure while driving the create flow, which is a real test failure and must
    propagate.
    """


def _pick_content_page(browser: Browser, timeout_seconds: int) -> Page:
    """Return the Electron window page that serves the chrome UI.

    Each window is a single web context now (the chrome page, which hosts
    hub pages, the workspace iframe, and the in-DOM modals), so the right
    page is simply the one on the backend origin -- including the
    ``/workspace/<id>`` route, which IS the top-level page while a workspace
    is displayed. We poll until that page exists because Electron spawns the
    backend asynchronously after launch (the window sits on the file://
    shell.html loading screen until then).
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
                logger.info("Picked Electron content page at {}", url)
                return page
        threading.Event().wait(timeout=0.5)
    raise TimeoutError(
        f"No Electron content page settled on a backend URL within {timeout_seconds}s; observed pages: {last_observed}"
    )


def _connect_and_pick_content_page(
    playwright: Playwright, debug_port: int, timeout_seconds: int
) -> tuple[Browser, Page]:
    """Attach to Electron's CDP endpoint and return ``(browser, content_page)``.

    Wraps ``connect_over_cdp`` + :func:`_pick_content_page` in short rounds
    with a FRESH connection per round: a cross-origin main-frame navigation
    that lands while a session is already attached (the chrome view's
    file://shell.html -> http://<backend>/ boot handoff process-swaps the
    renderer) can leave the connected client's page object frozen on the
    pre-swap URL forever, while a fresh connection enumerates targets with
    their current URLs. The caller owns (and must close) the returned browser.

    Raises ``PlaywrightError`` / ``TimeoutError`` exactly like its two halves,
    so callers' launch-flake handling is unchanged.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        browser = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{debug_port}", timeout=_CDP_CONNECT_TIMEOUT_MS
        )
        remaining = deadline - time.monotonic()
        round_seconds = min(_PICK_ROUND_SECONDS, max(1, int(remaining)))
        try:
            return browser, _pick_content_page(browser, round_seconds)
        except TimeoutError:
            browser.close()
            if remaining <= _PICK_ROUND_SECONDS:
                raise
            logger.info(
                "No backend page visible after a {}s round; reconnecting for a fresh target snapshot", round_seconds
            )
    raise TimeoutError(f"No Electron content page settled on a backend URL within {timeout_seconds}s")


def backend_origin_from_page(page: Page) -> str:
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

    Handles both the prefilled-via-env-var case (when the opt-in
    ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is set) and the blank-form case
    (a normal launch where ``_operator_workspace_default`` falls back to the
    hardcoded defaults).
    """
    current_value = page.input_value(selector)
    if current_value == expected_value:
        logger.debug("Field {} already has expected value {!r}", selector, expected_value)
        return
    logger.info("Typing {!r} into {}", expected_value, selector)
    page.fill(selector, expected_value)


# The failure view a failed create draws, and the ids named from it: its See
# details toggle and the panel that toggle opens (CreatingPage.ts failureTurn,
# which names them the way every error names its details).
_FAILURE_VIEW_ID: Final[str] = "failure-view"
_FAILURE_DETAILS_ID: Final[str] = f"{_FAILURE_VIEW_ID}-details"


class WorkspaceCreateAttemptFailedError(RuntimeError):
    """Raised when the Electron create flow surfaces its failure view.

    Carries the sentence Imbue Studio rendered into the loading screen's
    ``#error-message`` element and the raw cause behind its See details
    so a create attempt failure fails the run *fast* with the real cause, instead
    of blocking until the full create-form navigation budget elapses. The
    silent-hang this prevents is what turned a one-line "unknown runtime
    'runsc'" docker error into an opaque 10-minute Playwright timeout.
    """


class WelcomeChatSeedFailedError(RuntimeError):
    """Raised when the desktop client answers the new workspace's welcome-chat seed with a failure."""


def _read_failure_message(page: Page) -> str:
    """Return the failure view's sentence ('#error-message') and the raw cause behind its See details.

    The sentence is the creation page's own words; what ``mngr create`` actually
    reported is kept behind the failure's collapsed See details, so it is opened
    and read too.
    """
    message_element = page.query_selector("#error-message")
    if message_element is None:
        return "unknown error: the '#error-message' element was not present"
    message = message_element.inner_text().strip() or "unknown error: the '#error-message' element was empty"
    details_trigger = page.query_selector(f"#{_FAILURE_DETAILS_ID}")
    if details_trigger is None:
        return message
    details_trigger.click()
    # The panel is drawn only once the page redraws after the click, so wait for it.
    try:
        details_panel = page.wait_for_selector(f"#{_FAILURE_DETAILS_ID}-panel", state="attached", timeout=5_000)
    except PlaywrightTimeoutError:
        logger.warning("The create failure's See details panel did not open; reporting its sentence alone")
        return message
    details = details_panel.inner_text().strip() if details_panel is not None else ""
    return f"{message}\n{details}" if details else message


def wait_for_workspace_ready_or_failure(browser: Browser, creating_page: Page, timeout_seconds: int) -> Frame:
    """Block until the create flow reaches the workspace or reports failure; return the workspace frame.

    The create flow has two mutually exclusive terminal states after the create
    form is submitted:

    - **success**: the ready workspace opens inside the chrome page's sandboxed
      content iframe on the ``agent-<id>.localhost`` origin. The SPA creating page
      extracts the workspace coordinate from the ready workspace's ``/goto``
      URL and enters it in-app on the ``/workspace/<id>`` route, arming the
      iframe. This scans every page's FRAMES for the one that reached the
      agent subdomain (the workspace is a cross-origin iframe now, not its own
      CDP page).
    - **failure**: the loading screen's failure sub-view (``#failure-view``)
      becomes visible on ``creating_page`` (still showing the ``/creating``
      loader) -- the SPA creating page un-hides it once the status
      poll/SSE reports FAILED.

    Polls both rather than only waiting for success, so a create attempt failure raises
    ``WorkspaceCreateAttemptFailedError`` with the surfaced error text immediately
    instead of hanging until ``timeout_seconds`` expires. Returns the workspace
    ``Frame``; raises ``PlaywrightTimeoutError`` if neither state is reached
    within the budget.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        workspace_frame = _find_workspace_frame(browser)
        if workspace_frame is not None:
            return workspace_frame
        try:
            failure_is_visible = creating_page.is_visible(f"#{_FAILURE_VIEW_ID}")
        except PlaywrightError:
            # The page routes onto /workspace/<id> the instant the shell
            # shows the workspace, which can destroy the execution context
            # mid-check; loop so the next iteration re-scans the frames
            # and finds the workspace iframe on the agent subdomain.
            failure_is_visible = False
        if failure_is_visible:
            raise WorkspaceCreateAttemptFailedError(
                f"Workspace create attempt failed: {_read_failure_message(creating_page)}"
            )
        creating_page.wait_for_timeout(_CREATE_OUTCOME_POLL_INTERVAL_MS)
    raise PlaywrightTimeoutError(
        f"Workspace neither became ready nor reported failure within {timeout_seconds}s "
        f"(creating page last URL: {creating_page.url!r})"
    )


def _find_workspace_frame_in_page(page: Page) -> Frame | None:
    """The frame of ``page`` on a workspace's own origin, or None while the page frames none."""
    for frame in page.frames:
        if _AGENT_SUBDOMAIN_PATTERN.search(frame.url):
            return frame
    return None


def _find_workspace_frame(browser: Browser) -> Frame | None:
    """The frame on a workspace's own origin in any of the app's pages, or None while no page frames one."""
    for context in browser.contexts:
        for candidate in context.pages:
            workspace_frame = _find_workspace_frame_in_page(candidate)
            if workspace_frame is not None:
                return workspace_frame
    return None


def _is_welcome_chat_seed_answer(response: Response) -> bool:
    return (
        response.request.method == "POST"
        and _WELCOME_CHAT_HAND_OVER_PATH_PATTERN.search(urlsplit(response.url).path) is not None
        and response.status != _WELCOME_CHAT_HELD_STATUS
    )


def _wait_for_welcome_chat_seed(page: Page, responses_so_far: Sequence[Response], timeout_seconds: float) -> None:
    """Block until the desktop client answers the new workspace's welcome-chat seed; raise if it failed.

    The workspace is entered while its welcome chat is still being seeded, so the workspace being on
    screen says nothing about the seed. A caller that tears the app down at that point kills the
    seed's ``mngr`` call and leaves a half-seeded chat in the workspace.

    ``responses_so_far`` are the responses ``page`` received from the create form's submission on: the
    seed can be answered before the desktop renders.
    """
    answer: Response | None = next(
        (response for response in responses_so_far if _is_welcome_chat_seed_answer(response)), None
    )
    if answer is None:
        answer = page.wait_for_event(
            "response", predicate=_is_welcome_chat_seed_answer, timeout=timeout_seconds * 1000
        )
    if answer.status != _WELCOME_CHAT_SEEDED_STATUS:
        raise WelcomeChatSeedFailedError(
            f"The new workspace's welcome chat was not seeded ({answer.status}): {answer.text()}"
        )


@contextmanager
def _page_listener(
    page: Page,
    # Literals rather than an enum: Playwright types ``Page.on`` per literal event name, which an enum's value is not.
    event: Literal["dialog", "response"],
    listener: Callable[[Any], None],
) -> Generator[None, None, None]:
    """Have ``listener`` hear every ``event`` ``page`` raises inside the block, and none after it.

    Playwright tags each listener with an attribute, so ``listener`` has to be able to take one:
    a model's method can, a builtin's (``list.append``) cannot.
    """
    page.on(event, listener)
    try:
        yield
    finally:
        page.remove_listener(event, listener)


class _ResponseLog(MutableModel):
    """The responses a page received while ``record`` listened for them."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responses: list[Response] = Field(default_factory=list, description="The responses, in the order they arrived.")

    def record(self, response: Response) -> None:
        self.responses.append(response)


def drive_create_flow(
    browser: Browser,
    page: Page,
    default_workspace_template_path: Path,
    workspace_name: str,
    launch_mode: str = "DOCKER",
    region: str | None = None,
) -> Frame:
    """Drive the create form to a ready workspace whose welcome chat is seeded; return the workspace frame.

    ``page`` is the chrome page the form is driven on; the ready workspace
    opens inside that page's sandboxed content iframe, whose frame this
    returns (see ``wait_for_workspace_ready_or_failure``).

    Runs exactly once per successful Electron attach; any failure here is a real
    test failure (not a wedged-launch flake) and propagates to fail the test.

    ``launch_mode`` selects the compute provider in the create form (DOCKER,
    LIMA, AWS, ...). ``region`` selects the machine region for region-aware
    modes (aws/vultr/imbue_cloud); it is required by the form for those modes
    and ignored (the row is hidden) for others.

    There is no AI-provider or API-key field: workspaces boot unauthenticated
    and sign in through the workspace's own provider chooser afterwards.
    """
    backend_origin = backend_origin_from_page(page)
    logger.info("Backend origin: {}", backend_origin)

    logger.info("Navigating to /create")
    page.goto(f"{backend_origin}/create", wait_until="domcontentloaded")
    page.wait_for_selector("#create-form", state="attached", timeout=10_000)

    # The form defaults to the "Imbue Cloud" preset (cloud compute / backup)
    # for everyone, including signed-out users. Submitting with a cloud provider
    # but no account opens the sign-in modal instead of creating, so pick the
    # "local" preset card first: it switches the backup provider to the
    # non-cloud set (the compute mode is overridden below).
    page.click('[data-preset="local"]')

    # The repo field, the workspace-name field, and the compute-provider
    # controls all live in the create form's advanced configuration view,
    # which is collapsed by default. Open it via the single "Advanced
    # Configuration" toggle so those fields are visible (mirroring what a
    # user setting a non-default repo would do).
    page.wait_for_selector("#toggle-advanced:visible", timeout=5_000)
    page.click("#toggle-advanced")
    page.wait_for_selector("#git_url:visible", timeout=5_000)

    _ensure_field_value(page, "#host_name", workspace_name)
    _ensure_field_value(page, "#git_url", str(default_workspace_template_path))
    # Select the requested compute provider. With no account selected the
    # form defaults to LIMA; CI's local-Docker test pins DOCKER. The select
    # lives in the (now-open) advanced configuration view.
    page.select_option("#launch_mode", launch_mode)
    # Region-aware modes (aws/vultr/imbue_cloud) reveal a region select
    # that must carry a value; the JS shows the row on the launch_mode
    # change event, so wait for it before selecting.
    if region is not None:
        page.wait_for_selector("#region:visible", timeout=5_000)
        page.select_option("#region", region)

    response_log = _ResponseLog()
    with _page_listener(page, "response", response_log.record):
        logger.info("Submitting create form")
        page.click("#create-submit")

        # Submitting starts create attempt in the background and lands on the
        # creating/loading page, which streams progress and redirects into
        # the workspace once the create attempt completes.
        page.wait_for_selector("#creating", state="attached", timeout=10_000)

        # Race the workspace-ready content page against the create flow's failure
        # view, so a `mngr create` failure (e.g. an unregistered docker runtime)
        # fails this run fast with the surfaced error rather than blocking the whole
        # navigation budget. The ready workspace opens inside the chrome page's
        # content iframe once the page routes onto /workspace/<id>.
        workspace_page = wait_for_workspace_ready_or_failure(browser, page, _CREATE_FORM_TIMEOUT_SECONDS)
        logger.info("Machine ready at {}", workspace_page.url)

        workspace_page.wait_for_selector(
            DESKTOP_BACKDROP_SELECTOR,
            state="visible",
            timeout=_SYSTEM_INTERFACE_TIMEOUT_SECONDS * 1000,
        )
        logger.info("system_interface desktop rendered; waiting for the welcome chat's seed")
        _wait_for_welcome_chat_seed(page, response_log.responses, _WELCOME_CHAT_SEED_TIMEOUT_SECONDS)
    logger.info("Welcome chat seeded; machine create attempt complete")
    return workspace_page


def _attach_renderer_diagnostics(page: Page) -> None:
    """Stream the Electron renderer's console output, JS errors, and failed
    requests to loguru.

    Electron's stderr only carries main-process output, so a renderer-side
    fault (e.g. the creating page's script throwing before it attaches its handlers, or
    failing to load) is otherwise invisible in CI. Mirroring those events into
    the run log makes a stuck create step diagnosable.
    """
    page.on("console", lambda message: logger.debug("[renderer console:{}] {}", message.type, message.text))
    page.on("pageerror", lambda error: logger.warning("[renderer pageerror] {}", error))
    # A frame that navigates in place keeps its handle but loses its document, which a wait on it
    # then never finds; naming each navigation is what tells that apart from a page that is slow.
    page.on("framenavigated", lambda frame: logger.info("[renderer framenavigated] {}", frame.url))
    page.on("framedetached", lambda frame: logger.info("[renderer framedetached] {}", frame.url))
    page.on(
        "requestfailed",
        lambda request: logger.warning("[renderer requestfailed] {} ({})", request.url, request.failure),
    )


def _attempt_create_workspace_via_electron(
    default_workspace_template_path: Path,
    workspace_name: str,
    debug_port: int,
    host_config_dir: Path | None,
    launch_mode: str = "DOCKER",
    region: str | None = None,
    on_workspace_ready: Callable[[Frame], None] | None = None,
) -> None:
    """One Electron launch + CDP attach + create-flow drive.

    Raises :class:`_ElectronConnectError` if the launch/CDP-attach phase fails
    (a wedged Electron the caller should recover by relaunching). Errors from the
    create flow itself propagate unchanged so real test failures are not retried.

    ``on_workspace_ready``, if given, is called with the workspace (content-iframe)
    page once the workspace's ``system_interface`` has rendered and its welcome
    chat is seeded, while the browser is still connected (e.g. to send a chat
    message). Its exceptions propagate unchanged -- they are real failures, not
    launch flakes, so they are not retried.
    """
    with _launched_electron(default_workspace_template_path, debug_port, host_config_dir):
        with sync_playwright() as playwright:
            try:
                _wait_for_cdp(debug_port, _CDP_READY_TIMEOUT_SECONDS)
                browser, page = _connect_and_pick_content_page(playwright, debug_port, _BACKEND_READY_TIMEOUT_SECONDS)
            except (PlaywrightError, TimeoutError) as exc:
                raise _ElectronConnectError(f"Electron CDP attach failed on port {debug_port}: {exc}") from exc
            # Always disconnect the browser once it is open, regardless of which
            # phase fails: a create-flow failure is a real test failure that
            # must propagate (the attach phase above is the launch-flake part).
            try:
                # Surface renderer console/JS errors into the run log so a stuck
                # create step (creating-page handlers not attaching) is diagnosable.
                _attach_renderer_diagnostics(page)
                workspace_page = drive_create_flow(
                    browser,
                    page,
                    default_workspace_template_path,
                    workspace_name,
                    launch_mode=launch_mode,
                    region=region,
                )
                if on_workspace_ready is not None:
                    on_workspace_ready(workspace_page)
            finally:
                browser.close()


@contextmanager
def electron_app_session(
    workspace_git_url: Path | None,
    debug_port: int,
    host_config_dir: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    user_data_dir: Path | None = None,
    launch_target: ElectronLaunchTarget = DEV_CHECKOUT_LAUNCH_TARGET,
    base_env: Mapping[str, str] | None = None,
) -> Generator[tuple[Browser, Page], None, None]:
    """Launch Electron + attach Playwright and yield ``(browser, content_page)``.

    The generic sibling of :func:`create_workspace_via_electron` for flows that
    drive arbitrary app pages (sign-in, settings, the landing list) instead of
    the create form. The launch + CDP attach is retried with a fresh Electron
    process and port up to ``_ELECTRON_LAUNCH_ATTEMPTS`` times (the same
    wedged-handshake flake recovery); once the session is yielded, caller
    exceptions propagate unchanged and tear the app down.

    The same caller contract as :func:`create_workspace_via_electron` applies
    (``MINDS_ROOT_NAME`` set, ``debug_port`` free, ``host_config_dir`` for the
    pytest config guard). ``workspace_git_url`` only seeds the create form's
    repo prefill; ``None`` leaves the form on the app's published defaults.
    ``extra_env``, ``user_data_dir``, ``launch_target`` and ``base_env`` are
    handed to every launch (see :func:`_launched_electron`).
    """
    last_error: _ElectronConnectError | None = None
    for attempt in range(1, _ELECTRON_LAUNCH_ATTEMPTS + 1):
        attempt_port = debug_port if attempt == 1 else find_free_port()
        with _launched_electron(
            workspace_git_url,
            attempt_port,
            host_config_dir,
            extra_env=extra_env,
            user_data_dir=user_data_dir,
            launch_target=launch_target,
            base_env=base_env,
        ):
            with sync_playwright() as playwright:
                try:
                    _wait_for_cdp(attempt_port, _CDP_READY_TIMEOUT_SECONDS)
                    browser, page = _connect_and_pick_content_page(
                        playwright, attempt_port, _BACKEND_READY_TIMEOUT_SECONDS
                    )
                except (PlaywrightError, TimeoutError) as exc:
                    last_error = _ElectronConnectError(f"Electron CDP attach failed on port {attempt_port}: {exc}")
                    logger.warning(
                        "Electron launch/CDP attempt {}/{} failed; relaunching: {}",
                        attempt,
                        _ELECTRON_LAUNCH_ATTEMPTS,
                        last_error,
                    )
                    continue
                try:
                    _attach_renderer_diagnostics(page)
                    yield browser, page
                    return
                finally:
                    browser.close()
    raise PlaywrightTimeoutError(
        f"Electron CDP attach failed after {_ELECTRON_LAUNCH_ATTEMPTS} relaunch attempts (last error: {last_error})"
    )


def create_workspace_via_electron(
    default_workspace_template_path: Path,
    workspace_name: str,
    debug_port: int,
    host_config_dir: Path | None = None,
    launch_mode: str = "DOCKER",
    region: str | None = None,
    on_workspace_ready: Callable[[Frame], None] | None = None,
) -> None:
    """Drive Electron to create a workspace from ``default_workspace_template_path``.

    ``launch_mode`` selects the compute provider in the create form (DOCKER,
    LIMA, AWS, ...). ``region`` selects the machine region for region-aware
    modes (aws/vultr/imbue_cloud); it is required by the form for those modes
    and ignored (the row is hidden) for others.

    ``on_workspace_ready`` is called with the workspace (content-view) page once
    the workspace has rendered and its welcome chat is seeded, before teardown --
    e.g. to send a chat message and await the reply on the same Electron session.

    Returns once the workspace's ``system_interface`` desktop has
    rendered through the desktop client proxy and its welcome chat is seeded,
    so the caller may close the app at once. Does NOT clean up the
    resulting mngr agent or its Docker container -- the caller decides
    whether to destroy or to capture the state.

    Retries the Electron launch + CDP attach (with a fresh process + debug port)
    up to ``_ELECTRON_LAUNCH_ATTEMPTS`` times to absorb a wedged Electron CDP
    handshake (a known Electron-in-CI flake); the create flow itself runs once,
    so a genuine create attempt failure fails the test immediately.

    Caller contract:
    - ``default_workspace_template_path`` must be a populated DEFAULT_WORKSPACE_TEMPLATE working tree (use
      :func:`resolve_default_workspace_template_path`).
    - ``workspace_name`` must be unique within the current mngr install.
    - ``debug_port`` must be an unused TCP port (use :func:`find_free_port`).
    - ``MINDS_ROOT_NAME`` must already be set in ``os.environ`` (call
      :func:`ensure_minds_env_defaults` first or activate a minds env).
    - ``host_config_dir`` is the cwd for the Electron process (see
      :func:`_launched_electron`); leave unset outside pytest.
    """
    last_error: _ElectronConnectError | None = None
    for attempt in range(1, _ELECTRON_LAUNCH_ATTEMPTS + 1):
        # Reuse the caller-provided port on the first try; allocate a fresh one
        # for each relaunch so a leftover socket from a wedged process can't clash.
        attempt_port = debug_port if attempt == 1 else find_free_port()
        try:
            _attempt_create_workspace_via_electron(
                default_workspace_template_path,
                workspace_name,
                attempt_port,
                host_config_dir,
                launch_mode=launch_mode,
                region=region,
                on_workspace_ready=on_workspace_ready,
            )
            return
        except _ElectronConnectError as exc:
            last_error = exc
            logger.warning(
                "Electron launch/CDP attempt {}/{} failed; relaunching: {}",
                attempt,
                _ELECTRON_LAUNCH_ATTEMPTS,
                exc,
            )
    raise PlaywrightTimeoutError(
        f"Electron CDP attach failed after {_ELECTRON_LAUNCH_ATTEMPTS} relaunch attempts (last error: {last_error})"
    )


# Helpers that drive a ready workspace's desktop, once the create primitives
# above have opened it.

_SCREENSHOT_DIR: Final[Path] = Path("/tmp/minds-electron-e2e")
CHAT_INPUT_SELECTOR: Final[str] = "textarea.message-input-textbox"
# A sent message as the chat page shows it: the optimistic pending bubble, or the committed user turn.
USER_TURN_SELECTOR: Final[str] = ".pending-message, .message.message-user"
# A chat renders inside its own frame at the chat app's origin: the page's URL path is the
# chat's agent id, which is how the frame is found among the workspace frame's descendants (the
# desktop frames the chat app's root, and the root frames the chat) and how one open chat is told
# from another.
CHAT_PAGE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"/(agent-[0-9a-f]+)/?$")
_CHAT_FRAME_POLL_INTERVAL_MS: Final[int] = 500
# A fresh workspace shows the welcome chat the creation page seeds; the desktop's launcher
# (the menu behind the taskbar's field) carries one row per app launch path, each marked
# ``data-launch="<app>:<launch>"``. The chat's ``new`` is the menu's primary free-text row, which
# with nothing typed runs the launch path with no text and starts an empty chat in the chat's
# pinned window. The shell renders the rows from its app list once that has arrived, so the
# rows can still be on their way when the backdrop is first visible.
_LAUNCHER_FIELD_SELECTOR: Final[str] = "[data-launcher-field]"
_LAUNCHER_INPUT_SELECTOR: Final[str] = "[data-launcher-field] textarea"
_LAUNCHER_OVERLAY_SELECTOR: Final[str] = "[data-launcher-overlay]"
NEW_CHAT_ROW_SELECTOR: Final[str] = '[data-launch="chat:new"]'
# The chat's ``main`` launch path, "Send to main chat".
MAIN_CHAT_ROW_SELECTOR: Final[str] = '[data-launch="chat:main"]'
NEW_TERMINAL_ROW_SELECTOR: Final[str] = '[data-launch="terminal:new"]'
_LAUNCHER_ROW_TIMEOUT_SECONDS: Final[int] = 60
# Terminal windows' pages are cross-origin iframes at the terminal service's own
# origin (service-per-origin): the terminal's origin label is ``terminal-<rand>``
# (a random per-service suffix), so the origin is
# https://terminal-<rand>.agent-<hex>.localhost:<port>/. Match the ``terminal-``
# label prefix -- the trailing hyphen keeps it from matching an unrelated
# service whose name merely starts with "terminal".
TERMINAL_IFRAME_SELECTOR: Final[str] = 'iframe[src^="https://terminal-"], iframe[src^="http://terminal-"]'
# The welcome chat's composer is on its page before any agent exists, but the workspace is
# entered before that chat is seeded and the chat app frames it only once it is (as late as the
# seed's own timeout), the shell frames it only once its app list has arrived, and a chat minted
# from a row is created asynchronously (a sign-in through its provider chooser launches it), so a
# chat input can take a while to appear on a fresh first boot.
_CHAT_INPUT_TIMEOUT_SECONDS: Final[int] = 240
_CHAT_REPLY_TIMEOUT_SECONDS: Final[int] = 240


class WorkspaceFlowError(RuntimeError):
    """Raised when a step driving the Electron workspace does not reach its expected state."""


def _debug_screenshot(target: Page | Frame, name: str) -> None:
    """Save a screenshot for post-hoc debugging of an e2e step; never raise.

    Accepts either a Page or a Frame (the workspace surface is a frame of the
    chrome page); a frame's screenshot is taken from its owning page.
    """
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _SCREENSHOT_DIR / f"{name}.png"
        page = target.page if isinstance(target, Frame) else target
        page.screenshot(path=str(path), full_page=False)
        logger.info("Saved screenshot {}", path)
    except (PlaywrightError, OSError) as exc:
        logger.warning("Could not screenshot {}: {!r}", name, exc)


def workspace_coordinate_from_subdomain(url: str) -> str:
    """Extract the workspace coordinate label from a workspace-origin URL.

    New origins carry the workspace id (``agent-<hex>``); a pre-existing
    workspace may still be on the legacy machine-keyed ``host-<hex>`` origin.
    """
    if _AGENT_SUBDOMAIN_PATTERN.match(url) is None:
        raise WorkspaceFlowError(f"Not a workspace-origin URL: {url!r}")
    netloc = url.split("://", 1)[1].split("/", 1)[0]
    for label in netloc.split("."):
        if label.startswith(("host-", "agent-")):
            return label
    raise WorkspaceFlowError(f"No workspace coordinate label in workspace-origin URL: {url!r}")


def descendant_frames(frame: Frame) -> list[Frame]:
    """Every frame under ``frame``, depth first: the desktop frames each app's page, and the chat app's root frames
    its chats, so a chat page is a grandchild of the workspace."""
    found: list[Frame] = []
    for child in frame.child_frames:
        found.append(child)
        found.extend(descendant_frames(child))
    return found


def _chat_frames_with_ids(workspace: Page | Frame) -> list[tuple[Frame, str]]:
    """The chat pages' frames currently inside the workspace, each with its chat's agent id, in frame order.

    The id is the frame's URL path, so it names the same chat however the URL's query string
    or trailing slash changes while the page is open.
    """
    candidates = workspace.frames if isinstance(workspace, Page) else descendant_frames(workspace)
    matched: list[tuple[Frame, str]] = []
    for frame in candidates:
        match = CHAT_PAGE_URL_PATTERN.search(frame.url.split("?", 1)[0])
        if match is not None:
            matched.append((frame, match.group(1)))
    return matched


def find_chat_frame(workspace: Page | Frame, chat_id: str) -> Frame | None:
    """The frame of the page showing chat ``chat_id`` inside the workspace, or None while no page shows it."""
    return next((frame for frame, frame_chat_id in _chat_frames_with_ids(workspace) if frame_chat_id == chat_id), None)


def _find_chat_frame_other_than(workspace: Page | Frame, known_chat_ids: frozenset[str]) -> Frame | None:
    """The first chat page's frame whose chat is not among ``known_chat_ids``, or None while there is none."""
    return next((frame for frame, chat_id in _chat_frames_with_ids(workspace) if chat_id not in known_chat_ids), None)


def _chat_frame_other_than(workspace: Page | Frame, known_chat_ids: frozenset[str], timeout_seconds: float) -> Frame:
    """The frame of a chat page whose chat is not among ``known_chat_ids``, once the shell has framed one.

    Raises WorkspaceFlowError when no such frame appears within ``timeout_seconds``.
    """
    deadline = time.monotonic() + timeout_seconds
    chat = _find_chat_frame_other_than(workspace, known_chat_ids)
    while chat is None and time.monotonic() < deadline:
        workspace.wait_for_timeout(_CHAT_FRAME_POLL_INTERVAL_MS)
        chat = _find_chat_frame_other_than(workspace, known_chat_ids)
    if chat is None:
        which = "" if len(known_chat_ids) == 0 else f" other than the {len(known_chat_ids)} already open"
        raise WorkspaceFlowError(f"No chat frame{which} opened inside the workspace within {timeout_seconds:.0f}s")
    return chat


def chat_frame(workspace: Page | Frame, timeout_seconds: float) -> Frame:
    """The first chat page's frame inside the workspace, once the workspace has opened one.

    A fresh workspace shows the welcome chat the creation page seeds once it is
    seeded, so that is the frame this returns -- and it returns before the
    composer renders, which is what a caller that has to sign the chat in first needs.

    Raises WorkspaceFlowError when no chat frame appears within ``timeout_seconds``.
    """
    return _chat_frame_other_than(workspace, frozenset(), timeout_seconds)


def send_to_main_chat_from_launcher(workspace: Page | Frame, text: str) -> None:
    """Type ``text`` into the taskbar's launcher field and press Enter, which sends it to the workspace's main chat.

    Focusing the field opens the launcher; Enter runs the highlighted row, so the press waits until that is the
    main chat's row.
    """
    workspace.fill(_LAUNCHER_INPUT_SELECTOR, text)
    workspace.wait_for_selector(
        f'{visible_launcher_row_selector(MAIN_CHAT_ROW_SELECTOR)}[data-highlighted="true"]',
        state="visible",
        timeout=_LAUNCHER_ROW_TIMEOUT_SECONDS * 1000,
    )
    workspace.press(_LAUNCHER_INPUT_SELECTOR, "Enter")
    logger.info("Sent a text to the main chat from the launcher")


# The welcome chat's window in the desktop shell: the chat app's pinned window, the one
# its auto-open restores. Every other window a fresh workspace opens is unpinned.
_CHAT_WINDOW: Final[str] = '[data-window-id][data-pinned="true"]'


def _raise_chat_window(workspace: Page | Frame, *, label: str) -> None:
    """Focus the welcome chat's window so pointer clicks reach its page.

    The shell shields every window but the focused one and makes the pages under the
    shield ``pointer-events: none``, so a click aimed into an unfocused window's page is
    intercepted by ``[data-window-shield]`` and Playwright never dispatches the press
    that would raise it. A fresh workspace's welcome chat opens alone and focused, so
    the chat is normally unshielded and this returns at once; the shell shields it when
    another window is shown above it or a menu or the launcher is open, and then the
    shell's own two-step raises it: press the shield, then act on the page.
    """
    shield = workspace.query_selector(f"{_CHAT_WINDOW} [data-window-shield]")
    if shield is None:
        return
    logger.info("[{}] the welcome chat's window is shielded; pressing the shield to raise it", label)
    shield.click()
    workspace.wait_for_selector(f'{_CHAT_WINDOW}[data-focused="true"]', timeout=10_000)


def _describe_chat_frame(chat: Frame) -> str:
    """The chat frame's URL and the start of its document's text, for a failure that found nothing in it."""
    try:
        body = chat.evaluate(
            "() => document.title + ' | ' + (document.body ? document.body.innerText : '').slice(0, 400)"
        )
    except PlaywrightError as exc:
        body = f"<unreadable: {exc}>"
    return f"url={chat.url} detached={chat.is_detached()} document={body!r}"


def _click_in_chat(workspace: Page | Frame, chat: Frame, selector: str, *, label: str) -> None:
    """Click ``selector`` in the chat's page, raising its window first if a shield takes the press."""
    try:
        chat.click(selector, timeout=10_000)
    except PlaywrightTimeoutError:
        _raise_chat_window(workspace, label=label)
        try:
            chat.click(selector, timeout=20_000)
        except PlaywrightTimeoutError:
            logger.warning(
                "[{}] could not click {} in the chat's page: {}", label, selector, _describe_chat_frame(chat)
            )
            raise


def sign_in_via_provider_chooser(workspace: Page | Frame, chat: Frame, *, api_key: SecretStr, label: str) -> None:
    """Drive the provider chooser in the welcome chat's own frame through the Anthropic API-key path.

    A freshly created workspace has no provider accounts, so its welcome chat (the
    conversation the creation page seeded) disables its composer and opens the chooser in
    that chat's page -- the designed first-boot step. Signing in mints a provider account
    holding the key rather than writing a shared settings block, launches the chat's agent on
    that account, and enables the composer; a message sent before the chat app has reported
    the chat creating waits for the chat and goes to it.

    Every control this clicks is targeted by a ``data-e2e`` attribute rather than copy or a
    tailwind class: this drives the template's dialog from the other repo, so it has to
    survive a wording change or a re-port of the UI.
    """
    logger.info("[{}] waiting for the provider chooser to appear in the welcome chat's frame", label)
    chat.wait_for_selector("[data-e2e=provider-chooser]", timeout=120_000)
    _raise_chat_window(workspace, label=label)
    # Try again shows only once the chooser has stopped asking for its provider list by itself;
    # press it the way a user would.
    chat.wait_for_selector("[data-e2e=lane-anthropic], [data-e2e=retry-providers]", timeout=60_000)
    if chat.query_selector("[data-e2e=lane-anthropic]") is None:
        logger.warning(
            "[{}] the chooser could not load its providers; pressing Try again: {!r}",
            label,
            chat.inner_text("[data-e2e=provider-chooser]"),
        )
        _click_in_chat(workspace, chat, "[data-e2e=retry-providers]", label=label)
    # Anthropic's lane, then its API-key method under "Other ways to sign in" --
    # the lane's primary method is the browser sign-in, which needs a human. When the
    # desktop app relays that sign-in, the chooser waits on the browser, and the other
    # ways in are behind "Try another way".
    _click_in_chat(workspace, chat, "[data-e2e=lane-anthropic]", label=label)
    chat.wait_for_selector("[data-e2e=method-api_key], [data-e2e=sign-in-another-way]", timeout=30_000)
    if chat.query_selector("[data-e2e=method-api_key]") is None:
        _click_in_chat(workspace, chat, "[data-e2e=sign-in-another-way]", label=label)
        chat.wait_for_selector("[data-e2e=method-api_key]", timeout=30_000)
    _click_in_chat(workspace, chat, "[data-e2e=method-api_key]", label=label)
    chat.wait_for_selector("[data-e2e=api-key-input]", timeout=30_000)
    chat.fill("[data-e2e=api-key-input]", api_key.get_secret_value())
    logger.info("[{}] submitting the API key through the chooser", label)
    _click_in_chat(workspace, chat, "[data-e2e=save-key]", label=label)
    # An accepted key closes the chooser by itself, which ends the sign-in. The success screen it
    # can show on the way out is not a step: its Done may be gone before a click lands.
    chat.wait_for_selector("[data-e2e=provider-chooser]", state="detached", timeout=300_000)
    logger.info("[{}] signed in via the chooser", label)


def wait_for_chat_input(page: Page | Frame) -> Frame:
    """The first chat's frame inside the workspace, once its composer is visible."""
    logger.info("Waiting up to {}s for the first chat's frame and its input", _CHAT_INPUT_TIMEOUT_SECONDS)
    input_deadline = time.monotonic() + _CHAT_INPUT_TIMEOUT_SECONDS
    chat = chat_frame(page, _CHAT_INPUT_TIMEOUT_SECONDS)
    # Playwright reads a zero timeout as "wait forever", so the remainder is floored.
    input_wait_seconds = max(input_deadline - time.monotonic(), 1.0)
    logger.info("Chat frame at {}; waiting up to {:.0f}s for its input", chat.url, input_wait_seconds)
    chat.wait_for_selector(CHAT_INPUT_SELECTOR, state="visible", timeout=input_wait_seconds * 1000)
    return chat


def send_chat_message(chat: Frame, page: Page | Frame, token: str) -> None:
    """Type a unique-token prompt into the chat's composer and send it; the user turn must render."""
    prompt = f"Reply with exactly this token and nothing else: {token}"
    chat.fill(CHAT_INPUT_SELECTOR, prompt)
    chat.press(CHAT_INPUT_SELECTOR, "Enter")
    logger.info("Sent chat message with token {}", token)
    # The user turn should render (optimistic pending bubble or a committed user
    # message) almost immediately -- proves the chat round-trips through the proxy.
    try:
        chat.wait_for_selector(USER_TURN_SELECTOR, state="attached", timeout=30_000)
    except PlaywrightTimeoutError:
        logger.warning("The sent message never rendered in the chat's page: {}", _describe_chat_frame(chat))
        raise
    _debug_screenshot(page, "message-sent")


def await_chat_reply(chat: Frame, page: Page | Frame, token: str) -> None:
    """Wait for an agent reply that echoes ``token``."""
    logger.info("Waiting up to {}s for the agent reply to echo the token", _CHAT_REPLY_TIMEOUT_SECONDS)
    chat.wait_for_function(
        """(token) => {
            const list = document.querySelector('.message-list');
            if (!list) return false;
            const assistants = list.querySelectorAll('.message.message-assistant');
            for (const el of assistants) { if (el.innerText && el.innerText.includes(token)) return true; }
            return false;
        }""",
        arg=token,
        timeout=_CHAT_REPLY_TIMEOUT_SECONDS * 1000,
    )
    logger.info("Agent reply echoed the token; chat works end-to-end")
    _debug_screenshot(page, "reply-received")


def launcher_opener_selector(workspace: Page | Frame) -> str:
    """The selector of the taskbar control that opens the launcher in the layout the workspace is showing."""
    # The launcher opens from the taskbar's search field: focusing its text area on a laptop, or
    # pressing the field itself where the compact layout renders it as a bare button. The shell
    # renders the taskbar together with the backdrop the caller has already waited for, so probing
    # for the input is enough to tell the two layouts apart.
    if workspace.query_selector(_LAUNCHER_INPUT_SELECTOR) is not None:
        return _LAUNCHER_INPUT_SELECTOR
    return _LAUNCHER_FIELD_SELECTOR


def visible_launcher_row_selector(row_selector: str) -> str:
    """The row as it is found inside the showing launcher; rows render only inside the menu, so a wait
    scoped this way also confirms the launcher came up."""
    return f"{_LAUNCHER_OVERLAY_SELECTOR}:visible {row_selector}"


# The account's home page (``LandingPage.ts``) lists its workspaces, one row each, with the row's Stop
# or Start control. A refused Stop or Start reports itself in the app's own error dialog (``ErrorDialog.ts``).
_ACCOUNT_HOME_PATH_TEMPLATE: Final[str] = "/account/{account_id}/"
_HOME_ROW_SELECTOR_TEMPLATE: Final[str] = '[data-agent-id="{agent_id}"]'
_STOP_WORKSPACE_CONTROL_SELECTOR: Final[str] = '[aria-label="Stop workspace"]'
_START_WORKSPACE_CONTROL_SELECTOR: Final[str] = '[aria-label="Start workspace"]'
_WORKSPACE_STATE_BADGE_SELECTOR: Final[str] = ".landing-mind-state-badge"
_ERROR_DIALOG_SELECTOR: Final[str] = '[role="alertdialog"]'
_STOPPED_BADGE_LABEL: Final[str] = "Stopped"
_CONFIRM_DIALOG_TYPE: Final[str] = "confirm"
_HOME_ROW_POLL_INTERVAL_MS: Final[int] = 1000
# Inside the row's own padding, clear of the controls at its far end.
_HOME_ROW_PRESS_POSITION: Final[Position] = {"x": 12.0, "y": 12.0}
_HOME_PAGE_READING_JS: Final[
    str
] = """([rowSelector, badgeSelector, stopSelector, startSelector, errorDialogSelector]) => {
    const row = document.querySelector(rowSelector);
    const badge = row === null ? null : row.querySelector(badgeSelector);
    const errorDialog = document.querySelector(errorDialogSelector);
    return {
        row: row === null ? null : {
            badge_label: badge === null ? null : (badge.textContent ?? '').trim(),
            is_stop_offered: row.querySelector(stopSelector) !== null,
            is_start_offered: row.querySelector(startSelector) !== null,
        },
        error_dialog_text: errorDialog === null ? null : (errorDialog.textContent ?? '').replace(/\\s+/g, ' ').trim(),
    };
}"""


class HomeRowState(FrozenModel):
    """What one workspace's row on the home page shows."""

    badge_label: str | None = Field(
        description="The state badge's text, or None while the row shows no badge, as a running machine's does."
    )
    is_stop_offered: bool = Field(description="Whether the row offers its Stop control.")
    is_start_offered: bool = Field(description="Whether the row offers its Start control.")


class _HomePageReading(FrozenModel):
    """What the home page shows of one workspace: its row, and the app's own error dialog while one is up."""

    row: HomeRowState | None = Field(description="The workspace's row, or None while the page shows no such row.")
    error_dialog_text: str | None = Field(description="What the app's error dialog says, or None while none is up.")


@pure
def is_home_row_running(state: HomeRowState) -> bool:
    return state.badge_label is None and state.is_stop_offered


@pure
def is_home_row_stopped(state: HomeRowState) -> bool:
    return state.badge_label == _STOPPED_BADGE_LABEL and state.is_start_offered


class _DialogLog(MutableModel):
    """The native dialogs a page raised while ``accept`` listened for them, each answered yes as it came."""

    confirmed_questions: list[str] = Field(default_factory=list, description="What each confirm dialog asked.")
    # CLEANUP: drop the alerts, and the check that reads them, once the stable and beta channels serve a
    # build from 0.10.4 on: earlier builds report a refused Stop or Start through a native alert.
    alerts: list[str] = Field(default_factory=list, description="What every other dialog said.")

    def accept(self, dialog: Dialog) -> None:
        if dialog.type == _CONFIRM_DIALOG_TYPE:
            self.confirmed_questions.append(dialog.message)
        else:
            self.alerts.append(dialog.message)
        dialog.accept()


@contextmanager
def _accepting_dialogs(page: Page) -> Generator[_DialogLog, None, None]:
    """Answer yes to every dialog ``page`` raises inside the block, keeping what each one said.

    Playwright dismisses a dialog nobody listens for, which answers no to a
    question and discards a failure's text, and that is what a dialog raised
    after the block gets again. Electron also shows each dialog as a native
    message box, which stays on screen after the answer given here and takes
    none of the page's input away from Playwright.
    """
    dialog_log = _DialogLog()
    with _page_listener(page, "dialog", dialog_log.accept):
        yield dialog_log


@pure
def _home_row_selector(agent_id: str) -> str:
    return _HOME_ROW_SELECTOR_TEMPLATE.format(agent_id=agent_id)


def _timeout_ms_until(deadline: float) -> float:
    """The milliseconds left until ``deadline``, floored: Playwright reads a zero timeout as "wait forever"."""
    return max(deadline - time.monotonic(), 1.0) * 1000


def _read_home_page(page: Page, agent_id: str) -> _HomePageReading:
    """What the home page shows of the workspace now.

    A stop has the app move its windows off the stopped workspace, which can
    replace the page under a read; such a read counts as showing nothing, and
    the next one is taken from the page that replaced it.
    """
    try:
        raw_reading = page.evaluate(
            _HOME_PAGE_READING_JS,
            [
                _home_row_selector(agent_id),
                _WORKSPACE_STATE_BADGE_SELECTOR,
                _STOP_WORKSPACE_CONTROL_SELECTOR,
                _START_WORKSPACE_CONTROL_SELECTOR,
                _ERROR_DIALOG_SELECTOR,
            ],
        )
    except PlaywrightError as exc:
        logger.debug("Could not read the home page for {} while the page was changing: {}", agent_id, exc)
        return _HomePageReading(row=None, error_dialog_text=None)
    return _HomePageReading.model_validate(raw_reading)


@pure
def _describe_home_row(state: HomeRowState | None) -> str:
    if state is None:
        return "the home page showed no row for it"
    return (
        f"its row showed the badge {state.badge_label!r} with Stop "
        f"{'on' if state.is_stop_offered else 'not on'} offer and Start "
        f"{'on' if state.is_start_offered else 'not on'} offer"
    )


def _wait_for_home_row(
    page: Page,
    agent_id: str,
    is_reached: Callable[[HomeRowState], bool],
    dialog_log: _DialogLog,
    description: str,
    deadline: float,
) -> None:
    """Poll the workspace's row until ``is_reached`` holds for it.

    Raises WorkspaceFlowError as soon as the page reports a failure, in its
    error dialog or through a native one, and at ``deadline`` with what the
    row last showed.
    """
    reading = _read_home_page(page, agent_id)
    while reading.row is None or not is_reached(reading.row):
        reported_failures = [
            *dialog_log.alerts,
            *([] if reading.error_dialog_text is None else [reading.error_dialog_text]),
        ]
        if reported_failures:
            raise WorkspaceFlowError(
                f"The home page reported a failure before workspace {agent_id} was {description}: "
                f"{' / '.join(reported_failures)}"
            )
        if time.monotonic() >= deadline:
            raise WorkspaceFlowError(
                f"Workspace {agent_id} was not {description} on the home page in time; "
                f"{_describe_home_row(reading.row)}."
            )
        page.wait_for_timeout(_HOME_ROW_POLL_INTERVAL_MS)
        reading = _read_home_page(page, agent_id)


def open_account_home_page(page: Page, backend_origin: str, account_id: str) -> None:
    """Open the home page of ``account_id`` (``local`` is the device), which lists that account's workspaces."""
    home_path = _ACCOUNT_HOME_PATH_TEMPLATE.format(account_id=account_id)
    page.goto(f"{backend_origin}{home_path}", wait_until="domcontentloaded")


def _press_home_row_control(
    page: Page,
    agent_id: str,
    control_selector: str,
    is_ready: Callable[[HomeRowState], bool],
    ready_description: str,
    is_done: Callable[[HomeRowState], bool],
    done_description: str,
    timeout_seconds: float,
) -> None:
    """Press one of the row's controls once the row reads ready for it, agree to what it asks, and wait for the row to read done."""
    deadline = time.monotonic() + timeout_seconds
    with _accepting_dialogs(page) as dialog_log:
        _wait_for_home_row(page, agent_id, is_ready, dialog_log, ready_description, deadline)
        page.click(f"{_home_row_selector(agent_id)} {control_selector}", timeout=_timeout_ms_until(deadline))
        logger.info(
            "Pressed {} for workspace {}; agreed to {}", control_selector, agent_id, dialog_log.confirmed_questions
        )
        _wait_for_home_row(page, agent_id, is_done, dialog_log, done_description, deadline)


def stop_workspace_from_home_page(page: Page, agent_id: str, timeout_seconds: float) -> None:
    """Press Stop on the workspace's row of the home page, agree to its question, and wait for the row to read stopped.

    The row of a machine whose stop goes on after the app's own request returns
    (an Imbue Cloud machine uploading its disks) can read stopped briefly and
    then read "Stopping" again, so a caller that needs the stop finished asks
    whatever holds the machine.
    """
    _press_home_row_control(
        page,
        agent_id,
        _STOP_WORKSPACE_CONTROL_SELECTOR,
        is_home_row_running,
        "running",
        is_home_row_stopped,
        "stopped",
        timeout_seconds,
    )
    logger.info("Stopped workspace {} from the home page", agent_id)


def start_workspace_from_home_page(page: Page, agent_id: str, timeout_seconds: float) -> None:
    """Press Start on the workspace's row of the home page and wait for the row to read running.

    Start is pressed once the row reads stopped, which a machine still
    finishing its stop takes a while to.
    """
    _press_home_row_control(
        page,
        agent_id,
        _START_WORKSPACE_CONTROL_SELECTOR,
        is_home_row_stopped,
        "stopped",
        is_home_row_running,
        "running",
        timeout_seconds,
    )
    logger.info("Started workspace {} from the home page", agent_id)


def _find_rendered_workspace_frame(page: Page) -> Frame | None:
    """The frame of the workspace ``page`` shows, once its desktop's backdrop is visible; None until then."""
    workspace_frame = _find_workspace_frame_in_page(page)
    if workspace_frame is None:
        return None
    try:
        is_rendered = workspace_frame.is_visible(DESKTOP_BACKDROP_SELECTOR)
    except PlaywrightError as exc:
        logger.debug("Could not read the workspace's frame at {} while it was loading: {}", workspace_frame.url, exc)
        return None
    return workspace_frame if is_rendered else None


def enter_workspace_from_home_page(page: Page, agent_id: str, timeout_seconds: float) -> Frame:
    """Press the workspace's row on the home page and wait for its desktop to render; return the workspace's frame.

    The press enters a workspace the app reads as healthy, and sends one it
    still reads as unreachable through its recovery page, which returns to the
    workspace once it answers. The frame is looked for in this page alone, the
    one the row was pressed on, and again on every poll: the app can replace
    it, or its document, while the workspace comes back.
    """
    deadline = time.monotonic() + timeout_seconds
    page.click(_home_row_selector(agent_id), position=_HOME_ROW_PRESS_POSITION, timeout=_timeout_ms_until(deadline))
    workspace_frame = _find_rendered_workspace_frame(page)
    while workspace_frame is None:
        if time.monotonic() >= deadline:
            raise WorkspaceFlowError(
                f"The desktop of workspace {agent_id} did not render within {timeout_seconds:.0f}s of pressing "
                f"its row on the home page; the page was last at {page.url}"
            )
        page.wait_for_timeout(_HOME_ROW_POLL_INTERVAL_MS)
        workspace_frame = _find_rendered_workspace_frame(page)
    logger.info("Entered workspace {} from the home page; its desktop rendered at {}", agent_id, workspace_frame.url)
    return workspace_frame
