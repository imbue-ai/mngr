"""Out-of-band, age-based reaper for leaked Azure test VMs (and orphaned network).

The session-end leak check in ``conftest.py`` only reaps resources when the
pytest session survives to run ``pytest_sessionfinish``. A session/runner
killed mid-run leaks VMs that only the *next* Azure release session would reap
-- and the release suite is not run in CI, so that next run may be far off.
This module is the age-based reaper that the standalone CI job
(``scripts/cleanup_old_azure_test_instances.py``, run on every push to main and
pull request) drives, mirroring Modal's ``cleanup_old_modal_test_environments``
and Vultr's ``cleanup_old_vultr_test_instances``.

``AzureVpsClient.create_instance`` tags every VM launched while
``PYTEST_CURRENT_TEST`` is set with ``mngr-pytest-launched=true`` plus an ISO
``mngr-created-at`` tag (Azure's VM list does not reliably expose creation
time, so the age check reads that tag). The reaper matches on the
pytest-launched tag -- so production VMs, which never carry it, are never
touched -- and keeps only those older than a max age. The same
``find_old_test_vms`` / ``force_delete_vms`` / ``reclaim_orphan_test_network``
functions back the ``conftest.py`` session-end check, so the two paths can
never drift.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import Any

from azure.core.exceptions import AzureError
from loguru import logger

from imbue.mngr_azure.client import AZURE_PYTEST_LAUNCHED_TAG

# ISO-timestamp tag the client stamps on every pytest-launched resource at
# create time; read here for the age check.
AZURE_CREATED_AT_TAG_KEY = "mngr-created-at"


def is_orphan_test_resource(resource: Any, cutoff: datetime) -> bool:
    """Return True iff ``resource`` is a pytest-launched test resource created before ``cutoff``.

    A resource with no pytest-launched tag, no ``mngr-created-at`` tag, or an
    unparseable timestamp is left alone: we never delete a resource whose age
    we cannot establish from the tags we control.
    """
    tags = dict(resource.tags or {})
    if tags.get(AZURE_PYTEST_LAUNCHED_TAG) != "true":
        return False
    created_raw = tags.get(AZURE_CREATED_AT_TAG_KEY)
    if created_raw is None:
        return False
    try:
        created_at = datetime.fromisoformat(created_raw)
    except ValueError:
        return False
    return created_at < cutoff


def find_old_test_vms(compute: Any, resource_group: str, max_age: timedelta, now: datetime) -> list[str]:
    """Return names of ``mngr-pytest-launched=true`` VMs in ``resource_group`` older than ``max_age``.

    Younger VMs are skipped so neither the session-end check nor the reaper
    ever race-kills an in-flight test on a parallel worker. A scan failure logs
    and yields an empty list rather than raising.
    """
    cutoff = now - max_age
    leaked: list[str] = []
    try:
        for vm in compute.virtual_machines.list(resource_group):
            if is_orphan_test_resource(vm, cutoff):
                leaked.append(vm.name)
    except AzureError as e:
        logger.warning("Failed to scan for orphaned Azure test VMs: {}", e)
    return leaked


def force_delete_vms(compute: Any, resource_group: str, vm_names: Sequence[str]) -> None:
    """Best-effort delete of the given VMs; logs and swallows failures.

    Awaits each delete (``.result()``): ``begin_delete`` returns an LROPoller
    immediately, so a server-side delete failure only surfaces on
    ``.result()``. Mirrors the production ``destroy_instance`` path. Does not
    raise: a failed delete is logged so one stuck VM does not block the rest.
    """
    for vm_name in vm_names:
        try:
            compute.virtual_machines.begin_delete(resource_group, vm_name).result()
        except AzureError as e:
            logger.warning("Failed to delete leaked Azure VM {}: {}", vm_name, e)


def reclaim_orphan_test_network(network: Any, resource_group: str, max_age: timedelta, now: datetime) -> None:
    """Best-effort delete unattached pytest-launched NICs / public IPs older than ``max_age``.

    A test create that fails *after* provisioning the NIC + public IP but
    before the VM (e.g. ``SkuNotAvailable``) leaves those orphaned, and the
    production reclaim runs at GC time (``mngr gc``) -- which may never run.
    These are not a test bug (they stem from Azure capacity), so callers clean
    them silently. NICs go first (they hold the public IPs). Only mngr
    pytest-launched resources are touched.
    """
    cutoff = now - max_age
    try:
        nics = list(network.network_interfaces.list(resource_group))
    except AzureError as e:
        logger.warning("Failed to list NICs for orphan reclaim: {}", e)
        nics = []
    for nic in nics:
        if nic.virtual_machine is not None or not is_orphan_test_resource(nic, cutoff):
            continue
        try:
            # Await the delete: surfaces server-side failures into the except
            # arm and ensures the NIC is gone before deleting the public IP it
            # holds below.
            network.network_interfaces.begin_delete(resource_group, nic.name).result()
            logger.info("Reclaimed orphaned test NIC {}", nic.name)
        except AzureError as e:
            logger.warning("Failed to reclaim orphaned test NIC {}: {}", nic.name, e)
    try:
        public_ips = list(network.public_ip_addresses.list(resource_group))
    except AzureError as e:
        logger.warning("Failed to list public IPs for orphan reclaim: {}", e)
        public_ips = []
    for public_ip in public_ips:
        if public_ip.ip_configuration is not None or not is_orphan_test_resource(public_ip, cutoff):
            continue
        try:
            network.public_ip_addresses.begin_delete(resource_group, public_ip.name).result()
            logger.info("Reclaimed orphaned test public IP {}", public_ip.name)
        except AzureError as e:
            logger.warning("Failed to reclaim orphaned test public IP {}: {}", public_ip.name, e)


def cleanup_old_azure_test_instances(
    compute: Any, network: Any, resource_group: str, max_age: timedelta, now: datetime
) -> int:
    """Reclaim orphaned network, then delete pytest-launched VMs older than ``max_age``.

    Returns the number of VMs targeted for deletion (the cost leak that
    matters). Never raises: scan, reclaim, and delete failures are all logged
    and swallowed.
    """
    reclaim_orphan_test_network(network, resource_group, max_age, now)
    old = find_old_test_vms(compute, resource_group, max_age, now)
    if not old:
        logger.info("No leaked Azure test VMs older than {} found", max_age)
        return 0
    logger.info("Found {} leaked Azure test VM(s) older than {}; deleting", len(old), max_age)
    force_delete_vms(compute, resource_group, old)
    return len(old)
