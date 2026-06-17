"""Pytest fixtures and session-finish leak detection for mngr_azure tests.

Modeled after ``libs/mngr_aws/imbue/mngr_aws/conftest.py``. Provides a safety
net for Azure release tests so killed test runs cannot leak VM cost:

1. Per-test cleanup happens via ``mngr destroy --force`` in each test's
   ``finally`` block (in ``test_release_azure.py``).
2. ``pytest_sessionfinish`` here scans the mngr resource group for VMs tagged
   ``mngr-pytest-launched=true`` at the end of the session, force-deletes any
   matches older than the TTL, and fails the session.
3. Cloud-init in each test VM runs ``shutdown -P +N`` (best-effort: on Azure an
   OS shutdown leaves the VM Stopped-but-allocated, which still bills compute,
   so layer 2 is the real cost backstop -- unlike AWS/GCP, Azure has no native
   delete-after-duration).

The scan filters on the ``mngr-pytest-launched`` tag and ignores anything
younger than ``_TEST_LEAK_TTL`` (parsed from the ``mngr-created-at`` tag) so it
never race-kills an in-flight test on a parallel worker.

Also registers the shared plugin-test fixtures (including ``temp_mngr_ctx``) so
backend-level unit tests can construct real provider instances.
"""

import os
from collections.abc import Generator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from loguru import logger

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import setup_mngr_test_environment
from imbue.mngr_azure.client import AZURE_PYTEST_LAUNCHED_TAG
from imbue.mngr_azure.testing import AZURE_DEFAULT_REGION
from imbue.mngr_azure.testing import AZURE_DEFAULT_RESOURCE_GROUP
from imbue.mngr_azure.testing import AZURE_RELEASE_TESTS_OPT_IN
from imbue.mngr_azure.testing import AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_azure.testing import azure_credentials_available
from imbue.mngr_azure.testing import get_default_subscription_id

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def setup_test_mngr_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Override mngr's autouse env setup to keep Azure CLI auth across HOME swap.

    HOME isolation hides ``~/.azure`` from the test process, which makes
    ``DefaultAzureCredential``'s ``AzureCliCredential`` fail (the ``az`` token
    cache lives there) even when the real shell has working creds. Pin
    ``AZURE_CONFIG_DIR`` to the real ``~/.azure`` *before* HOME is swapped so the
    credential keeps resolving. A no-op when ``az login`` was never run (the
    release-test ``skipif`` then takes over).
    """
    real_azure_config = os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")
    monkeypatch.setenv("AZURE_CONFIG_DIR", real_azure_config)
    setup_mngr_test_environment(tmp_home_dir, temp_host_dir, mngr_test_prefix, mngr_test_root_name, monkeypatch)
    yield


# Orphan-scan grace period. A test-tagged VM younger than this is left alone to
# avoid race-killing an in-flight test on a parallel worker. Derived from the
# shared ``AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS`` constant (the same value
# release tests propagate into cloud-init) so the two TTLs can never drift.
_TEST_LEAK_TTL: Final[timedelta] = timedelta(seconds=AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS)


def _force_delete_vms(compute: Any, vm_names: list[str]) -> None:
    for vm_name in vm_names:
        try:
            # Await the delete (``.result()``): ``begin_delete`` returns an
            # LROPoller immediately, so a server-side delete failure only
            # surfaces on ``.result()``. Without it, the ``except`` arm can only
            # catch request-submission errors and a delete the server later
            # rejects is silently dropped -- the session would then report the
            # leak as cleaned when it was not. Mirrors the production
            # ``destroy_instance`` path (which awaits) and the GCP conftest fix.
            compute.virtual_machines.begin_delete(AZURE_DEFAULT_RESOURCE_GROUP, vm_name).result()
        except AzureError as e:
            logger.warning("Failed to delete leaked Azure VM {}: {}", vm_name, e)


def _find_orphan_test_vms(compute: Any) -> list[str]:
    """Return VM names tagged ``mngr-pytest-launched=true`` and older than the TTL.

    ``AzureVpsClient.create_instance`` adds the tag to every VM launched while
    ``PYTEST_CURRENT_TEST`` is set, plus an ISO ``mngr-created-at`` tag used here
    for the age check (Azure VM list does not reliably expose creation time).
    Younger VMs are intentionally skipped so this never races a parallel worker's
    in-flight test.
    """
    cutoff = datetime.now(timezone.utc) - _TEST_LEAK_TTL
    leaked: list[str] = []
    try:
        for vm in compute.virtual_machines.list(AZURE_DEFAULT_RESOURCE_GROUP):
            tags = dict(vm.tags or {})
            if tags.get(AZURE_PYTEST_LAUNCHED_TAG) != "true":
                continue
            created_raw = tags.get("mngr-created-at")
            if created_raw is None:
                continue
            try:
                created_at = datetime.fromisoformat(created_raw)
            except ValueError:
                continue
            if created_at < cutoff:
                leaked.append(vm.name)
    except AzureError as e:
        logger.warning("Failed to scan for orphaned Azure test VMs: {}", e)
    return leaked


def _reclaim_orphan_test_network(network: Any) -> None:
    """Best-effort delete unattached pytest-launched NICs / public IPs at session end.

    A test create that fails *after* provisioning the NIC + public IP but before
    the VM (e.g. ``SkuNotAvailable``) leaves those orphaned, and the production
    reclaim runs at GC time (``mngr gc``) -- which may never run in a finished
    session. These are not a test bug (they stem from Azure capacity), so they are
    cleaned silently rather than failing the session. NICs go first (they hold the
    public IPs). Only mngr pytest-launched resources are touched.
    """
    cutoff = datetime.now(timezone.utc) - _TEST_LEAK_TTL
    try:
        nics = list(network.network_interfaces.list(AZURE_DEFAULT_RESOURCE_GROUP))
    except AzureError as e:
        logger.warning("Failed to list NICs for session-end orphan reclaim: {}", e)
        nics = []
    for nic in nics:
        if nic.virtual_machine is not None or not _is_orphan_test_resource(nic, cutoff):
            continue
        try:
            # Await the delete: ``begin_delete`` is async, and awaiting it both
            # surfaces server-side failures into the ``except`` arm (so a failed
            # reclaim is logged, not silently dropped) and ensures the NIC is
            # gone before we try to delete the public IP it holds below.
            network.network_interfaces.begin_delete(AZURE_DEFAULT_RESOURCE_GROUP, nic.name).result()
            logger.info("Reclaimed orphaned test NIC {}", nic.name)
        except AzureError as e:
            logger.warning("Failed to reclaim orphaned test NIC {}: {}", nic.name, e)
    try:
        public_ips = list(network.public_ip_addresses.list(AZURE_DEFAULT_RESOURCE_GROUP))
    except AzureError as e:
        logger.warning("Failed to list public IPs for session-end orphan reclaim: {}", e)
        public_ips = []
    for public_ip in public_ips:
        if public_ip.ip_configuration is not None or not _is_orphan_test_resource(public_ip, cutoff):
            continue
        try:
            # Await the delete so a server-side failure surfaces into the
            # ``except`` arm and is logged rather than silently dropped.
            network.public_ip_addresses.begin_delete(AZURE_DEFAULT_RESOURCE_GROUP, public_ip.name).result()
            logger.info("Reclaimed orphaned test public IP {}", public_ip.name)
        except AzureError as e:
            logger.warning("Failed to reclaim orphaned test public IP {}: {}", public_ip.name, e)


def _is_orphan_test_resource(resource: Any, cutoff: datetime) -> bool:
    tags = dict(resource.tags or {})
    if tags.get(AZURE_PYTEST_LAUNCHED_TAG) != "true":
        return False
    created_raw = tags.get("mngr-created-at")
    if created_raw is None:
        return False
    try:
        created_at = datetime.fromisoformat(created_raw)
    except ValueError:
        return False
    return created_at < cutoff


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Detect and clean up leaked Azure resources at session end.

    Implemented as a pytest hook (not a fixture) so it runs after every
    session-scoped fixture teardown. Skips silently unless release tests were
    actually opted into (``MNGR_AZURE_RELEASE_TESTS=1``), credentials are
    available, and a subscription is resolvable -- the same conjunction that
    gates the release-test ``pytestmark``, so the cleanup hook never makes a live
    Azure call from a run that did not opt into Azure-using tests. Leaked VMs
    force-fail the session (a real test bug); orphaned NICs / public IPs from
    capacity-failed creates are reclaimed silently. The session exit status is set
    to ``TESTS_FAILED`` only for VM leaks, and only when the session was otherwise
    passing so a more-specific failure (INTERRUPTED, INTERNAL_ERROR, etc.) is
    preserved.
    """
    del exitstatus
    if not (AZURE_RELEASE_TESTS_OPT_IN and azure_credentials_available()):
        return
    subscription_id = get_default_subscription_id()
    if subscription_id is None:
        return

    try:
        compute = ComputeManagementClient(DefaultAzureCredential(), subscription_id)
        network = NetworkManagementClient(DefaultAzureCredential(), subscription_id)
    except AzureError as e:
        logger.warning("Failed to build Azure clients for session-end leak scan: {}", e)
        return

    _reclaim_orphan_test_network(network)

    orphans = _find_orphan_test_vms(compute)
    if not orphans:
        return

    _force_delete_vms(compute, orphans)
    message = (
        "=" * 70
        + "\nAZURE SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
        + "=" * 70
        + f"\n\nLeaked Azure VMs tagged {AZURE_PYTEST_LAUNCHED_TAG}=true and "
        + f"older than {AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS // 60} minutes in "
        + f"resource group {AZURE_DEFAULT_RESOURCE_GROUP} (region {AZURE_DEFAULT_REGION}):\n  "
        + "\n  ".join(orphans)
        + "\n\nVMs have been force-deleted, but tests should not leak.\n"
    )
    logger.error(message)
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
