import importlib.metadata
import importlib.resources
import os
import shutil
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import Generator
from uuid import uuid4

import docker
import docker.errors
import pluggy
import psutil
import pytest
from loguru import logger
from urwid.widget.listbox import SimpleFocusListWalker

import imbue.mngr.main
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.errors import MngrError
from imbue.mngr.plugin_catalog import get_independent_entry_point_names
from imbue.mngr.plugins import hookspecs
from imbue.mngr.providers.docker.instance import create_docker_client
from imbue.mngr.providers.docker.testing import remove_docker_container_and_volume
from imbue.mngr.providers.docker.volume import LABEL_PROVIDER
from imbue.mngr.providers.docker.volume import STATE_CONTAINER_TYPE_LABEL
from imbue.mngr.providers.docker.volume import STATE_CONTAINER_TYPE_VALUE
from imbue.mngr.providers.registry import load_local_backend_only
from imbue.mngr.providers.registry import reset_backend_registry
from imbue.mngr.utils.deps import CORE_DEPS
from imbue.mngr.utils.env_utils import looks_like_mngr_test_container_name
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.plugin_testing import register_test_placeholder_agent_type
from imbue.mngr.utils.testing import capture_log_warnings
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import worker_docker_state_prefixes
from imbue.mngr.utils.testing import worker_test_ids

# Resource guards (tmux, rsync, unison, modal, docker_cli, docker_sdk) are
# registered automatically via the resource_guards entry point group.

# The urwid import above triggers creation of deprecated module aliases.
# These are the deprecated module aliases that urwid 3.x creates for backwards
# compatibility. They point to the new locations but emit DeprecationWarning
# when any attribute (including __file__) is accessed. By removing them from
# sys.modules, we prevent warnings during pytest/inspect module iteration.
_URWID_DEPRECATED_ALIASES = (
    "urwid.web_display",
    "urwid.lcd_display",
    "urwid.html_fragment",
    "urwid.monitored_list",
    "urwid.listbox",
    "urwid.treetools",
)


def _remove_deprecated_urwid_module_aliases() -> None:
    """Remove deprecated urwid module aliases from sys.modules.

    urwid 3.x maintains backwards compatibility by creating deprecated module
    aliases (e.g., urwid.listbox -> urwid.widget.listbox). These aliases emit
    DeprecationWarning when any attribute is accessed, including __file__.

    When pytest/Python's inspect module iterates over sys.modules during test
    collection, it accesses __file__ on these deprecated aliases, triggering
    many spurious warnings. By removing the aliases from sys.modules after
    urwid is imported, we prevent these warnings without suppressing them.

    This is not suppression - we're removing the problematic module objects
    rather than ignoring warnings they emit.
    """
    for mod in _URWID_DEPRECATED_ALIASES:
        if mod in sys.modules:
            del sys.modules[mod]


# Clean up deprecated urwid aliases immediately after import.
# This needs to happen at module load time, before pytest starts collecting tests.
# We use SimpleFocusListWalker to ensure urwid is fully loaded first.
_ = SimpleFocusListWalker
_remove_deprecated_urwid_module_aliases()


# =============================================================================
# Shared plugin fixtures (single-sourced from plugin_testing.py)
# =============================================================================
#
# Most of mngr's test fixtures (HOME isolation, temp host/profile/config dirs,
# git-repo helpers, the autouse setup_test_mngr_env, the shell-stub fixtures,
# etc.) are defined once in imbue.mngr.utils.plugin_testing and registered here
# via register_plugin_test_fixtures -- the exact same entry point every mngr
# plugin uses. Single-sourcing them this way keeps mngr-core and its plugins
# from drifting apart.
#
# Two fixtures below override the plugin_testing versions. Only one is a genuine
# design difference:
#   - plugin_manager: deliberately diverges -- mngr-core blocks every external
#                     entry-point plugin except those in enabled_plugins, whereas
#                     the plugin-facing version loads all entry points so a
#                     plugin's own hooks are present.
#   - mngr_test_id:   identical to the shared version except it also appends to
#                     worker_test_ids. That list is read only by mngr-core's
#                     session_cleanup leak scan, which isn't shared with plugins,
#                     so the bookkeeping would simply be inert there. This override
#                     is incidental, not principled -- it could go away if that
#                     scan were ever shared with plugins.
# register_plugin_test_fixtures runs first; because these defs come after it,
# they win for the mngr-core test session.

register_plugin_test_fixtures(globals())


@pytest.fixture
def mngr_test_id() -> str:
    """Generate a unique test ID for isolation.

    This ID is used for both the host directory and prefix to ensure
    test isolation and easy cleanup of test resources (e.g., tmux sessions).
    """
    test_id = uuid4().hex
    worker_test_ids.append(test_id)
    return test_id


@pytest.fixture
def disable_remote_providers_for_subprocesses(
    project_config_dir: Path, monkeypatch: pytest.MonkeyPatch, temp_git_repo: Path
) -> Path:
    """Disable the Modal and Docker providers for subprocesses spawned during a test.

    Writes a settings.local.toml inside a temporary git repo's config directory
    and chdir's into that repo. Spawned subprocesses inherit the CWD, so the
    config loader's upward directory walk finds the settings file.

    Use this when a test spawns a child process that runs ``mngr`` commands
    and would otherwise fail because Modal credentials are not available in
    the test environment, or would create Docker state containers that leak.
    """
    settings_path = project_config_dir / "settings.local.toml"
    settings_path.write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    monkeypatch.chdir(temp_git_repo)
    return settings_path


@pytest.fixture
def temp_git_repo_cwd(temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary git repository and chdir into it.

    Combines temp_git_repo with monkeypatch.chdir so tests that need a git
    repo as the working directory (e.g. for project-scope config discovery)
    don't need to request both fixtures separately.
    """
    monkeypatch.chdir(temp_git_repo)
    return temp_git_repo


@pytest.fixture
def active_concurrency_group() -> Generator[ConcurrencyGroup, None, None]:
    """Provide an active ConcurrencyGroup for tests that construct MngrContext directly."""
    with ConcurrencyGroup(name="test") as cg:
        yield cg


@pytest.fixture()
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru warning messages for assertion in tests.

    Delegates to capture_log_warnings() in testing.py (the single source of
    truth shared with plugin_testing.py's identically-named fixture).
    """
    with capture_log_warnings() as messages:
        yield messages


# =============================================================================
# Autouse fixtures
# =============================================================================


_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKSPACE_PACKAGES = (
    _REPO_ROOT / "libs" / "imbue_common",
    _REPO_ROOT / "libs" / "concurrency_group",
    _REPO_ROOT / "libs" / "resource_guards",
    _REPO_ROOT / "libs" / "mngr",
)


@pytest.fixture
def isolated_mngr_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary venv with mngr installed for subprocess-based tests.

    Returns the venv directory. Use ``venv / "bin" / "mngr"`` to run mngr
    commands, or ``venv / "bin" / "python"`` for the interpreter.

    Writes a ``uv-receipt.toml`` so that ``require_uv_tool_receipt()``
    recognises this venv as a uv-tool-managed installation.

    This fixture is useful for tests that install/uninstall packages and
    need full isolation from the main workspace venv.

    To avoid network access (and the flakiness that comes with it), we
    export mngr's pinned deps from the lockfile via ``uv export``, then
    install them with ``--no-deps`` (uses uv cache, no resolution or
    network needed).
    """
    venv_dir = tmp_path / "isolated-venv"

    workspace_install_args: list[str] = []
    for pkg in _WORKSPACE_PACKAGES:
        workspace_install_args.extend(["-e", str(pkg)])

    python_path = str(venv_dir / "bin" / "python")

    # Undo the autouse fixture's UV_OFFLINE/UV_FROZEN so uv can fetch
    # packages into the fresh venv from its local cache.
    monkeypatch.delenv("UV_OFFLINE", raising=False)
    monkeypatch.delenv("UV_FROZEN", raising=False)

    cg = ConcurrencyGroup(name="isolated-venv-setup")
    with cg:
        # Export mngr's pinned transitive deps from the lockfile (no editable/comment lines)
        export_result = cg.run_process_to_completion(
            ("uv", "export", "--package", "imbue-mngr", "--no-hashes", "--frozen"),
            cwd=_REPO_ROOT,
        )
        reqs_file = tmp_path / "pinned-deps.txt"
        reqs_file.write_text(
            "\n".join(
                line for line in export_result.stdout.splitlines() if line and not line.startswith(("#", " ", "-e"))
            )
        )

        cg.run_process_to_completion(("uv", "venv", str(venv_dir)))
        # Install pinned deps from cache (no resolution or network needed)
        cg.run_process_to_completion(
            ("uv", "pip", "install", "--python", python_path, "--no-deps", "-r", str(reqs_file)),
        )
        # Install workspace packages as editable (no-deps since deps are already installed)
        cg.run_process_to_completion(
            ("uv", "pip", "install", "--python", python_path, "--no-deps", *workspace_install_args),
        )

    # Write a uv-receipt.toml so plugin add/remove recognise this as a
    # uv-tool-managed venv (the receipt lives at sys.prefix root).
    receipt_content = (
        '[tool]\nrequirements = [{ name = "mngr" }]\n'
        "entrypoints = [\n"
        f'    {{ name = "mngr", install-path = "{venv_dir / "bin" / "mngr"}", from = "mngr" }},\n'
        "]\n"
    )
    (venv_dir / "uv-receipt.toml").write_text(receipt_content)

    return venv_dir


class MinimalInstallEnv(FrozenModel):
    """A fresh mngr install in an isolated venv, with subprocess env and git repo."""

    venv_dir: Path
    env: dict[str, str]
    repo_dir: Path

    def run_mngr(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run the venv's mngr binary with the given arguments."""
        mngr_bin = str(self.venv_dir / "bin" / "mngr")
        return subprocess.run(
            [mngr_bin, *args],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
            env=self.env,
            timeout=30,
        )

    def run_python(self, script: str) -> subprocess.CompletedProcess[str]:
        """Run a Python script in the isolated venv."""
        python_bin = str(self.venv_dir / "bin" / "python")
        return subprocess.run(
            [python_bin, "-c", script],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
            env=self.env,
            timeout=30,
        )


@pytest.fixture
def minimal_install_env(
    isolated_mngr_venv: Path,
    temp_host_dir: Path,
    mngr_test_root_name: str,
    tmp_path: Path,
) -> MinimalInstallEnv:
    """Provide a fresh mngr install in an isolated venv for install tests.

    The venv is built from the workspace (not the dev venv), so it exercises
    the real install path: entry points, dependency resolution, etc.

    The subprocess environment is intentionally minimal (not inherited from
    the parent process). PATH contains only the venv bin and the directories
    of mngr's declared system dependencies: CORE_DEPS from
    libs/mngr/imbue/mngr/utils/deps.py (git, tmux, jq) plus three
    fixture-specific extras (curl, used by scripts/install.sh to bootstrap
    uv, and rsync + ssh, optional deps included so file-sync and remote
    code paths are exercised). This catches code that depends on tools from
    the developer's environment (e.g. the modal CLI being on PATH).
    """
    # Pull the core binary names from CORE_DEPS so this list cannot drift
    # away from what mngr actually requires. The extras below are
    # fixture-specific (see docstring above).
    system_deps = [dep.binary for dep in CORE_DEPS] + ["curl", "rsync", "ssh"]
    dep_dirs: set[str] = set()
    for dep in system_deps:
        dep_path = shutil.which(dep)
        if dep_path is not None:
            dep_dirs.add(str(Path(dep_path).parent))
    system_path = ":".join(sorted(dep_dirs))

    env = {
        "PATH": f"{isolated_mngr_venv / 'bin'}:{system_path}",
        "HOME": str(temp_host_dir.parent),
        "MNGR_HOST_DIR": str(temp_host_dir),
        "MNGR_ROOT_NAME": mngr_test_root_name,
    }

    repo_dir = tmp_path / "repo"
    init_git_repo(repo_dir)

    return MinimalInstallEnv(venv_dir=isolated_mngr_venv, env=env, repo_dir=repo_dir)


@pytest.fixture
def enabled_plugins() -> frozenset[str]:
    """Return the set of plugin entry point names to enable for this test.

    Defaults to the BASIC-tier plugins (claude, opencode, pi_coding,
    llm, modal, tutor).  Override in test files or local conftest.py
    for different configurations::

        @pytest.fixture
        def enabled_plugins():
            return frozenset()
    """
    return get_independent_entry_point_names()


@pytest.fixture(autouse=True)
def plugin_manager(
    enabled_plugins: frozenset[str],
) -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with all external plugins disabled by default.

    Discovers all entry-point plugins and blocks everything except those
    listed in ``enabled_plugins``. Tests that need specific plugins
    override the ``enabled_plugins`` fixture.

    Backend loading uses ``load_local_backend_only`` to avoid docker/modal
    SDK imports that would trigger resource guards.
    """
    # Reset the module-level plugin manager singleton before each test
    imbue.mngr.main.reset_plugin_manager()

    # Clear the registries and caches to ensure clean state
    reset_backend_registry()
    reset_agent_registry()
    reset_provider_instances()

    # Discover all entry-point plugins and block everything except enabled_plugins
    all_eps = {ep.name for ep in importlib.metadata.entry_points(group="mngr")}
    to_block = all_eps - enabled_plugins

    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    for name in to_block:
        pm.set_blocked(name)
    pm.load_setuptools_entrypoints("mngr")

    # Only register the local backend, not modal or docker.
    # This prevents tests from depending on Modal credentials or Docker daemon.
    # This also loads the provider configs since backends and configs are registered together.
    load_local_backend_only(pm)

    # Load other registries (agents)
    load_agents_from_plugins(pm)
    register_test_placeholder_agent_type()

    yield pm

    # Reset after the test as well
    imbue.mngr.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()
    reset_provider_instances()


# =============================================================================
# Session Cleanup - Detect and clean up leaked test resources
# =============================================================================


def _get_tmux_sessions_with_prefix(prefix: str) -> list[str]:
    """Get tmux sessions matching the given prefix."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        sessions = [s.strip() for s in result.stdout.splitlines() if s.strip()]
        return [s for s in sessions if s.startswith(prefix)]
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return []


def _kill_tmux_sessions(sessions: list[str]) -> None:
    """Kill the specified tmux sessions and all their processes."""
    for session in sessions:
        cleanup_tmux_session(session)


def _is_xdist_worker_process(process: psutil.Process) -> bool:
    """Check if a process is a pytest-xdist worker process."""
    try:
        cmdline = process.cmdline()
        cmdline_str = " ".join(cmdline)
        # xdist workers are python processes running pytest with gw* identifiers
        return "pytest" in cmdline_str.lower() and "gw" in cmdline_str
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _format_process_info(process: psutil.Process) -> str:
    """Format process information for error messages."""
    try:
        cmdline = process.cmdline()[:5]
        return f"  PID {process.pid}: {process.name()} - {' '.join(cmdline)}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return f"  PID {process.pid}: <process info unavailable>"


def _is_alive_non_zombie(process: psutil.Process) -> bool:
    """Check if a process is alive and not a zombie."""
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _get_stale_docker_test_containers(max_age_seconds: int = 3600) -> list[tuple[str, str]]:
    """Get Docker containers from tests that are older than max_age_seconds.

    Returns a list of (container_id, container_name) tuples for containers
    (both state containers and host containers) that appear to originate
    from tests and are older than the threshold.  This catches containers
    leaked by crashed or interrupted test runs.

    A container is considered test-originated if any of:
    - Its provider label starts with "docker-test-" (from make_docker_provider_with_cleanup), or
    - Its name starts with "mngr_test-" (from generate_test_environment_name), or
    - Its name contains a test prefix pattern (mngr_ followed by a hex UUID, as
      generated by the autouse mngr_test_prefix fixture).
    """
    try:
        client = create_docker_client()
    except docker.errors.DockerException as e:
        # Called unconditionally at session end, including in sessions with no
        # Docker daemon (e.g. offload sandboxes), so a connection failure here
        # is expected -- log at debug only.
        logger.debug("Skipped stale docker container sweep (Docker unavailable): {}", e)
        return []

    try:
        # Find ALL containers with LABEL_PROVIDER set (both state and host containers).
        containers = client.containers.list(
            all=True,
            filters={
                "label": [LABEL_PROVIDER],
            },
        )
    except docker.errors.DockerException as e:
        logger.warning("Failed to list Docker containers during stale-container sweep: {}", e)
        client.close()
        return []

    now = datetime.now(timezone.utc)
    stale: list[tuple[str, str]] = []

    for container in containers:
        labels = container.labels or {}
        provider_name = labels.get(LABEL_PROVIDER, "")
        container_name = container.name or ""

        # Identify test-originated containers by either:
        # 1. Provider label starting with "docker-test-" (SDK-based tests)
        # 2. Container name starting with "mngr_" followed by a hex UUID
        #    (subprocess tests that use the autouse mngr_test_prefix fixture)
        # 3. Container name starting with "mngr_test-" (subprocess tests that
        #    use generate_test_environment_name)
        is_test_container = (
            provider_name.startswith("docker-test-")
            or container_name.startswith("mngr_test-")
            or looks_like_mngr_test_container_name(container_name)
        )
        if not is_test_container:
            continue

        # Check age via container creation time
        try:
            container.reload()
            created_str = container.attrs.get("Created", "")
            if not created_str:
                continue
            # Docker returns ISO format with nanosecond precision
            created_str = created_str.split(".")[0] + "+00:00"
            created = datetime.fromisoformat(created_str)
            age_seconds = (now - created).total_seconds()
            if age_seconds > max_age_seconds:
                stale.append((container.id, container.name or ""))
        except (ValueError, KeyError, docker.errors.DockerException):
            continue

    client.close()
    return stale


def _get_leaked_state_containers_for_prefixes(prefixes: list[str]) -> list[tuple[str, str]]:
    """Find surviving state containers whose name starts with one of *prefixes*.

    Returns (container_id, container_name) tuples for state containers (those
    carrying the state-container type label) created under any of this worker's
    registered prefixes. These are leaks we can attribute to our own fixtures,
    so the caller fails the suite for them (after cleaning them up).
    """
    if not prefixes:
        return []
    try:
        client = create_docker_client()
    except docker.errors.DockerException as e:
        # We only reach here when this worker's docker fixtures ran (non-empty
        # prefixes), so the daemon was reachable during the tests. Failing to
        # connect now means we cannot verify our own cleanup -- this is
        # unexpected, so surface it loudly rather than silently skipping.
        logger.opt(exception=e).error("Failed to connect to Docker to check for leaked state containers")
        return []

    try:
        containers = client.containers.list(
            all=True,
            filters={"label": [f"{STATE_CONTAINER_TYPE_LABEL}={STATE_CONTAINER_TYPE_VALUE}"]},
        )
    except docker.errors.DockerException as e:
        logger.opt(exception=e).error("Failed to list Docker containers while checking for leaked state containers")
        return []
    finally:
        client.close()

    leaked: list[tuple[str, str]] = []
    for container in containers:
        name = container.name or ""
        if any(name.startswith(prefix) for prefix in prefixes):
            leaked.append((container.id, name))
    return leaked


def _remove_docker_containers(containers: list[tuple[str, str]]) -> None:
    """Force-remove the specified Docker containers and their backing volumes.

    Takes a list of (container_id, container_name) tuples. Uses the shared
    remove_docker_container_and_volume helper which removes the container
    first, then removes the backing Docker volume (same name as the container).
    """
    if not containers:
        return

    try:
        client = create_docker_client()
    except docker.errors.DockerException:
        return

    try:
        for container_id, _name in containers:
            try:
                container = client.containers.get(container_id)
                remove_docker_container_and_volume(client, container)
            except (docker.errors.DockerException, docker.errors.NotFound):
                pass
    finally:
        client.close()


class _DockerdStartupError(MngrError):
    """Raised when the release-test session fixture cannot bring dockerd up."""


# Number of times _ensure_dockerd_for_release will invoke start-dockerd.sh
# before giving up. Kept as a named constant so the loop bound and the
# message in the raised _DockerdStartupError stay in lockstep.
_DOCKERD_STARTUP_ATTEMPTS: Final[int] = 3


@pytest.fixture(scope="session", autouse=True)
def _ensure_dockerd_for_release() -> None:
    """Start the Docker daemon if running inside a release test sandbox.

    The Dockerfile.release installs /start-dockerd.sh. The sandbox CMD also
    runs it at launch, but offload overrides the entrypoint, so this session
    fixture is how dockerd actually comes up for release tests.

    start-dockerd.sh is idempotent and polls `docker info` internally until
    the daemon is ready. On gVisor the first attempt can flake (iptables
    setup, IPv6 disable, dockerd bind race), so we retry up to
    _DOCKERD_STARTUP_ATTEMPTS times and verify /var/run/docker.sock exists
    before returning. If we still cannot bring dockerd up, we raise --
    otherwise every docker/docker_sdk test in the session would fail with
    an opaque FileNotFoundError on the socket.
    """
    start_script = Path("/start-dockerd.sh")
    if not start_script.exists():
        return

    docker_sock = Path("/var/run/docker.sock")
    if docker_sock.exists():
        # dockerd already running -- typically started by the Dockerfile.release
        # CMD at sandbox launch. Skip the startup script entirely. Some Modal
        # sandboxes have a read-only /etc/resolv.conf, and running the script
        # when dockerd is already up would otherwise fail there for no reason.
        return

    last_result = None
    for attempt in range(_DOCKERD_STARTUP_ATTEMPTS):
        cg = ConcurrencyGroup(name=f"ensure-dockerd-{attempt}")
        with cg:
            last_result = cg.run_process_to_completion(
                [str(start_script)],
                is_checked_after=False,
            )
        if last_result.returncode == 0 and docker_sock.exists():
            logger.info("[_ensure_dockerd_for_release] dockerd ready on attempt {}", attempt + 1)
            return
        logger.warning(
            "[_ensure_dockerd_for_release] attempt {} failed: returncode={} socket_exists={}\nstdout: {}\nstderr: {}",
            attempt + 1,
            last_result.returncode,
            docker_sock.exists(),
            last_result.stdout,
            last_result.stderr,
        )

    # `last_result` is guaranteed non-None: range(_DOCKERD_STARTUP_ATTEMPTS)
    # is non-empty (the constant is >= 1) and each iteration assigns it
    # unconditionally before the early-return check. Assert to document the
    # invariant and narrow the type for Pyright so the error template can
    # reference `last_result.X!r` directly.
    assert last_result is not None
    raise _DockerdStartupError(
        f"Failed to start dockerd after {_DOCKERD_STARTUP_ATTEMPTS} attempts. "
        f"Last returncode={last_result.returncode}, "
        f"socket_exists={docker_sock.exists()}. "
        f"stdout={last_result.stdout!r} "
        f"stderr={last_result.stderr!r}"
    )


@pytest.fixture(scope="session", autouse=True)
def session_cleanup() -> Generator[None, None, None]:
    """Session-scoped fixture to detect and clean up leaked test resources.

    This fixture runs at the end of each pytest session (once per xdist worker)
    and checks for:
    1. Leftover child processes (excluding xdist workers on the leader)
    2. Leftover tmux sessions created by this worker's tests
    3. Docker state containers leaked under one of this worker's registered
       prefixes (an own-fixture leak)
    4. Stale Docker test containers from other/older sessions (older than 1
       hour) that cannot be attributed to this worker

    If any leaked resources are found:
    - An error is raised to fail the test suite for leaks we can attribute to
      this worker (processes, tmux sessions, prefix-matched state containers);
      stale containers from other sessions are warn-and-cleaned without failing
    - The resources are killed/removed as a last-ditch cleanup measure

    Tests should always clean up after themselves! This is just a safety net.
    """
    # Run all tests first
    yield

    errors: list[str] = []

    # Determine our role in xdist (if using xdist)
    is_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER") is not None
    is_xdist_leader = not is_xdist_worker and os.environ.get("PYTEST_XDIST_TESTRUNUID") is not None

    # 1. Check for leftover child processes
    try:
        current = psutil.Process()
        children = list(current.children(recursive=True))
    except psutil.NoSuchProcess:
        children = []

    # On the xdist leader, filter out xdist worker processes (they're expected)
    if is_xdist_leader:
        children = [c for c in children if not _is_xdist_worker_process(c)]

    # Filter out zombie/dead processes - they're not actually leaked
    leftover_processes = [p for p in children if _is_alive_non_zombie(p)]

    if leftover_processes:
        proc_info = [_format_process_info(p) for p in leftover_processes]
        errors.append(
            "Leftover child processes found!\n"
            "Tests should clean up spawned processes before completing.\n" + "\n".join(proc_info)
        )

    # 2. Check for leftover tmux sessions from this worker's tests.
    # Note: Each test gets its own tmux server via TMUX_TMPDIR, and the
    # per-test fixture kills that server on teardown. This check queries
    # the default tmux server as a fallback safety net -- it would only
    # catch leaks if a test somehow bypassed the per-test TMUX_TMPDIR.
    leftover_sessions: list[str] = []
    for test_id in worker_test_ids:
        prefix = f"mngr_{test_id}-"
        sessions = _get_tmux_sessions_with_prefix(prefix)
        leftover_sessions.extend(sessions)

    if leftover_sessions:
        errors.append(
            "Leftover test tmux sessions found!\n"
            "Tests should destroy their agents/sessions before completing.\n"
            + "\n".join(f"  {s}" for s in leftover_sessions)
        )

    # 3. Check for Docker state containers leaked by THIS worker's fixtures.
    # These carry one of the prefixes our docker fixtures registered, so they
    # are unambiguously our own leaks (a fixture failed to clean up). We fail
    # the suite for these. We cannot fail on arbitrary containers from other
    # concurrent workers/sessions (we have no way to attribute them), so those
    # are handled by the age-based sweep below (warn + clean, no failure).
    leaked_state_containers = _get_leaked_state_containers_for_prefixes(worker_docker_state_prefixes)
    if leaked_state_containers:
        container_lines = [f"  {name} ({cid[:12]})" for cid, name in leaked_state_containers]
        errors.append(
            "Leaked Docker state containers found!\n"
            "A docker fixture failed to remove its state container before completing.\n" + "\n".join(container_lines)
        )

    # 4. Check for stale Docker test containers from other/older sessions (older
    # than 1 hour). These cannot be attributed to this worker, so we warn and
    # clean them but do not fail the suite.
    stale_docker_containers = _get_stale_docker_test_containers(max_age_seconds=3600)
    if stale_docker_containers:
        logger.warning(
            "Cleaning {} stale docker test container(s) from other/older sessions", len(stale_docker_containers)
        )

    # 5. Clean up leaked resources (last-ditch safety measure)
    for process in leftover_processes:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    _kill_tmux_sessions(leftover_sessions)
    _remove_docker_containers(leaked_state_containers)
    _remove_docker_containers(stale_docker_containers)

    # 6. Fail the test suite if any issues were found
    if errors:
        raise AssertionError(
            "=" * 70 + "\n"
            "TEST SESSION CLEANUP FOUND LEAKED RESOURCES!\n" + "=" * 70 + "\n\n" + "\n\n".join(errors) + "\n\n"
            "These resources have been cleaned up, but tests should not leak!\n"
            "Please fix the test(s) that failed to clean up properly."
        )
