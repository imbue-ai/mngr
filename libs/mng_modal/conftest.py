"""Project-level conftest for mng_modal.

Provides test infrastructure by inheriting from mng's conftest and adding
modal-specific resource cleanup.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
import toml

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mng.primitives import UserId
from imbue.mng.utils.logging import suppress_warnings
from imbue.mng.utils.testing import ModalSubprocessTestEnv
from imbue.mng.utils.testing import assert_home_is_temp_directory
from imbue.mng.utils.testing import delete_modal_apps_in_environment
from imbue.mng.utils.testing import delete_modal_environment
from imbue.mng.utils.testing import delete_modal_volumes_in_environment
from imbue.mng.utils.testing import generate_test_environment_name
from imbue.mng.utils.testing import get_subprocess_test_env
from imbue.mng.utils.testing import isolate_home
from imbue.mng.utils.testing import worker_modal_app_names
from imbue.mng.utils.testing import worker_modal_environment_names
from imbue.mng.utils.testing import worker_modal_volume_names
from imbue.mng_modal.backend import ModalProviderBackend
from imbue.mng_modal.register_guards import register_modal_guard
from imbue.resource_guards.resource_guards import register_resource_guard

suppress_warnings()

register_marker("tmux: marks tests that create real tmux sessions or mng agents")
register_marker("rsync: marks tests that invoke rsync for file transfer")
register_marker("unison: marks tests that start a real unison file-sync process")
register_marker("modal: marks tests that connect to the Modal cloud service")
register_resource_guard("tmux")
register_resource_guard("rsync")
register_resource_guard("unison")
register_resource_guard("modal")
register_modal_guard()

register_conftest_hooks(globals())

# Inherit all fixtures from mng's conftest (same pattern as mng_claude)
pytest_plugins = ["imbue.mng.conftest"]


@pytest.fixture(autouse=True)
def _reset_modal_app_registry() -> Generator[None, None, None]:
    """Reset the Modal app registry after each test for isolation."""
    yield
    ModalProviderBackend.reset_app_registry()


@pytest.fixture(autouse=True)
def setup_test_mng_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Set up environment variables for all tests, including Modal tokens.

    This overrides mng's setup_test_mng_env to additionally load Modal
    credentials from ~/.modal.toml before HOME is overridden.
    """
    # Load modal token from real home before overriding HOME
    modal_toml_path = Path(os.path.expanduser("~/.modal.toml"))
    if modal_toml_path.exists():
        for value in toml.load(modal_toml_path).values():
            if value.get("active", ""):
                monkeypatch.setenv("MODAL_TOKEN_ID", value.get("token_id", ""))
                monkeypatch.setenv("MODAL_TOKEN_SECRET", value.get("token_secret", ""))
                break

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


# =============================================================================
# Modal subprocess test fixtures
# =============================================================================


@pytest.fixture(scope="session")
def modal_test_session_env_name() -> str:
    return generate_test_environment_name()


@pytest.fixture(scope="session")
def modal_test_session_host_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    host_dir = tmp_path_factory.mktemp("modal_session") / "mng"
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


@pytest.fixture(scope="session")
def modal_test_session_user_id() -> UserId:
    return UserId(uuid4().hex)


@pytest.fixture(scope="session")
def modal_test_session_cleanup(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
) -> Generator[None, None, None]:
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
    prefix = f"{modal_test_session_env_name}-"
    host_dir = modal_test_session_host_dir
    env = get_subprocess_test_env(
        root_name="mng-acceptance-test",
        prefix=prefix,
        host_dir=host_dir,
    )
    env["MNG_USER_ID"] = modal_test_session_user_id
    yield ModalSubprocessTestEnv(env=env, prefix=prefix, host_dir=host_dir)


# =============================================================================
# Session Cleanup - Detect and clean up leaked Modal test resources
# =============================================================================


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
            subprocess.run(
                ["uv", "run", "modal", "app", "stop", app_id],
                capture_output=True,
                timeout=30,
            )
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
            subprocess.run(
                ["uv", "run", "modal", "volume", "delete", name, "--yes"],
                capture_output=True,
                timeout=30,
            )
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
                ["uv", "run", "modal", "environment", "delete", name, "--yes"],
                capture_output=True,
                timeout=30,
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
        app_info = [f"  {app_id} ({app_name})" for app_id, app_name in leaked_apps]
        errors.append(
            "Leftover Modal apps found!\n"
            "Tests should destroy their Modal hosts before completing.\n" + "\n".join(app_info)
        )

    leaked_volumes = _get_leaked_modal_volumes()
    if leaked_volumes:
        volume_info = [f"  {name}" for name in leaked_volumes]
        errors.append(
            "Leftover Modal volumes found!\n"
            "Tests should delete their Modal volumes before completing.\n" + "\n".join(volume_info)
        )

    leaked_envs = _get_leaked_modal_environments()
    if leaked_envs:
        env_info = [f"  {name}" for name in leaked_envs]
        errors.append(
            "Leftover Modal environments found!\n"
            "Tests should delete their Modal environments before completing.\n" + "\n".join(env_info)
        )

    _stop_modal_apps(leaked_apps)
    _delete_modal_volumes(leaked_volumes)
    _delete_modal_environments(leaked_envs)

    if errors:
        raise AssertionError(
            "=" * 70
            + "\n"
            + "MODAL SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
            + "=" * 70
            + "\n\n"
            + "\n\n".join(errors)
            + "\n\n"
            + "These resources have been cleaned up, but tests should not leak!\n"
        )
