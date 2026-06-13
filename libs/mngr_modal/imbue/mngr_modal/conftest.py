import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Generator
from typing import assert_never
from uuid import uuid4

import modal
import modal.exception
import pluggy
import pytest
import toml
from loguru import logger
from modal.environments import delete_environment

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.env_utils import TEST_ENV_PATTERN
from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX
from imbue.mngr.utils.testing import ModalCleanupOutcome
from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import delete_modal_apps_in_environment
from imbue.mngr.utils.testing import delete_modal_environment
from imbue.mngr.utils.testing import delete_modal_volumes_in_environment
from imbue.mngr.utils.testing import deregister_modal_test_environment
from imbue.mngr.utils.testing import deregister_modal_test_volume
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import read_shared_modal_env_name
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
from imbue.modal_proxy.testing import FakeModalInterface


def make_modal_provider_real(
    mngr_ctx: MngrContext,
    app_name: str,
    is_persistent: bool = False,
    is_snapshotted_after_create: bool = False,
    user_id_override: UserId | None = None,
) -> ModalProviderInstance:
    """Create a ModalProviderInstance with real Modal for acceptance tests.

    By default, is_snapshotted_after_create=False to speed up tests by not creating
    an initial snapshot. Tests that specifically need to test initial snapshot
    behavior should pass is_snapshotted_after_create=True.

    ``user_id_override`` forwards into ``ModalProviderConfig.user_id`` so the
    shared-env test mode (MNGR_TEST_SHARED_MODAL_ENV_NAME) can pin the env
    name's user_id segment to the suffix the justfile pre-created.
    """
    prefix = mngr_ctx.config.prefix
    if not TEST_ENV_PATTERN.match(prefix):
        raise ConfigStructureError(
            f"Modal test prefix '{prefix}' does not match the required pattern "
            f"'mngr_test-YYYY-MM-DD-HH-MM-SS-*'. Use the modal_mngr_ctx fixture "
            f"(not temp_mngr_ctx) when creating real Modal providers, so that "
            f"test environments can be identified and cleaned up by CI."
        )
    config = ModalProviderConfig(
        app_name=app_name,
        host_dir=Path("/mngr"),
        default_sandbox_timeout=300,
        default_cpu=1.0,
        default_memory=2.0,
        is_persistent=is_persistent,
        is_snapshotted_after_create=is_snapshotted_after_create,
        user_id=user_id_override,
    )
    # Acceptance fixtures always need to bootstrap the per-session Modal env,
    # so call bootstrap_for_host_creation before build_provider_instance.
    instance_name = ProviderInstanceName("modal-test")
    ModalProviderBackend.bootstrap_for_host_creation(
        name=instance_name,
        config=config,
        mngr_ctx=mngr_ctx,
    )
    instance = ModalProviderBackend.build_provider_instance(
        name=instance_name,
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

    When MNGR_TEST_SHARED_MODAL_ENV_NAME is set (offload mode), the prefix is
    taken from the pre-created shared env so every test in the offload run
    points at the same Modal environment.
    """
    shared = read_shared_modal_env_name()
    if shared is not None:
        timestamp_name, _ = shared
    else:
        now = datetime.now(timezone.utc)
        timestamp_name = f"{TEST_ENV_PREFIX}{now.strftime('%Y-%m-%d-%H-%M-%S')}"
    config = MngrConfig(default_host_dir=temp_host_dir, prefix=f"{timestamp_name}-")
    return make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)


def _cleanup_modal_test_resources(
    app_name: str,
    volume_name: str,
    environment_name: str,
    *,
    is_env_owned_by_test: bool = True,
) -> None:
    """Clean up Modal test resources after a test completes.

    1. Close the Modal app context. For ephemeral apps this advances the
       `app.run()` generator, which Modal treats as the calling-program exit
       and stops the app + its sandboxes server-side. For persistent apps
       (`App.lookup` with `create_if_missing=True`, `run_context is None`)
       this is a no-op locally; the app gets stopped by step 3, since
       `modal environment delete` "deletes all apps in the selected
       environment" (https://modal.com/docs/reference/cli/environment).
    2. Delete the volume (must precede env deletion).
    3. Delete the environment (only when ``is_env_owned_by_test`` is True).

    Apps are deliberately not in the deregister chain. We leave registered
    apps tracked and let `_get_leaked_modal_apps` be the authoritative
    source of app liveness.

    Volume + env are deregistered only on DELETED/NOT_FOUND. FAILED leaves the
    resource tracked so the session-end leak detector surfaces it.

    When ``is_env_owned_by_test`` is False (shared-env mode), the environment
    is owned by the justfile wrapper, not the test, so we skip env deletion
    entirely. Volume cleanup still runs -- volumes are per-test and must be
    deleted.

    Known limitation: treating NOT_FOUND as success has a residual failure
    mode. If env-create propagation is eventually consistent across Modal
    replicas, a delete that hits a stale replica returns NOT_FOUND for a
    resource that actually exists, and we'd deregister. The CI hourly cleanup
    script (`cleanup_old_modal_test_environments.py`) is the safety net.
    """
    ModalProviderBackend.close_app(app_name)

    # Delete the volume using Modal SDK (must be done before environment deletion).
    _apply_cleanup_outcome(
        outcome=_delete_modal_volume_via_sdk(volume_name, environment_name),
        deregister=lambda: deregister_modal_test_volume(volume_name),
        resource_description=f"volume {volume_name} in environment {environment_name}",
    )
    if not is_env_owned_by_test:
        return
    # Delete the environment using Modal SDK.
    _apply_cleanup_outcome(
        outcome=_delete_modal_environment_via_sdk(environment_name),
        deregister=lambda: deregister_modal_test_environment(environment_name),
        resource_description=f"environment {environment_name}",
    )


def _apply_cleanup_outcome(
    outcome: ModalCleanupOutcome,
    deregister: Callable[[], None],
    resource_description: str,
) -> None:
    """Dispatch a `ModalCleanupOutcome` to the standard policy.

    DELETED|NOT_FOUND -> call `deregister()` (resource is gone, drop it from
    the leak-tracking lists). FAILED -> log a `logger.error` naming the
    resource and explaining that it stays registered so the session-end
    leak detector surfaces it. The `_` arm uses `assert_never` so adding
    a new outcome enum value forces every caller's policy to be revisited
    here in one place.
    """
    match outcome:
        case ModalCleanupOutcome.DELETED | ModalCleanupOutcome.NOT_FOUND:
            deregister()
        case ModalCleanupOutcome.FAILED:
            logger.error(
                "Cleanup of Modal {} failed; leaving registered so session-end leak detector surfaces it.",
                resource_description,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _classify_modal_sdk_delete(delete_fn: Callable[[], object], resource_description: str) -> ModalCleanupOutcome:
    """Run an SDK delete callable and classify the outcome as a `ModalCleanupOutcome`.

    Shared scaffold for `_delete_modal_volume_via_sdk` and
    `_delete_modal_environment_via_sdk`: success -> DELETED,
    `modal.exception.NotFoundError` -> NOT_FOUND (debug-logged),
    `(modal.exception.Error, OSError)` -> FAILED (warning-logged).
    See `imbue.mngr.utils.testing.ModalCleanupOutcome` for the contract.

    `delete_fn` is typed `Callable[[], object]` because the SDK wrappers
    passed in (e.g. `modal.Volume.objects.delete`) are not declared as
    returning `None`; the return value is intentionally discarded here.
    """
    try:
        delete_fn()
        return ModalCleanupOutcome.DELETED
    except modal.exception.NotFoundError:
        logger.debug("Modal {} already gone", resource_description)
        return ModalCleanupOutcome.NOT_FOUND
    except (modal.exception.Error, OSError) as e:
        logger.warning("Failed to delete Modal {}: {}", resource_description, e)
        return ModalCleanupOutcome.FAILED


def _delete_modal_volume_via_sdk(volume_name: str, environment_name: str) -> ModalCleanupOutcome:
    """Delete a Modal volume via the SDK and classify the outcome."""
    return _classify_modal_sdk_delete(
        lambda: modal.Volume.objects.delete(volume_name, environment_name=environment_name),
        f"volume {volume_name} in env {environment_name}",
    )


def _delete_modal_environment_via_sdk(environment_name: str) -> ModalCleanupOutcome:
    """Delete a Modal environment via the SDK and classify the outcome."""
    return _classify_modal_sdk_delete(
        lambda: delete_environment(environment_name),
        f"environment {environment_name}",
    )


def _build_real_modal_provider_with_shared_env_support(
    modal_mngr_ctx: MngrContext,
    mngr_test_id: str,
    *,
    is_persistent: bool = False,
    is_snapshotted_after_create: bool = False,
) -> tuple[ModalProviderInstance, str, str, str, bool]:
    """Build a real-Modal provider and register its resources for leak detection.

    Honors ``MNGR_TEST_SHARED_MODAL_ENV_NAME``: when set, threads the suffix
    through ``ModalProviderConfig.user_id`` so every fixture in the offload
    run lands in the justfile's pre-created env, and skips
    ``register_modal_test_environment`` (the env is not owned by the test).

    Returns ``(provider, app_name, environment_name, volume_name, is_env_owned_by_test)``
    so each fixture can yield the provider, then pass ``is_env_owned_by_test``
    into ``_cleanup_modal_test_resources`` on teardown.
    """
    shared = read_shared_modal_env_name()
    user_id_override = UserId(shared[1]) if shared is not None else None
    app_name = f"{MODAL_TEST_APP_PREFIX}{mngr_test_id}"
    provider = make_modal_provider_real(
        modal_mngr_ctx,
        app_name,
        is_persistent=is_persistent,
        is_snapshotted_after_create=is_snapshotted_after_create,
        user_id_override=user_id_override,
    )
    environment_name = provider.environment_name
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    register_modal_test_app(app_name)
    register_modal_test_volume(volume_name)
    is_env_owned_by_test = shared is None
    if is_env_owned_by_test:
        register_modal_test_environment(environment_name)
    return provider, app_name, environment_name, volume_name, is_env_owned_by_test


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
    provider, app_name, environment_name, volume_name, is_env_owned_by_test = (
        _build_real_modal_provider_with_shared_env_support(modal_mngr_ctx, mngr_test_id)
    )

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name, is_env_owned_by_test=is_env_owned_by_test)


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
    provider, app_name, environment_name, volume_name, is_env_owned_by_test = (
        _build_real_modal_provider_with_shared_env_support(modal_mngr_ctx, mngr_test_id, is_persistent=True)
    )

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name, is_env_owned_by_test=is_env_owned_by_test)


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
    provider, app_name, environment_name, volume_name, is_env_owned_by_test = (
        _build_real_modal_provider_with_shared_env_support(
            modal_mngr_ctx, mngr_test_id, is_snapshotted_after_create=True
        )
    )

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name, is_env_owned_by_test=is_env_owned_by_test)


# =============================================================================
# Shared modal test fixtures
#
# These fixtures are importable by other packages (e.g., mngr_claude) via
# pytest_plugins = ["imbue.mngr_modal.conftest"]. This avoids duplicating
# modal test infrastructure across plugin packages.
# =============================================================================


# The developer's real ~/.modal.toml, resolved at import time -- before any
# test's autouse HOME-isolation fixture redirects HOME to a temp dir. Modal
# credentials must come from the real home, not the per-test temp home.
_REAL_MODAL_TOML_PATH = Path(os.path.expanduser("~/.modal.toml"))


@pytest.fixture(autouse=True)
def _load_modal_test_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load Modal credentials from the real ~/.modal.toml into the test env.

    This complements -- rather than overrides -- the base autouse
    setup_test_mngr_env that every mngr plugin gets via
    register_plugin_test_fixtures: that fixture isolates HOME, and this one
    layers the Modal tokens on top so real-Modal tests can authenticate.

    The two are independent autouse fixtures setting independent env vars
    (HOME vs MODAL_TOKEN_*), so their relative order does not matter. The real
    ~/.modal.toml path is captured at import time, so reading it is unaffected by
    the HOME override regardless of which fixture runs first. Consuming packages
    (e.g. mngr_claude) pull this in via pytest_plugins without it clobbering
    their base HOME isolation.
    """
    if not _REAL_MODAL_TOML_PATH.exists():
        return
    for value in toml.load(_REAL_MODAL_TOML_PATH).values():
        if value.get("active", ""):
            monkeypatch.setenv("MODAL_TOKEN_ID", value.get("token_id", ""))
            monkeypatch.setenv("MODAL_TOKEN_SECRET", value.get("token_secret", ""))
            break


@pytest.fixture(scope="session")
def modal_test_session_env_name() -> str:
    """Generate a unique, timestamp-based environment name for this test session.

    In shared-env mode (MNGR_TEST_SHARED_MODAL_ENV_NAME set), returns the
    bare timestamp portion of the shared env name so the session-scoped
    subprocess-env fixture lands in the same Modal environment as the
    function-scoped fixtures. Same shape as ``generate_test_environment_name()``
    -- callers join with ``-`` to build their full prefix.
    """
    shared = read_shared_modal_env_name()
    if shared is not None:
        timestamp_name, _ = shared
        return timestamp_name
    return generate_test_environment_name()


@pytest.fixture(scope="session")
def modal_test_session_host_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a session-scoped host directory for Modal tests."""
    host_dir = tmp_path_factory.mktemp("modal_session") / "mngr"
    host_dir.mkdir(parents=True, exist_ok=True)
    return host_dir


@pytest.fixture(scope="session")
def modal_test_session_user_id() -> UserId:
    """Generate a deterministic user ID for the test session.

    In shared-env mode, returns the user_id suffix from
    MNGR_TEST_SHARED_MODAL_ENV_NAME so subprocess tests target the same
    Modal environment as the in-process fixtures.
    """
    shared = read_shared_modal_env_name()
    if shared is not None:
        _, user_id_suffix = shared
        return UserId(user_id_suffix)
    return UserId(uuid4().hex)


@pytest.fixture(scope="session")
def modal_test_session_cleanup(
    modal_test_session_env_name: str,
    modal_test_session_user_id: UserId,
) -> Generator[None, None, None]:
    """Session-scoped fixture that cleans up the Modal environment at session end.

    In shared-env mode the env is shared across many concurrent offload
    sandboxes, so this fixture skips the env-wide app/volume sweep entirely
    -- ``delete_modal_apps_in_environment`` / ``delete_modal_volumes_in_environment``
    enumerate every resource in the env, which would stop other sandboxes'
    in-flight apps and delete their live volumes. The justfile wrapper
    deletes the env outright on EXIT, which per Modal's ``modal environment
    delete`` confirmation message cascades to every Modal resource scoped
    to the env (Apps, Volumes, Secrets, Dicts, Queues). Function-scoped
    fixtures (``real_modal_provider`` and friends) still delete their own
    volumes individually before the env goes away; subprocess-style tests
    do not -- they rely entirely on that env-delete cascade. The env
    itself is also not deleted or registered for leak detection here.
    """
    if read_shared_modal_env_name() is not None:
        yield
        return
    prefix = f"{modal_test_session_env_name}-"
    environment_name = f"{prefix}{modal_test_session_user_id}"
    if len(environment_name) > 64:
        environment_name = environment_name[:64]
    register_modal_test_environment(environment_name)
    yield
    delete_modal_apps_in_environment(environment_name)
    delete_modal_volumes_in_environment(environment_name)
    # Deregister only on DELETED/NOT_FOUND (synchronous response is
    # authoritative). FAILED leaves the env tracked so the session-end
    # leak detector still surfaces it. See `_cleanup_modal_test_resources`
    # docstring for the NOT_FOUND-treated-as-success limitation and safety net.
    _apply_cleanup_outcome(
        outcome=delete_modal_environment(environment_name),
        deregister=lambda: deregister_modal_test_environment(environment_name),
        resource_description=f"session environment {environment_name}",
    )


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
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning("Failed to list leaked modal apps: {}", e)
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
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning("Failed to list leaked modal volumes: {}", e)
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
            logger.warning(
                "`modal environment list --json` returned non-zero ({}): {}",
                result.returncode,
                (result.stderr or result.stdout).strip(),
            )
            return []
        envs = json.loads(result.stdout)
        return [e.get("name", "") for e in envs if e.get("name", "") in worker_modal_environment_names]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning("Failed to list leaked modal environments: {}", e)
        return []


def _delete_modal_environments(environment_names: list[str]) -> None:
    for name in environment_names:
        try:
            subprocess.run(
                ["uv", "run", "modal", "environment", "delete", name, "--yes"], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
            pass


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Detect and clean up leaked Modal resources at the very end of the test session.

    Implemented as a pytest hook (not a fixture) so it runs AFTER all session-
    scoped fixture teardowns -- including `modal_test_session_cleanup`'s
    deregister chain. Fixture-dependency ordering alone is insufficient
    because pytest's autouse session-scoped fixtures tear down before non-
    autouse session-scoped fixtures regardless of declared dependencies, so
    an autouse leak-check fixture would poll a still-registered env. The
    `pytest_sessionfinish` hook runs after every session-scoped fixture
    teardown and bypasses fixture-ordering entirely.

    Failure mode: writes a loud error to stderr/loguru and, if the session
    was otherwise passing (`session.exitstatus == 0`), sets
    `session.exitstatus` to TESTS_FAILED. If the session was already
    failing for a more-specific reason (any non-zero `session.exitstatus`,
    e.g. INTERRUPTED/INTERNAL_ERROR), preserve it -- those codes carry
    strictly more diagnostic information than TESTS_FAILED. Does not
    raise -- raising from `pytest_sessionfinish` is silently dropped by
    pytest. The `exitstatus` parameter is required by the hook signature
    but unused; we read `session.exitstatus` instead so the check follows
    the canonical session-state accessor.
    """
    # exitstatus is unused; del to satisfy ruff ARG001.
    del exitstatus
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
        message = (
            "=" * 70
            + "\nMODAL SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
            + "=" * 70
            + "\n\n"
            + "\n\n".join(errors)
            + "\n\nThese resources have been cleaned up, but tests should not leak!\n"
        )
        logger.error(message)
        # Force the test session to fail. Raising from pytest_sessionfinish
        # is silently swallowed; setting exitstatus on the session is the
        # documented way to signal failure from this hook. Only overwrite
        # a successful status: a non-zero status (INTERRUPTED=2,
        # INTERNAL_ERROR=3, USAGE_ERROR=4, NO_TESTS_COLLECTED=5) carries
        # strictly more diagnostic information than TESTS_FAILED=1, so
        # downgrading would hide the real reason CI failed.
        if session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


# =============================================================================
# Testing Modal Interface fixtures
#
# These fixtures provide a ModalProviderInstance backed by FakeModalInterface
# for testing mngr_modal business logic without Modal credentials or SSH.
# =============================================================================


@pytest.fixture
def testing_modal(tmp_path: Path, cg: ConcurrencyGroup) -> FakeModalInterface:
    return make_testing_modal_interface(tmp_path, cg)


@pytest.fixture
def testing_provider(
    temp_mngr_ctx: MngrContext,
    testing_modal: FakeModalInterface,
) -> Generator[ModalProviderInstance, None, None]:
    provider = make_testing_provider(temp_mngr_ctx, testing_modal)
    yield provider
    testing_modal.cleanup()


@pytest.fixture
def testing_provider_no_host_volume(
    temp_mngr_ctx: MngrContext,
    testing_modal: FakeModalInterface,
) -> Generator[ModalProviderInstance, None, None]:
    provider = make_testing_provider(temp_mngr_ctx, testing_modal, is_host_volume_created=False)
    yield provider
    testing_modal.cleanup()
