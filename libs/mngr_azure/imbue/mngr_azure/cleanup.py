"""Azure adapter for the shared, age-based VPS test-instance reaper.

The reaper logic lives in ``imbue.mngr_vps.leak_cleanup`` (shared across every VPS provider).
This module supplies only Azure's plumbing: the ISO ``mngr-created-at`` tag and the
``mngr-pytest-launched`` marker ``AzureVpsClient`` stamps on every VM, and the matching
``CreatedAtExtractor`` that reads them back from ``list_instances()``.

Orphaned NICs / public IPs (left by a create that fails after provisioning the network but
before the VM) are reclaimed by the client's own production ``reclaim_orphaned_network_resources``
(the same sweep ``mngr gc`` runs), so the reaper does not reimplement it. An instance is reapable
only when it carries the pytest-launched marker (so production VMs are never touched) and a
parseable ``mngr-created-at`` timestamp. Driven by the session-end hook in ``conftest.py`` and the
standalone CI reaper ``scripts/cleanup_old_azure_test_instances.py``.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Final
from typing import Protocol

from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.client import AZURE_PYTEST_LAUNCHED_TAG
from imbue.mngr_vps.leak_cleanup import cleanup_old_test_instances
from imbue.mngr_vps.leak_cleanup import has_launched_marker
from imbue.mngr_vps.leak_cleanup import parse_iso_utc
from imbue.mngr_vps.leak_cleanup import parse_tag_value
from imbue.mngr_vps.primitives import VpsInstanceId

# ISO ``mngr-created-at`` tag ``AzureVpsClient.create_instance`` stamps on every VM.
AZURE_CREATED_AT_TAG_KEY: Final[str] = "mngr-created-at"

# Synthetic provider name for the network-reclaim sweep's returned resource records; the reaper
# does not key on it, but ``reclaim_orphaned_network_resources`` requires one.
AZURE_REAPER_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("azure-test-reaper")


class AzureReaperClient(Protocol):
    """The slice of ``AzureVpsClient`` the Azure reaper needs.

    Extends the shared ``VpsReaperClient`` contract (``list_instances`` + ``destroy_instance``)
    with Azure's production ``reclaim_orphaned_network_resources`` (which the cross-provider reaper
    has no notion of). Typing against this Protocol -- rather than the concrete ``AzureVpsClient``
    -- lets a lightweight fake stand in for unit tests. ``list_instances`` is declared with no
    arguments for the same reason as the shared Protocol (the reaper never passes the filter).
    """

    def list_instances(self) -> list[dict[str, Any]]: ...

    def destroy_instance(self, instance_id: VpsInstanceId) -> None: ...

    def reclaim_orphaned_network_resources(
        self, provider_name: ProviderInstanceName, dry_run: bool = False
    ) -> list[Any]: ...


def azure_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a pytest-launched VM's UTC creation time, or ``None`` to leave it alone.

    Reapable iff the VM carries the ``mngr-pytest-launched`` marker (so production VMs are never
    matched) and a parseable ISO ``mngr-created-at`` tag.
    """
    if not has_launched_marker(instance, AZURE_PYTEST_LAUNCHED_TAG):
        return None
    return parse_iso_utc(parse_tag_value(instance.get("tags", ()), AZURE_CREATED_AT_TAG_KEY))


def cleanup_old_azure_test_instances(client: AzureReaperClient, max_age: timedelta, now: datetime) -> int:
    """Reclaim orphaned network, then destroy pytest-launched VMs older than ``max_age``.

    Returns the number of VMs cleaned up. The network reclaim is best-effort (the client logs and
    swallows its own failures, since orphaned NICs/IPs are an Azure-capacity artifact, not a test
    bug). VM reaping surfaces failures via the shared reaper: a scan error propagates and a non-404
    destroy failure raises ``VpsLeakCleanupError``.
    """
    client.reclaim_orphaned_network_resources(AZURE_REAPER_PROVIDER_NAME)
    return cleanup_old_test_instances(client, azure_test_created_at, max_age, now)
