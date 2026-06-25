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
import signal
import socket
import subprocess
import sys
import tempfile
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
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.desktop_client.templates import FALLBACK_BRANCH as _FORM_DEFAULT_BRANCH

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
# The modal overlay view loads ``/inbox`` (optionally with ``?selected=<id>``)
# when the inbox modal is shown. Like the chrome views, it lives on the
# backend origin but is not the content view; exclude it so the runner does
# not pick it up if the modal has ever been opened.
_INBOX_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^http://localhost:\d+/inbox(?:/|$|\?)")
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
_CREATE_FORM_TIMEOUT_SECONDS: Final[int] = 600
_SYSTEM_INTERFACE_TIMEOUT_SECONDS: Final[int] = 180
_CREATE_OUTCOME_POLL_INTERVAL_MS: Final[int] = 500

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

    In CI the checkout is a detached HEAD, so ``git rev-parse --abbrev-ref
    HEAD`` returns ``HEAD`` and the branch-matching step would never fire --
    meaning a PR that needs a same-named FCT branch (e.g. one changing the
    mngr<->FCT config contract) could not be tested against it. GitHub Actions
    exposes the real branch in the environment, so consult that first:
    ``GITHUB_HEAD_REF`` is the PR source branch (set only for pull_request
    events); ``GITHUB_REF_NAME`` is the branch for push events (but a
    ``<n>/merge`` ref for PRs, which we ignore).

    Any failure to invoke git (missing ``.git`` -- e.g. when the runner
    executes inside a Modal sandbox whose source tree was uploaded via
    ``add_local_dir`` and the worktree's ``.git`` file points at a
    gitdir that does not exist on the sandbox; ``CalledProcessError``;
    ``TimeoutExpired``) is logged at warning level and treated as
    "branch unknown", which routes the caller through the documented
    fall-back to FCT ``main`` rather than crashing the whole run.
    """
    ci_head_ref = os.environ.get("GITHUB_HEAD_REF")
    if ci_head_ref:
        return ci_head_ref
    ci_ref_name = os.environ.get("GITHUB_REF_NAME")
    if ci_ref_name and not ci_ref_name.endswith("/merge"):
        return ci_ref_name
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
    """Shallow-clone ``branch`` of the FCT public remote into ``destination``.

    Also fetches any release tags into the clone. The minds create form's
    default branch field (see ``FALLBACK_BRANCH`` in templates.py) pins
    to an annotated FCT tag (e.g. ``v0.3.0``); without this extra fetch,
    a depth-1 clone of an unrelated branch does not have the tag's commit,
    and the downstream ``mngr create`` clone of the form's branch field
    would fail with ``Remote branch v0.3.0 not found``. Cheap (a handful
    of extra refs) and keeps test create flows aligned with production.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, _FCT_REMOTE, str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # ``--depth 1`` would only fetch the tag's tip, but ``--tags`` already
    # implies fetching all tag-pointed commits at shallow depth; combine
    # so each tag's target commit is reachable without filling out full
    # branch history.
    subprocess.run(
        ["git", "-C", str(destination), "fetch", "--depth", "1", "--tags", "origin"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The create form pre-fills its branch field with `_FORM_DEFAULT_BRANCH`
    # (templates.py `FALLBACK_BRANCH`), so the spawned `mngr create` runs
    # `git checkout <that ref>` in this very clone. Leaving the clone on
    # the originally-cloned branch turns that into a real checkout that
    # rejects any uncommitted edits the test fixture made to opt files in
    # (e.g. `.mngr/settings.toml is_allowed_in_pytest`). Pre-positioning
    # to the form's default makes that downstream checkout a no-op even
    # when the working tree is dirty. Best effort: if the ref is not
    # reachable (e.g. tag not present on FCT remote yet), leave the clone
    # as-is and let `mngr create` surface the resulting error.
    _checkout_best_effort(destination, _FORM_DEFAULT_BRANCH)
    return destination


def _checkout_best_effort(repo: Path, ref: str) -> None:
    verify = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if verify.returncode != 0:
        logger.info("Skipping pre-checkout of FCT clone to {!r}: ref not reachable", ref)
        return
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--detach", ref],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Point FETCH_HEAD at the same commit we just checked out. The minds create
    # flow runs ``git checkout -B <ref> FETCH_HEAD`` in this clone; with HEAD
    # already on <ref>, making FETCH_HEAD == HEAD turns that into a true no-op
    # that preserves the uncommitted ``is_allowed_in_pytest`` opt-in the test
    # writes into ``.mngr/settings.toml``. Without this, FETCH_HEAD still points
    # at the branch tip left by the earlier ``fetch --tags`` (a different
    # commit, whose ``.mngr/settings.toml`` differs from the tag's), so the
    # downstream checkout tries to switch content and aborts on the dirty file
    # ("Your local changes ... would be overwritten by checkout"). Fetching from
    # ``.`` is local-only (no network).
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "--no-tags", ".", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


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


def materialize_isolated_fct(fct_source: Path, scratch_dir: Path) -> Path:
    """Return a throwaway FCT working tree the caller may safely write into.

    The pytest wrapper writes a ``is_allowed_in_pytest`` opt-in into the
    returned tree's ``.mngr/settings.toml`` before ``mngr create`` mirrors
    it into the workspace container. When ``fct_source`` is the operator's
    ``.external_worktrees/forever-claude-template/`` checkout, that edit
    must not land on the real file, so clone it into ``scratch_dir``
    (committed state) and position it on the create form's default branch
    (matching :func:`_shallow_clone_fct`). When ``fct_source`` is already a
    throwaway clone (steps 2-3 of :func:`resolve_fct_path`), return it
    unchanged.
    """
    if fct_source != _FCT_EXTERNAL_WORKTREE:
        return fct_source
    destination = scratch_dir / "fct_isolated"
    destination.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning FCT external worktree into {} to keep the operator's checkout pristine", destination)
    subprocess.run(
        ["git", "clone", str(fct_source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    _checkout_best_effort(destination, _FORM_DEFAULT_BRANCH)
    return destination


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
    the ``MINDS_WORKSPACE_*`` prefill vars (honored only when the explicit
    opt-in ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is also set -- see
    ``_operator_workspace_default`` in templates.py), and scrubs any
    ANTHROPIC creds the operator's shell might have exported so they
    don't silently leak into every workspace we create.
    """
    env = dict(os.environ)
    env["MINDS_WORKSPACE_GIT_URL"] = str(workspace_git_url)
    env["MINDS_WORKSPACE_NAME"] = workspace_name
    # Opt into the local-worktree create-form defaults (see just minds-start).
    env["MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"] = "1"
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
    if process.poll() is not None:
        return
    try:
        process_group_id = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    _signal_process_group(process_group_id, signal.SIGTERM)
    try:
        process.wait(timeout=_ELECTRON_SIGTERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(
            "Electron did not exit on SIGTERM within {}s; sending SIGKILL",
            _ELECTRON_SIGTERM_GRACE_SECONDS,
        )
        _signal_process_group(process_group_id, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Electron process group did not exit within 5s of SIGKILL")


@contextmanager
def _launched_electron(
    workspace_git_url: Path,
    workspace_name: str,
    debug_port: int,
    host_config_dir: Path | None = None,
) -> Iterator[subprocess.Popen[bytes]]:
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
    intentionally generous (30s) because the minds backend that Electron
    spawns needs a few seconds to drain mngr_forward streams cleanly --
    shorter grace periods routinely escalate to SIGKILL and leave the
    workspace in a half-shutdown state.

    Each launch also gets its own throwaway ``--user-data-dir`` so that,
    even if a prior attempt's teardown was imperfect, this instance never
    collides with a stale single-instance lock from the default profile.

    Note: tearing down Electron does NOT destroy the workspace's mngr
    agent / Docker container. Those persist as separate host-level
    processes; cleanup of them is the caller's responsibility.
    """
    if not _ELECTRON_BINARY.is_file():
        raise FileNotFoundError(
            f"Electron binary missing at {_ELECTRON_BINARY}. Run `cd apps/minds && pnpm install` first."
        )

    with tempfile.TemporaryDirectory(prefix="minds-electron-userdata-") as user_data_dir:
        cmd = [
            str(_ELECTRON_BINARY),
            str(_ELECTRON_MAIN_JS),
            f"--remote-debugging-port={debug_port}",
            # A fresh, throwaway profile per launch. Electron's single-instance
            # lock is keyed on the user-data-dir; isolating it guarantees a
            # relaunch (after a prior attempt was SIGKILLed) cannot fail
            # ``requestSingleInstanceLock()`` against a lock a surviving child
            # of the previous attempt might still hold.
            f"--user-data-dir={user_data_dir}",
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
            cwd=str(host_config_dir or _REPO_ROOT),
            env=_build_electron_env(workspace_git_url, workspace_name),
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
            response = httpx.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"status={response.status_code}"
        except (httpx.HTTPError, OSError) as exc:
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
    """Return the Electron WebContentsView that serves the main content.

    Electron's BaseWindow has multiple WebContentsView's (chrome view,
    content view, sidebar, and a lazy modal overlay view). Each is its
    own CDP page. The content view is the one whose URL is on the
    backend origin and is not one of the chrome-owned surfaces: not
    rooted at ``/_chrome`` (chrome / sidebar) and not the inbox modal
    at ``/inbox``. We poll until that page exists because Electron
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
                if _INBOX_PATH_PATTERN.match(url):
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


def destroy_agent_best_effort(workspace_name: str, config_project_dir: Path | None = None) -> None:
    """Tear down the mngr agent created during a run. Always survives.

    ``mngr destroy`` may legitimately fail (e.g. the run crashed before
    create succeeded, the docker daemon stopped). We log and swallow.

    The pytest test calls this in its ``finally`` so the test never leaks
    an agent into the host. The snapshot script does NOT call it -- the
    whole point of the snapshot is to capture the sandbox with the agent
    alive.

    ``config_project_dir`` is exported as ``MNGR_PROJECT_CONFIG_DIR`` so
    this subprocess loads the same isolated, opted-in config the pytest
    wrapper built, rather than the repo's ``.mngr/`` (which would fail the
    pytest config guard). Leave unset outside pytest.
    """
    cmd = ["uv", "run", "mngr", "destroy", workspace_name, "--force"]
    logger.info("Cleanup: {}", " ".join(cmd))
    env = dict(os.environ)
    if config_project_dir is not None:
        env["MNGR_PROJECT_CONFIG_DIR"] = str(config_project_dir)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
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


class WorkspaceCreationFailedError(RuntimeError):
    """Raised when the Electron create flow surfaces its failure view.

    Carries the human-readable text minds rendered into the loading
    screen's ``#error-message`` element (whatever ``mngr create`` reported)
    so a creation failure fails the run *fast* with the real cause, instead
    of blocking until the full create-form navigation budget elapses. The
    silent-hang this prevents is what turned a one-line "unknown runtime
    'runsc'" docker error into an opaque 10-minute Playwright timeout.
    """


def _read_failure_message(page: Page) -> str:
    """Return the text minds rendered into the failure view's '#error-message' element."""
    message_element = page.query_selector("#error-message")
    if message_element is None:
        return "unknown error: the '#error-message' element was not present"
    message = message_element.inner_text().strip()
    return message or "unknown error: the '#error-message' element was empty"


def _wait_for_workspace_ready_or_failure(page: Page, timeout_seconds: int) -> None:
    """Block until the create flow reaches the workspace or reports failure.

    The minds create flow has two mutually exclusive terminal states after
    the create form is submitted: a redirect to the ``agent-<id>.localhost``
    workspace URL (success), or the loading screen's failure sub-view
    (``#failure-view``) becoming visible (failure -- ``creating.js``'s
    ``showFailure()`` un-hides it once the status poll/SSE reports FAILED).

    Polls both rather than only waiting for the success URL (the old
    behavior), so a creation failure raises ``WorkspaceCreationFailedError``
    with the surfaced error text immediately instead of hanging until
    ``timeout_seconds`` expires. Raises ``PlaywrightTimeoutError`` if neither
    state is reached within the budget.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _AGENT_SUBDOMAIN_PATTERN.search(page.url):
            return
        try:
            failure_is_visible = page.is_visible("#failure-view")
        except PlaywrightError:
            # A redirect to the workspace can destroy the execution context
            # mid-check; loop so the next iteration re-reads page.url, which
            # will match the success pattern and return.
            failure_is_visible = False
        if failure_is_visible:
            raise WorkspaceCreationFailedError(f"Workspace creation failed: {_read_failure_message(page)}")
        page.wait_for_timeout(_CREATE_OUTCOME_POLL_INTERVAL_MS)
    raise PlaywrightTimeoutError(
        f"Workspace neither became ready nor reported failure within {timeout_seconds}s (last URL: {page.url!r})"
    )


def _drive_create_flow(
    page: Page,
    fct_path: Path,
    workspace_name: str,
    launch_mode: str = "DOCKER",
    account_label: str | None = None,
    region: str | None = None,
) -> None:
    """Drive the create form to a ready workspace on an attached page.

    Runs exactly once per successful Electron attach; any failure here is a real
    test failure (not a wedged-launch flake) and propagates to fail the test.

    ``launch_mode`` selects the compute provider in the create form (DOCKER,
    LIMA, AWS, ...). ``account_label`` optionally selects an AI-provider account
    (by visible option text) before submitting. ``region`` selects the machine
    region for region-aware modes (aws/vultr/imbue_cloud); it is required by the
    form for those modes and ignored (the row is hidden) for others.
    """
    backend_origin = _backend_origin_from_page(page)
    logger.info("Backend origin: {}", backend_origin)

    logger.info("Navigating to /create")
    page.goto(f"{backend_origin}/create", wait_until="domcontentloaded")
    page.wait_for_selector("#create-form", state="attached", timeout=10_000)

    # The form defaults to the "Imbue Cloud" preset (cloud compute / AI / backup)
    # for everyone, including signed-out users. Submitting with a cloud provider
    # but no account opens the sign-in modal instead of creating. When this run
    # has no account, pick the "local" preset card first so the AI / backup
    # providers are the non-cloud set (the compute mode is overridden below);
    # account-based modes pass ``account_label`` and keep the cloud defaults.
    if account_label is None:
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
    _ensure_field_value(page, "#git_url", str(fct_path))
    # Optionally select an AI-provider account (by visible label) before
    # picking the compute mode -- some modes/tiers require a real account.
    if account_label is not None:
        page.select_option("#account_id", label=account_label)
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

    logger.info("Submitting create form")
    page.click("#create-submit")

    # Submitting starts creation in the background and lands on the
    # creating/loading page, which streams progress and redirects into
    # the workspace once creation completes.
    page.wait_for_selector("#creating", state="attached", timeout=10_000)

    # Race the workspace-ready redirect against the create flow's
    # failure view, so a `mngr create` failure (e.g. an unregistered
    # docker runtime) fails this run fast with the surfaced error
    # rather than blocking the whole navigation budget.
    _wait_for_workspace_ready_or_failure(page, _CREATE_FORM_TIMEOUT_SECONDS)
    logger.info("Workspace ready at {}", page.url)

    page.wait_for_selector(
        _DOCKVIEW_WORKSPACE_SELECTOR,
        state="visible",
        timeout=_SYSTEM_INTERFACE_TIMEOUT_SECONDS * 1000,
    )
    logger.info("system_interface dockview rendered; workspace creation complete")


def _attempt_create_workspace_via_electron(
    fct_path: Path,
    workspace_name: str,
    debug_port: int,
    host_config_dir: Path | None,
    launch_mode: str = "DOCKER",
    account_label: str | None = None,
    region: str | None = None,
) -> None:
    """One Electron launch + CDP attach + create-flow drive.

    Raises :class:`_ElectronConnectError` if the launch/CDP-attach phase fails
    (a wedged Electron the caller should recover by relaunching). Errors from the
    create flow itself propagate unchanged so real test failures are not retried.
    """
    with _launched_electron(fct_path, workspace_name, debug_port, host_config_dir):
        with sync_playwright() as playwright:
            try:
                _wait_for_cdp(debug_port, _CDP_READY_TIMEOUT_SECONDS)
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{debug_port}", timeout=_CDP_CONNECT_TIMEOUT_MS
                )
            except (PlaywrightError, TimeoutError) as exc:
                raise _ElectronConnectError(f"Electron CDP attach failed on port {debug_port}: {exc}") from exc
            # Always disconnect the browser once it is open, regardless of which
            # phase fails: picking the content page is still part of the attach
            # phase (a wedged-launch flake -> ``_ElectronConnectError``), while a
            # create-flow failure is a real test failure that must propagate.
            try:
                try:
                    page = _pick_content_page(browser, _BACKEND_READY_TIMEOUT_SECONDS)
                except (PlaywrightError, TimeoutError) as exc:
                    raise _ElectronConnectError(f"Electron CDP attach failed on port {debug_port}: {exc}") from exc
                _drive_create_flow(
                    page,
                    fct_path,
                    workspace_name,
                    launch_mode=launch_mode,
                    account_label=account_label,
                    region=region,
                )
            finally:
                browser.close()


def create_workspace_via_electron(
    fct_path: Path,
    workspace_name: str,
    debug_port: int,
    host_config_dir: Path | None = None,
    launch_mode: str = "DOCKER",
    account_label: str | None = None,
    region: str | None = None,
) -> None:
    """Drive Electron to create a workspace from ``fct_path``.

    ``launch_mode`` selects the compute provider in the create form (DOCKER,
    LIMA, AWS, ...). ``account_label`` optionally selects an AI-provider account
    (by visible option text) before submitting. ``region`` selects the machine
    region for region-aware modes (aws/vultr/imbue_cloud); it is required by the
    form for those modes and ignored (the row is hidden) for others.

    Returns once the workspace's ``system_interface`` dockview UI has
    rendered through the desktop client proxy. Does NOT clean up the
    resulting mngr agent or its Docker container -- the caller decides
    whether to destroy or to capture the state.

    Retries the Electron launch + CDP attach (with a fresh process + debug port)
    up to ``_ELECTRON_LAUNCH_ATTEMPTS`` times to absorb a wedged Electron CDP
    handshake (a known Electron-in-CI flake); the create flow itself runs once,
    so a genuine creation failure fails the test immediately.

    Caller contract:
    - ``fct_path`` must be a populated FCT working tree (use
      :func:`resolve_fct_path`).
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
                fct_path,
                workspace_name,
                attempt_port,
                host_config_dir,
                launch_mode=launch_mode,
                account_label=account_label,
                region=region,
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
