"""Azure adapter for the shared, age-based VPS test-instance reaper.

Supplies Azure's ``CreatedAtExtractor`` (reads the ``mngr-created-at`` tag) and a thin
wrapper over the shared reaper in ``imbue.mngr_vps.leak_cleanup``. Also reclaims orphaned
NICs / public IPs left by capacity-failed creates.
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

AZURE_CREATED_AT_TAG_KEY: Final[str] = "mngr-created-at"

AZURE_REAPER_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("azure-test-reaper")


class AzureReaperClient(Protocol):
    """The slice of ``AzureVpsClient`` the Azure reaper needs."""

    def list_instances(self) -> list[dict[str, Any]]: ...

    def destroy_instance(self, instance_id: VpsInstanceId) -> None: ...

    def reclaim_orphaned_network_resources(
        self, provider_name: ProviderInstanceName, dry_run: bool = False
    ) -> list[Any]: ...


def azure_test_created_at(instance: Mapping[str, Any]) -> datetime | None:
    """Return a pytest-launched VM's UTC creation time, or ``None`` to leave it alone."""
    if not has_launched_marker(instance, AZURE_PYTEST_LAUNCHED_TAG):
        return None
    return parse_iso_utc(parse_tag_value(instance.get("tags", ()), AZURE_CREATED_AT_TAG_KEY))


def cleanup_old_azure_test_instances(client: AzureReaperClient, max_age: timedelta, now: datetime) -> int:
    """Reclaim orphaned network, then destroy pytest-launched VMs older than ``max_age``."""
    client.reclaim_orphaned_network_resources(AZURE_REAPER_PROVIDER_NAME)
    return cleanup_old_test_instances(client, azure_test_created_at, max_age, now)
