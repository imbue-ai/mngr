from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Generator

import modal
import modal.exception
import pluggy
import pytest
from modal.environments import delete_environment

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import ConfigStructureError
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.modal.backend import ModalProviderBackend
from imbue.mng.providers.modal.backend import STATE_VOLUME_SUFFIX
from imbue.mng.providers.modal.config import ModalProviderConfig
from imbue.mng.providers.modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng.utils.testing import TEST_ENV_PREFIX
from imbue.mng.utils.testing import make_mng_ctx
from imbue.mng.utils.testing import register_modal_test_app
from imbue.mng.utils.testing import register_modal_test_environment
from imbue.mng.utils.testing import register_modal_test_volume


def make_modal_provider_real(
    mng_ctx: MngContext,
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
        host_dir=Path("/mng"),
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
        mng_ctx=mng_ctx,
    )
    if not isinstance(instance, ModalProviderInstance):
        raise ConfigStructureError(f"Expected ModalProviderInstance, got {type(instance).__name__}")
    return instance


@pytest.fixture
def modal_mng_ctx(
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    cg: ConcurrencyGroup,
) -> MngContext:
    """Create a MngContext with a timestamp-based prefix for Modal acceptance tests.

    Uses the mng_test-YYYY-MM-DD-HH-MM-SS- prefix format so that environments
    created by these tests are visible to the CI cleanup script
    (cleanup_old_modal_test_environments.py), providing a safety net if
    per-test fixture cleanup fails.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
    prefix = f"{TEST_ENV_PREFIX}{timestamp}-"
    config = MngConfig(default_host_dir=temp_host_dir, prefix=prefix)
    return make_mng_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)


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
def real_modal_provider(modal_mng_ctx: MngContext, mng_test_id: str) -> Generator[ModalProviderInstance, None, None]:
    """Create a ModalProviderInstance with real Modal for acceptance tests.

    This fixture creates a Modal environment and cleans it up after the test.
    Cleanup happens in the fixture teardown (not at session end) to prevent
    environment leaks and reduce the time spent on cleanup.

    Uses modal_mng_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    provider = make_modal_provider_real(modal_mng_ctx, app_name)
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
    modal_mng_ctx: MngContext, mng_test_id: str
) -> Generator[ModalProviderInstance, None, None]:
    """Create a persistent ModalProviderInstance for testing shutdown script creation.

    This fixture is similar to real_modal_provider but uses is_persistent=True,
    which enables the shutdown script feature.

    Uses modal_mng_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    provider = make_modal_provider_real(modal_mng_ctx, app_name, is_persistent=True)
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
    modal_mng_ctx: MngContext, mng_test_id: str
) -> Generator[ModalProviderInstance, None, None]:
    """Create a ModalProviderInstance with is_snapshotted_after_create=True.

    Use this fixture for tests that specifically test initial snapshot behavior,
    such as restarting a host after hard kill using the initial snapshot.

    Uses modal_mng_ctx (with timestamp-based prefix) so leaked environments
    are visible to the CI cleanup script as a safety net.
    """
    app_name = f"{MODAL_TEST_APP_PREFIX}{mng_test_id}"
    provider = make_modal_provider_real(modal_mng_ctx, app_name, is_snapshotted_after_create=True)
    environment_name = provider.environment_name
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    # Register resources for leak detection
    register_modal_test_app(app_name)
    register_modal_test_environment(environment_name)
    register_modal_test_volume(volume_name)

    yield provider

    _cleanup_modal_test_resources(app_name, volume_name, environment_name)
