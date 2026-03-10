import json
import os
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Generator
from typing import cast
from uuid import uuid4

import docker
import docker.errors
import pluggy
import psutil
import pytest
import toml
from click.testing import CliRunner
from urwid.widget.listbox import SimpleFocusListWalker

import imbue.mng.main
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.agents.agent_registry import load_agents_from_plugins
from imbue.mng.agents.agent_registry import reset_agent_registry
from imbue.mng.config.consts import PROFILES_DIRNAME
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import Host
from imbue.mng.plugins import hookspecs
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import UserId
from imbue.mng.providers.docker.testing import remove_docker_container_and_volume
from imbue.mng.providers.docker.volume import LABEL_PROVIDER
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.modal.backend import ModalProviderBackend
from imbue.mng.providers.registry import load_local_backend_only
from imbue.mng.providers.registry import reset_backend_registry
from imbue.mng.utils.testing import ModalSubprocessTestEnv
from imbue.mng.utils.testing import assert_home_is_temp_directory
from imbue.mng.utils.testing import cleanup_tmux_session
from imbue.mng.utils.testing import delete_modal_apps_in_environment
from imbue.mng.utils.testing import delete_modal_environment
from imbue.mng.utils.testing import delete_modal_volumes_in_environment
from imbue.mng.utils.testing import generate_test_environment_name
from imbue.mng.utils.testing import get_subprocess_test_env
from imbue.mng.utils.testing import init_git_repo
from imbue.mng.utils.testing import isolate_home
from imbue.mng.utils.testing import isolate_tmux_server
from imbue.mng.utils.testing import make_mng_ctx
from imbue.mng.utils.testing import worker_modal_app_names
from imbue.mng.utils.testing import worker_modal_environment_names
from imbue.mng.utils.testing import worker_modal_volume_names
from imbue.mng.utils.testing import worker_test_ids

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
# Non-autouse fixtures
# =============================================================================


@pytest.fixture
def cg() -> Generator[ConcurrencyGroup, None, None]:
    """Provide a ConcurrencyGroup for tests that need to run processes."""
    with ConcurrencyGroup(name="test") as group:
        yield group


@pytest.fixture
def mng_test_id() -> str:
    """Generate a unique test ID for isolation.

    This ID is used for both the host directory and prefix to ensure
    test isolation and easy cleanup of test resources (e.g., tmux sessions).
    """
    test_id = uuid4().hex
    worker_test_ids.append(test_id)
    return test_id


@pytest.fixture
def mng_test_prefix(mng_test_id: str) -> str:
    """Get the test prefix for tmux session names.

    Format: mng_{test_id}- (underscore separator for easy cleanup).
    """
    return f"mng_{mng_test_id}-"


@pytest.fixture
def mng_test_root_name(mng_test_id: str) -> str:
    """Get the test root name for config isolation.

    Format: mng-test-{test_id}

    This ensures tests don't load the project's .mng/settings.toml config,
    which might have settings like extra_window that would interfere with tests.
    """
    return f"mng-test-{mng_test_id}"


@pytest.fixture
def temp_host_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for host/mng data.

    This fixture creates .mng inside tmp_path (which becomes the fake HOME),
    ensuring tests don't write to the real ~/.mng.
    """
    host_dir = tmp_path / ".mng"
    host_dir.mkdir()
    return host_dir


@pytest.fixture
def tmp_home_dir(tmp_path: Path) -> Generator[Path, None, None]:
    yield tmp_path


@pytest.fixture
def setup_git_config(tmp_path: Path) -> None:
    """Create a .gitconfig in the fake HOME so git commands work.

    Use this fixture for any test that runs git commands.
    The temp_git_repo fixture depends on this, so you don't need both.
    """
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")


@pytest.fixture
def temp_git_repo(tmp_path: Path, setup_git_config: None) -> Path:
    """Create a temporary git repository with an initial commit.

    This fixture:
    1. Ensures .gitconfig exists in the fake HOME (via setup_git_config)
    2. Creates a git repo with one tracked file and an initial commit

    Use this fixture for any test that needs a git repository.
    """
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()

    init_git_repo(repo_dir)

    return repo_dir


@pytest.fixture
def disable_remote_providers_for_subprocesses(
    project_config_dir: Path, monkeypatch: pytest.MonkeyPatch, temp_git_repo: Path
) -> Path:
    """Disable the Modal and Docker providers for subprocesses spawned during a test.

    Writes a settings.local.toml inside a temporary git repo's config directory
    and chdir's into that repo. Spawned subprocesses inherit the CWD, so the
    config loader's upward directory walk finds the settings file.

    Use this when a test spawns a child process that runs ``mng`` commands
    and would otherwise fail because Modal credentials are not available in
    the test environment, or would create Docker state containers that leak.
    """
    settings_path = project_config_dir / "settings.local.toml"
    settings_path.write_text("[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n")
    monkeypatch.chdir(temp_git_repo)
    return settings_path


@pytest.fixture
def temp_work_dir(tmp_path: Path) -> Path:
    """Create a temporary work_dir directory for agents."""
    work_dir = tmp_path / "work_dir"
    work_dir.mkdir()
    return work_dir


@pytest.fixture
def project_config_dir(temp_git_repo: Path, mng_test_root_name: str) -> Path:
    """Return the project config directory inside the test git repo, creating it.

    The directory is named `.{mng_test_root_name}` (e.g., `.mng-test-abc123`).
    Tests can write `settings.toml` or `settings.local.toml` into this directory
    to configure project-level settings for a test.
    """
    config_dir = temp_git_repo / f".{mng_test_root_name}"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


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
def temp_profile_dir(temp_host_dir: Path) -> Path:
    """Create a temporary profile directory.

    Use this fixture when tests need to create their own MngContext with custom config.
    """
    profile_dir = temp_host_dir / PROFILES_DIRNAME / uuid4().hex
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


@pytest.fixture
def temp_config(temp_host_dir: Path, mng_test_prefix: str) -> MngConfig:
    """Create a MngConfig with a temporary host directory.

    Use this fixture when calling API functions that need a config.
    """
    return MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix, is_error_reporting_enabled=False)


@pytest.fixture
def temp_mng_ctx(
    temp_config: MngConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngContext, None, None]:
    """Create a MngContext with a temporary host directory.

    Use this fixture when calling API functions that need a context.
    """
    cg = ConcurrencyGroup(name="test")
    with cg:
        yield make_mng_ctx(temp_config, plugin_manager, temp_profile_dir, concurrency_group=cg)


@pytest.fixture
def local_provider(temp_host_dir: Path, temp_mng_ctx: MngContext) -> LocalProviderInstance:
    """Create a LocalProviderInstance with a temporary host directory."""
    return LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=temp_host_dir,
        mng_ctx=temp_mng_ctx,
    )


@pytest.fixture
def per_host_dir(temp_host_dir: Path) -> Path:
    """Get the host directory for the local provider.

    This is the directory where host-scoped data lives: agents/, data.json,
    activity/, etc. This is the same as temp_host_dir (e.g. ~/.mng/).
    """
    return temp_host_dir


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a Click CLI runner for testing CLI commands."""
    return CliRunner()


# =============================================================================
# Modal subprocess test environment fixture (session-scoped)
# =============================================================================


@pytest.fixture(scope="session")
def modal_test_session_env_name() -> str:
    """Generate a unique, timestamp-based environment name for this test session.

    This fixture is session-scoped, so all tests in a session share the same
    environment name. The name includes a UTC timestamp in the format:
    mng_test-YYYY-MM-DD-HH-MM-SS
    """
    return generate_test_environment_name()


@pytest.fixture(scope="session")
def modal_test_session_host_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a session-scoped host directory for Modal tests.

    This ensures all tests in a session share the same host directory,
    which means they share the same Modal environment.
    """
    host_dir = tmp_path_factory.mktemp("modal_session") / "mng"
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


@pytest.fixture(scope="session")
def modal_test_session_user_id() -> UserId:
    """Generate a deterministic user ID for the test session.

    This user ID is shared across all subprocess Modal tests in a session
    via the MNG_USER_ID environment variable. By generating it upfront,
    the cleanup fixture can construct the exact environment name without
    needing to find the user_id file in the profile directory structure.
    """
    return UserId(uuid4().hex)


@pytest.fixture(scope="session")
def modal_test_session_cleanup(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
) -> Generator[None, None, None]:
    """Session-scoped fixture that cleans up the Modal environment at session end.

    This fixture ensures the Modal environment created for tests is deleted
    when the test session completes, including all apps and volumes.
    """
    yield

    # Clean up Modal environment after the session.
    # The environment name is {prefix}{user_id}, where prefix is based on the timestamp
    # and user_id is the session-scoped deterministic ID.
    prefix = f"{modal_test_session_env_name}-"
    environment_name = f"{prefix}{modal_test_session_user_id}"

    # Truncate environment_name if needed (Modal has 64 char limit)
    if len(environment_name) > 64:
        environment_name = environment_name[:64]

    # Delete apps, volumes, and environment using functions from testing.py
    delete_modal_apps_in_environment(environment_name)
    delete_modal_volumes_in_environment(environment_name)
    delete_modal_environment(environment_name)


@pytest.fixture
def modal_subprocess_env(
    modal_test_session_env_name: str,
    modal_test_session_host_dir: Path,
    modal_test_session_cleanup: None,
    modal_test_session_user_id: UserId,
) -> Generator[ModalSubprocessTestEnv, None, None]:
    """Create a subprocess test environment with session-scoped Modal environment.

    This fixture:
    1. Uses a session-scoped MNG_PREFIX based on UTC timestamp (mng_test-YYYY-MM-DD-HH-MM-SS)
    2. Uses a session-scoped MNG_HOST_DIR so all tests share the same host directory
    3. Sets MNG_USER_ID so all subprocesses use the same deterministic user ID
    4. Cleans up the Modal environment at the end of the session (not per-test)

    Using session-scoped environments reduces the number of environments created
    and makes cleanup easier (environments have timestamps in their names).
    """
    prefix = f"{modal_test_session_env_name}-"
    host_dir = modal_test_session_host_dir

    env = get_subprocess_test_env(
        root_name="mng-acceptance-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    # Set the user ID so all subprocesses use the same deterministic ID.
    # This ensures the cleanup fixture can construct the exact environment name.
    env["MNG_USER_ID"] = modal_test_session_user_id

    yield ModalSubprocessTestEnv(env=env, prefix=prefix, host_dir=host_dir)


# =============================================================================
# Autouse fixtures
# =============================================================================


@pytest.fixture
def _isolate_tmux_server(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Give each test its own isolated tmux server.

    Delegates to the shared isolate_tmux_server() context manager in testing.py.
    See its docstring for details on the isolation strategy and why /tmp is used.
    """
    with isolate_tmux_server(monkeypatch):
        yield


@pytest.fixture(autouse=True)
def setup_test_mng_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests.

    This autouse fixture ensures:
    - HOME points to tmp_path (not the real ~/)
    - MNG_HOST_DIR points to tmp_path/.mng (not ~/.mng)
    - MNG_PREFIX uses a unique test ID for isolation
    - MNG_ROOT_NAME prevents loading project config (.mng/settings.toml)
    - TMUX_TMPDIR gives each test its own tmux server (via _isolate_tmux_server)

    By setting HOME to tmp_path, tests cannot accidentally read or modify
    files in the real home directory. This protects files like ~/.claude.json.
    """
    # before we nuke our home directory, we need to load the right token from the real home directory
    modal_toml_path = Path(os.path.expanduser("~/.modal.toml"))
    if modal_toml_path.exists():
        for value in toml.load(modal_toml_path).values():
            if value.get("active", ""):
                monkeypatch.setenv("MODAL_TOKEN_ID", value.get("token_id", ""))
                monkeypatch.setenv("MODAL_TOKEN_SECRET", value.get("token_secret", ""))
                break
    if not os.environ.get("MODAL_TOKEN_ID") or not os.environ.get("MODAL_TOKEN_SECRET"):
        # check if we have "release" mark enabled:
        if "release" in getattr(pytest, "current_test_marks", []):
            raise Exception(
                "No active Modal token found in ~/.modal.toml for release tests. Please ensure you have an active token configured or set the env vars"
            )

    isolate_home(tmp_home_dir, monkeypatch)
    monkeypatch.setenv("MNG_HOST_DIR", str(temp_host_dir))
    monkeypatch.setenv("MNG_PREFIX", mng_test_prefix)
    monkeypatch.setenv("MNG_ROOT_NAME", mng_test_root_name)

    # Unison derives its config directory from $HOME. Since we override HOME
    # above, unison tries to create its config dir inside tmp_path, which
    # fails because the expected parent directories don't exist. The UNISON
    # env var overrides this to a path we control.
    unison_dir = tmp_home_dir / ".unison"
    unison_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("UNISON", str(unison_dir))

    # Safety check: verify Path.home() is in a temp directory.
    # If this fails, tests could accidentally modify the real home directory.
    assert_home_is_temp_directory()

    yield


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Create a local Host via the local provider.

    This fixture eliminates the repeated pattern of:
        host = local_provider.create_host(HostName("localhost"))

    Use this when tests need a Host instance for creating agents,
    executing commands, etc. The local provider always returns a Host
    (the concrete OnlineHostInterface implementation).
    """
    host = local_provider.create_host(HostName("localhost"))
    return cast(Host, host)


_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKSPACE_PACKAGES = (
    _REPO_ROOT / "libs" / "imbue_common",
    _REPO_ROOT / "libs" / "concurrency_group",
    _REPO_ROOT / "libs" / "mng",
)


@pytest.fixture
def isolated_mng_venv(tmp_path: Path) -> Path:
    """Create a temporary venv with mng installed for subprocess-based tests.

    Returns the venv directory. Use ``venv / "bin" / "mng"`` to run mng
    commands, or ``venv / "bin" / "python"`` for the interpreter.

    Writes a ``uv-receipt.toml`` so that ``require_uv_tool_receipt()``
    recognises this venv as a uv-tool-managed installation.

    This fixture is useful for tests that install/uninstall packages and
    need full isolation from the main workspace venv.

    To avoid network access (and the flakiness that comes with it), we
    export mng's pinned deps from the lockfile via ``uv export``, then
    install them with ``--no-deps`` (uses uv cache, no resolution or
    network needed).
    """
    venv_dir = tmp_path / "isolated-venv"

    workspace_install_args: list[str] = []
    for pkg in _WORKSPACE_PACKAGES:
        workspace_install_args.extend(["-e", str(pkg)])

    python_path = str(venv_dir / "bin" / "python")

    cg = ConcurrencyGroup(name="isolated-venv-setup")
    with cg:
        # Export mng's pinned transitive deps from the lockfile (no editable/comment lines)
        export_result = cg.run_process_to_completion(
            ("uv", "export", "--package", "mng", "--no-hashes", "--frozen"),
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
            ("uv", "pip", "install", "--python", python_path, "--no-deps", "-r", str(reqs_file))
        )
        # Install workspace packages as editable (no-deps since deps are already installed)
        cg.run_process_to_completion(
            ("uv", "pip", "install", "--python", python_path, "--no-deps", *workspace_install_args)
        )

    # Write a uv-receipt.toml so plugin add/remove recognise this as a
    # uv-tool-managed venv (the receipt lives at sys.prefix root).
    receipt_content = (
        '[tool]\nrequirements = [{ name = "mng" }]\n'
        "entrypoints = [\n"
        f'    {{ name = "mng", install-path = "{venv_dir / "bin" / "mng"}", from = "mng" }},\n'
        "]\n"
    )
    (venv_dir / "uv-receipt.toml").write_text(receipt_content)

    return venv_dir


@pytest.fixture(autouse=True)
def plugin_manager() -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with mng hookspecs and local backend only.

    This fixture only loads the local provider backend, not modal. This ensures
    tests don't depend on Modal credentials being available.

    Also loads external plugins via setuptools entry points to match the behavior
    of load_config(). This ensures that external plugins like mng_opencode are
    discovered and registered.

    This fixture also resets the module-level plugin manager singleton to ensure
    test isolation.
    """
    # Reset the module-level plugin manager singleton before each test
    imbue.mng.main.reset_plugin_manager()

    # Clear the registries to ensure clean state
    reset_backend_registry()
    reset_agent_registry()

    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    pm.load_setuptools_entrypoints("mng")

    # Only register the local backend, not modal
    # This prevents tests from depending on Modal credentials
    # This also loads the provider configs since backends and configs are registered together
    load_local_backend_only(pm)

    # Load other registries (agents)
    load_agents_from_plugins(pm)

    yield pm

    # Reset after the test as well
    imbue.mng.main.reset_plugin_manager()
    reset_backend_registry()
    reset_agent_registry()

    # Clean up Modal app contexts to prevent async cleanup errors
    ModalProviderBackend.reset_app_registry()


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


def _get_leaked_modal_apps() -> list[tuple[str, str]]:
    """Get Modal apps that were registered and are still running.

    Returns a list of (app_id, app_name) tuples for apps that were created during
    tests but are still running (not in 'stopped' state).

    Uses 'uv run modal app list --json' to query the current state of all apps.
    """
    if not worker_modal_app_names:
        return []

    try:
        result = subprocess.run(
            ["uv", "run", "modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        apps = json.loads(result.stdout)
        leaked: list[tuple[str, str]] = []

        for app in apps:
            app_name = app.get("Description", "")
            app_id = app.get("App ID", "")
            state = app.get("State", "")

            # Check if this app was created by our tests and is not stopped
            if app_name in worker_modal_app_names and state != "stopped":
                leaked.append((app_id, app_name))

        return leaked
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _stop_modal_apps(apps: list[tuple[str, str]]) -> None:
    """Stop the specified Modal apps.

    Takes a list of (app_id, app_name) tuples and stops each app using
    'uv run modal app stop <app_id>'.

    This function is defensive and will silently skip any apps that cannot
    be stopped.
    """
    if not apps:
        return

    for app_id, _app_name in apps:
        try:
            subprocess.run(
                ["uv", "run", "modal", "app", "stop", app_id],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def _get_leaked_modal_volumes() -> list[str]:
    """Get Modal volumes that were registered and still exist.

    Returns a list of volume names for volumes that were created during
    tests and still exist (not yet deleted).

    Uses 'uv run modal volume list --json' to query the current state of all volumes.
    """
    if not worker_modal_volume_names:
        return []

    try:
        result = subprocess.run(
            ["uv", "run", "modal", "volume", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        volumes = json.loads(result.stdout)
        leaked: list[str] = []

        for volume in volumes:
            volume_name = volume.get("Name", "")

            # Check if this volume was created by our tests
            if volume_name in worker_modal_volume_names:
                leaked.append(volume_name)

        return leaked
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _delete_modal_volumes(volume_names: list[str]) -> None:
    """Delete the specified Modal volumes.

    Takes a list of volume names and deletes each volume using
    'uv run modal volume delete <volume_name> --yes'.

    This function is defensive and will silently skip any volumes that cannot
    be deleted.
    """
    if not volume_names:
        return

    for volume_name in volume_names:
        try:
            subprocess.run(
                ["uv", "run", "modal", "volume", "delete", volume_name, "--yes"],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def _get_leaked_modal_environments() -> list[str]:
    """Get Modal environments that were registered and still exist.

    Returns a list of environment names for environments that were created during
    tests and still exist (not yet deleted).

    Uses 'uv run modal environment list --json' to query the current state of all environments.
    """
    if not worker_modal_environment_names:
        return []

    try:
        result = subprocess.run(
            ["uv", "run", "modal", "environment", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        environments = json.loads(result.stdout)
        leaked: list[str] = []

        for env in environments:
            env_name = env.get("name", "")

            # Check if this environment was created by our tests
            if env_name in worker_modal_environment_names:
                leaked.append(env_name)

        return leaked
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _delete_modal_environments(environment_names: list[str]) -> None:
    """Delete the specified Modal environments.

    Takes a list of environment names and deletes each environment using
    'uv run modal environment delete <environment_name> --yes'.

    This function is defensive and will silently skip any environments that cannot
    be deleted.
    """
    if not environment_names:
        return

    for env_name in environment_names:
        try:
            subprocess.run(
                ["uv", "run", "modal", "environment", "delete", env_name, "--yes"],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def _get_stale_docker_test_containers(max_age_seconds: int = 3600) -> list[tuple[str, str]]:
    """Get Docker containers from tests that are older than max_age_seconds.

    Returns a list of (container_id, container_name) tuples for containers
    (both state containers and host containers) that appear to originate
    from tests and are older than the threshold.  This catches containers
    leaked by crashed or interrupted test runs.

    A container is considered test-originated if any of:
    - Its provider label starts with "docker-test-" (from make_docker_provider_with_cleanup), or
    - Its name starts with "mng_test-" (from generate_test_environment_name), or
    - Its name contains a test prefix pattern (mng_ followed by a hex UUID, as
      generated by the autouse mng_test_prefix fixture).
    """
    try:
        client = docker.from_env()
    except docker.errors.DockerException:
        return []

    try:
        # Find ALL containers with LABEL_PROVIDER set (both state and host containers).
        containers = client.containers.list(
            all=True,
            filters={
                "label": [LABEL_PROVIDER],
            },
        )
    except docker.errors.DockerException:
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
        # 2. Container name starting with "mng_" followed by a hex UUID
        #    (subprocess tests that use the autouse mng_test_prefix fixture)
        # 3. Container name starting with "mng_test-" (subprocess tests that
        #    use generate_test_environment_name)
        is_test_container = (
            provider_name.startswith("docker-test-")
            or container_name.startswith("mng_test-")
            or _looks_like_test_prefix(container_name)
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


def _looks_like_test_prefix(container_name: str) -> bool:
    """Check if a container name starts with a test prefix pattern.

    Test prefixes follow the format 'mng_{hex32}-' where {hex32} is a
    32-character hex string (uuid4().hex). For example:
    'mng_22921e597952421296c8973d922f2eb3-docker-state-...'
    """
    if not container_name.startswith("mng_"):
        return False
    # Extract the part after 'mng_' up to the first '-'
    rest = container_name[4:]
    dash_idx = rest.find("-")
    if dash_idx != 32:
        return False
    hex_part = rest[:32]
    return all(c in "0123456789abcdef" for c in hex_part)


def _remove_docker_containers(containers: list[tuple[str, str]]) -> None:
    """Force-remove the specified Docker containers and their backing volumes.

    Takes a list of (container_id, container_name) tuples. Uses the shared
    remove_docker_container_and_volume helper which removes the container
    first, then removes the backing Docker volume (same name as the container).
    """
    if not containers:
        return

    try:
        client = docker.from_env()
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


@pytest.fixture(scope="session", autouse=True)
def session_cleanup() -> Generator[None, None, None]:
    """Session-scoped fixture to detect and clean up leaked test resources.

    This fixture runs at the end of each pytest session (once per xdist worker)
    and checks for:
    1. Leftover child processes (excluding xdist workers on the leader)
    2. Leftover tmux sessions created by this worker's tests
    3. Leftover Modal apps created by this worker's tests
    4. Leftover Modal volumes created by this worker's tests
    5. Leftover Modal environments created by this worker's tests
    6. Stale Docker containers from tests (older than 1 hour), including
       both state containers and host containers

    If any leaked resources are found:
    - An error is raised to fail the test suite (except for stale Docker
      containers, which are silently cleaned up since they may be from
      other sessions)
    - The resources are killed as a last-ditch cleanup measure

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
        prefix = f"mng_{test_id}-"
        sessions = _get_tmux_sessions_with_prefix(prefix)
        leftover_sessions.extend(sessions)

    if leftover_sessions:
        errors.append(
            "Leftover test tmux sessions found!\n"
            "Tests should destroy their agents/sessions before completing.\n"
            + "\n".join(f"  {s}" for s in leftover_sessions)
        )

    # 3. Check for leftover Modal apps from this worker's tests
    leftover_apps = _get_leaked_modal_apps()

    if leftover_apps:
        app_info = [f"  {app_id} ({app_name})" for app_id, app_name in leftover_apps]
        errors.append(
            "Leftover Modal apps found!\n"
            "Tests should destroy their Modal hosts before completing.\n" + "\n".join(app_info)
        )

    # 4. Check for leftover Modal volumes from this worker's tests
    leftover_volumes = _get_leaked_modal_volumes()

    if leftover_volumes:
        volume_info = [f"  {volume_name}" for volume_name in leftover_volumes]
        errors.append(
            "Leftover Modal volumes found!\n"
            "Tests should delete their Modal volumes before completing.\n" + "\n".join(volume_info)
        )

    # 5. Check for leftover Modal environments from this worker's tests
    leftover_environments = _get_leaked_modal_environments()

    if leftover_environments:
        env_info = [f"  {env_name}" for env_name in leftover_environments]
        errors.append(
            "Leftover Modal environments found!\n"
            "Tests should delete their Modal environments before completing.\n" + "\n".join(env_info)
        )

    # 6. Check for stale Docker containers from tests (older than 1 hour).
    # This catches both state containers and host containers that were leaked
    # by crashed or interrupted test runs.  We don't fail the test suite for
    # these (they may be from other sessions), but we do clean them up.
    stale_docker_containers = _get_stale_docker_test_containers(max_age_seconds=3600)

    # 7. Clean up leaked resources (last-ditch safety measure)
    for process in leftover_processes:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    _kill_tmux_sessions(leftover_sessions)
    _stop_modal_apps(leftover_apps)
    _delete_modal_volumes(leftover_volumes)
    _delete_modal_environments(leftover_environments)
    _remove_docker_containers(stale_docker_containers)

    # 8. Fail the test suite if any issues were found
    if errors:
        raise AssertionError(
            "=" * 70 + "\n"
            "TEST SESSION CLEANUP FOUND LEAKED RESOURCES!\n" + "=" * 70 + "\n\n" + "\n\n".join(errors) + "\n\n"
            "These resources have been cleaned up, but tests should not leak!\n"
            "Please fix the test(s) that failed to clean up properly."
        )
