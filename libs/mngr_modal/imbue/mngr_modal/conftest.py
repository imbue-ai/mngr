import json
import os
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Generator
from uuid import uuid4

import modal
import modal.exception
import pluggy
import pytest
import toml
from modal.environments import delete_environment

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import TEST_ENV_PREFIX
from imbue.mngr.utils.testing import assert_home_is_temp_directory
from imbue.mngr.utils.testing import delete_modal_apps_in_environment
from imbue.mngr.utils.testing import delete_modal_environment
from imbue.mngr.utils.testing import delete_modal_volumes_in_environment
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import isolate_home
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import register_modal_test_app
from imbue.mngr.utils.testing import register_modal_test_environment
from imbue.mngr.utils.testing import register_modal_test_volume
from imbue.mngr.utils.testing import worker_modal_app_names
from imbue.mngr.utils.testing import worker_modal_environment_names
from imbue.mngr.utils.testing import worker_modal_volume_names
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.backend import STATE_VOLUME_SUFFIX
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.testing import make_testing_modal_interface
from imbue.mngr_modal.testing import make_testing_provider
from imbue.modal_proxy.testing import TestingModalInterface


def make_modal_provider_real(
    mngr_ctx: MngrContext,
    app_name: str,
    is_persistent: bool = False,
    is_snapshotted_after_create: bool = False,
) -> ModalProviderInstance:
    """Create a ModalProviderInstance with real Modal for acceptance tests.

    By default, is_snapshotted_after_create=False to speed up tests by not creating
    an initial snapshot. Tests that specifically need to test initial snapshot
    behavior should pass is_snapshotted_after_create=True.
    """
    config = ModalProviderConfig(
        app_name=app_name,
        host_dir=Path("/mngr"),
        default_sandbox_timeout=300,
        # FIXME: we really should bump CPU up to 1.0 and memory up to at least 4gb for more stable tests
        default_cpu=0.5,
        default_memory=0.5,
        is_persistent=is_persistent,
        is_snapshotted_after_create=is_snapshotted_after_create,
    )
    instance = ModalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("modal-test"),
        config=config,
        mngr_ctx=mngr_ctx,
    )
    if not isinstance(instance, ModalProviderInstance):
        raise ConfigStructureError(f"Expected ModalProviderInstance, got {type(instance).__name__}")
    return instance


@pytest.fixture
def modal_mngr_ctx(
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    cg: ConcurrencyGroup,
) -> MngrContext:
    """Create a MngrContext with a timestamp-based prefix for Modal acceptance tests.

    Uses the mngr_test-YYYY-MM-DD-HH-MM-SS- prefix format so that environments
    created by these tests are visible to the CI cleanup script
    (cleanup_old_modal_test_environments.py), providing a safety net if
    per-test fixture cleanup fails.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    prefix = f"{TEST_ENV_PREFIX}{timestamp}-"
    config = MngrConfig(default_host_dir=temp_host_dir, prefix=prefix)
    return make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)


def _cleanup_modal_test_resources(app_name: str, volume_name: str, environment_name: str) -> None:
    """Clean up Modal test resources after a test completes.

    This helper performs cleanup in the correct order:
    1. Close the Modal app context
    2. Delete the volume (must be done before environment deletion)
    3. Delete the environment (cleans up any remaining resources)
    """
    # Close the Modal app context first
    ModalProviderBackend.close_app(app_name)

    # Delete the volume using Modal SDK (must be done before environment deletion)
    try:
        modal.Volume.objects.delete(volume_name, environment_name=environment_name)
    except (modal.exception.Error, OSError):
        pass

    # Delete the environment using Modal SDK (cleans up any remaining resources)
    try:
        delete_environment(environment_name)
    except (modal.exception.Error, OSError):
        pass


@pytest.fixture
def real_modal_provider(
    modal_mngr_ctx: MngrContext, mngr_test_id: str
) -> Generator[ModalProviderInstance, None, None]:
    """Create a ModalProviderInstance with real Modal for acceptance tests.

    This fixture creates a Modal environment and cleans it up after the test.
    Cleanup happens in the fixture teardown (not at session end) to prevent
    environment leaks and reduce the time spent on cleanup.

    Uses modal_mngr_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mngr_test_id}"
    provider = make_modal_provider_real(modal_mngr_ctx, app_name)
    environment_name = provider.environment_name
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    # Register resources for leak detection (safety net in case cleanup fails)
    register_modal_test_app(app_name)
    register_modal_test_environment(environment_name)
    register_modal_test_volume(volume_name)

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name)


@pytest.fixture
def persistent_modal_provider(
    modal_mngr_ctx: MngrContext, mngr_test_id: str
) -> Generator[ModalProviderInstance, None, None]:
    """Create a persistent ModalProviderInstance for testing shutdown script creation.

    This fixture is similar to real_modal_provider but uses is_persistent=True,
    which enables the shutdown script feature.

    Uses modal_mngr_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mngr_test_id}"
    provider = make_modal_provider_real(modal_mngr_ctx, app_name, is_persistent=True)
    environment_name = provider.environment_name
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    # Register resources for leak detection
    register_modal_test_app(app_name)
    register_modal_test_environment(environment_name)
    register_modal_test_volume(volume_name)

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name)


@pytest.fixture
def initial_snapshot_provider(
    modal_mngr_ctx: MngrContext, mngr_test_id: str
) -> Generator[ModalProviderInstance, None, None]:
    """Create a ModalProviderInstance with is_snapshotted_after_create=True.

    Use this fixture for tests that specifically test initial snapshot behavior,
    such as restarting a host after hard kill using the initial snapshot.

    Uses modal_mngr_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mngr_test_id}"
    provider = make_modal_provider_real(modal_mngr_ctx, app_name, is_snapshotted_after_create=True)
    environment_name = provider.environment_name
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    # Register resources for leak detection
    register_modal_test_app(app_name)
    register_modal_test_environment(environment_name)
    register_modal_test_volume(volume_name)

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name)


# =============================================================================
# Shared modal test fixtures
#
# These fixtures are importable by other packages (e.g., mngr_claude) via
# pytest_plugins = ["imbue.mngr_modal.conftest"]. This avoids duplicating
# modal test infrastructure across plugin packages.
# =============================================================================


@pytest.fixture(autouse=True)
def setup_test_mngr_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests, including Modal tokens.

    This overrides mngr's setup_test_mngr_env to additionally load Modal
    credentials from ~/.modal.toml before HOME is overridden.
    """
    modal_toml_path = Path(os.path.expanduser("~/.modal.toml"))
    if modal_toml_path.exists():
        for value in toml.load(modal_toml_path).values():
            if value.get("active", ""):
                monkeypatch.setenv("MODAL_TOKEN_ID", value.get("token_id", ""))
                monkeypatch.setenv("MODAL_TOKEN_SECRET", value.get("token_secret", ""))
                break

    isolate_home(tmp_home_dir, monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", str(temp_host_dir))
    monkeypatch.setenv("MNGR_PREFIX", mngr_test_prefix)
    monkeypatch.setenv("MNGR_ROOT_NAME", mngr_test_root_name)

    unison_dir = tmp_home_dir / ".unison"
    unison_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("UNISON", str(unison_dir))

    assert_home_is_temp_directory()

    yield


@pytest.fixture(scope="session")
def modal_test_session_env_name() -> str:
    """Generate a unique, timestamp-based environment name for this test session."""
    return generate_test_environment_name()


@pytest.fixture(scope="session")
def modal_test_session_host_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a session-scoped host directory for Modal tests."""
    host_dir = tmp_path_factory.mktemp("modal_session") / "mngr"
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


@pytest.fixture(scope="session")
def modal_test_session_user_id() -> UserId:
    """Generate a deterministic user ID for the test session."""
    return UserId(uuid4().hex)


@pytest.fixture(scope="session")
def modal_test_session_cleanup(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
) -> Generator[None, None, None]:
    """Session-scoped fixture that cleans up the Modal environment at session end."""
    yield
    prefix = f"{modal_test_session_env_name}-"
    environment_name = f"{prefix}{modal_test_session_user_id}"
    if len(environment_name) > 64:
        environment_name = environment_name[:64]
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
    """Create a subprocess test environment with session-scoped Modal environment."""
    prefix = f"{modal_test_session_env_name}-"
    host_dir = modal_test_session_host_dir
    env = get_subprocess_test_env(
        root_name="mngr-acceptance-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    env["MNGR_USER_ID"] = modal_test_session_user_id
    yield ModalSubprocessTestEnv(env=env, prefix=prefix, host_dir=host_dir)


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for Modal tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    return source_dir


# =============================================================================
# Modal cleanup fixtures
#
# These are importable by consuming packages via pytest_plugins so that
# ModalProviderBackend state is properly cleaned up between tests.
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_modal_app_registry() -> Generator[None, None, None]:
    """Reset the Modal app registry after each test for isolation."""
    yield
    ModalProviderBackend.reset_app_registry()


def _get_leaked_modal_apps() -> list[tuple[str, str]]:
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
        return [
            (app.get("App ID", ""), app.get("Description", ""))
            for app in apps
            if app.get("Description", "") in worker_modal_app_names and app.get("State", "") != "stopped"
        ]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _stop_modal_apps(apps: list[tuple[str, str]]) -> None:
    for app_id, _ in apps:
        try:
            subprocess.run(["uv", "run", "modal", "app", "stop", app_id], capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def _get_leaked_modal_volumes() -> list[str]:
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
        return [v.get("Name", "") for v in volumes if v.get("Name", "") in worker_modal_volume_names]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _delete_modal_volumes(volume_names: list[str]) -> None:
    for name in volume_names:
        try:
            subprocess.run(["uv", "run", "modal", "volume", "delete", name, "--yes"], capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def _get_leaked_modal_environments() -> list[str]:
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
        envs = json.loads(result.stdout)
        return [e.get("name", "") for e in envs if e.get("name", "") in worker_modal_environment_names]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


def _delete_modal_environments(environment_names: list[str]) -> None:
    for name in environment_names:
        try:
            subprocess.run(
                ["uv", "run", "modal", "environment", "delete", name, "--yes"], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


@pytest.fixture(scope="session", autouse=True)
def modal_session_cleanup() -> Generator[None, None, None]:
    """Detect and clean up leaked Modal resources at the end of the test session."""
    yield
    errors: list[str] = []
    leaked_apps = _get_leaked_modal_apps()
    if leaked_apps:
        errors.append(
            "Leftover Modal apps found!\n"
            "Tests should destroy their Modal hosts before completing.\n"
            + "\n".join(f"  {aid} ({aname})" for aid, aname in leaked_apps)
        )
    leaked_volumes = _get_leaked_modal_volumes()
    if leaked_volumes:
        errors.append(
            "Leftover Modal volumes found!\n"
            "Tests should delete their Modal volumes before completing.\n"
            + "\n".join(f"  {n}" for n in leaked_volumes)
        )
    leaked_envs = _get_leaked_modal_environments()
    if leaked_envs:
        errors.append(
            "Leftover Modal environments found!\n"
            "Tests should delete their Modal environments before completing.\n"
            + "\n".join(f"  {n}" for n in leaked_envs)
        )
    _stop_modal_apps(leaked_apps)
    _delete_modal_volumes(leaked_volumes)
    _delete_modal_environments(leaked_envs)
    if errors:
        raise AssertionError(
            "=" * 70
            + "\nMODAL SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
            + "=" * 70
            + "\n\n"
            + "\n\n".join(errors)
            + "\n\nThese resources have been cleaned up, but tests should not leak!\n"
        )


# =============================================================================
# Testing Modal Interface fixtures
#
# These fixtures provide a ModalProviderInstance backed by TestingModalInterface
# for testing mngr_modal business logic without Modal credentials or SSH.
# =============================================================================


@pytest.fixture
def testing_modal(tmp_path: Path, cg: ConcurrencyGroup) -> TestingModalInterface:
    return make_testing_modal_interface(tmp_path, cg)


@pytest.fixture
def testing_provider(
    temp_mngr_ctx: MngrContext,
    testing_modal: TestingModalInterface,
) -> Generator[ModalProviderInstance, None, None]:
    provider = make_testing_provider(temp_mngr_ctx, testing_modal)
    yield provider
    testing_modal.cleanup()


@pytest.fixture
def testing_provider_no_host_volume(
    temp_mngr_ctx: MngrContext,
    testing_modal: TestingModalInterface,
) -> Generator[ModalProviderInstance, None, None]:
    provider = make_testing_provider(temp_mngr_ctx, testing_modal, is_host_volume_created=False)
    yield provider
    testing_modal.cleanup()
