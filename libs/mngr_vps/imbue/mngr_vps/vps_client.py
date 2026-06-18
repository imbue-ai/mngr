import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_vps.errors import VpsError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus


class VpsClientInterface(MutableModel, ABC):
    """Abstract interface for VPS provider API operations.

    Each method maps to a single API call. The VPS Docker provider layer
    composes these into higher-level operations.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """Provision a new VPS instance. Returns the instance ID.

        Provider-specific image selection (AMI on AWS, OS image ID on Vultr)
        is configured on the client at construction time, not passed per
        call. To target a different image for a specific create, instantiate
        a separate client.
        """
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Permanently destroy a VPS instance."""
        ...

    @abstractmethod
    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        """Get the current status of a VPS instance."""
        ...

    @abstractmethod
    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        """Get the main IPv4 address of a VPS instance."""
        ...

    slow_provisioning_warning_threshold_seconds: float = Field(
        default=60.0,
        description=(
            "If wait_for_instance_active takes longer than this, emit a warning. "
            "Per-provider override: e.g. EC2 boots are routinely slower than Vultr."
        ),
    )

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        """Poll until instance is active and return its IP address.

        Default implementation: every 5 seconds, call ``get_instance_status``
        until it returns ``ACTIVE``, then call ``get_instance_ip`` (retrying
        if it raises ``VpsProvisioningError`` because the IP hasn't been
        assigned yet). Warns at the end of the call if provisioning took
        longer than ``slow_provisioning_warning_threshold_seconds``. Provider
        clients with an unusual lifecycle (e.g. OVH's multi-step order +
        rebuild + TOFU) should override.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            status = self.get_instance_status(instance_id)
            if status == VpsInstanceStatus.ACTIVE:
                try:
                    ip = self.get_instance_ip(instance_id)
                    elapsed = time.monotonic() - start
                    threshold = self.slow_provisioning_warning_threshold_seconds
                    if elapsed > threshold:
                        logger.warning(
                            "VPS {} provisioning took {:.1f}s (threshold: {:.0f}s)",
                            instance_id,
                            elapsed,
                            threshold,
                        )
                    return ip
                except VpsProvisioningError as e:
                    # Expected transient: the instance reports ACTIVE a moment
                    # before its IP is assigned. Swallow and retry on the next
                    # poll; debug-level so a stuck provision is still traceable
                    # without spamming a line every 5s on the happy path.
                    logger.debug("VPS {} is active but has no IP yet, retrying: {}", instance_id, e)
            time.sleep(5.0)
        raise VpsProvisioningError(f"VPS instance {instance_id} did not become active within {timeout_seconds}s")

    @abstractmethod
    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Upload an SSH public key. Returns the key ID."""
        ...

    @abstractmethod
    def delete_ssh_key(self, key_id: str) -> None:
        """Delete an SSH key by its ID."""
        ...


class ExternallyManagedVpsClient(VpsClientInterface):
    """A VPS client for VPSes that this process does not own and cannot order.

    Used when a ``VpsProvider`` operates purely on an already-existing,
    externally-provisioned VPS (e.g. ``mngr_imbue_cloud``'s slow path rebuilds
    a container on a leased pool VPS over the SSH access the lease grants).
    Only the container build/teardown methods -- which take an ``outer`` and
    make no VPS-API calls -- are valid in that context; every ordering /
    ssh-key operation raises so a wrong call site fails loudly instead of
    silently misbehaving.
    """

    def _unavailable(self, operation: str) -> VpsError:
        return VpsError(
            f"VPS API operation '{operation}' is unavailable: this VPS is externally managed "
            "(e.g. leased from the imbue_cloud pool) and cannot be ordered or destroyed by "
            "this client."
        )

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise self._unavailable("create_instance")

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        raise self._unavailable("destroy_instance")

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        raise self._unavailable("get_instance_status")

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        raise self._unavailable("get_instance_ip")

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        raise self._unavailable("wait_for_instance_active")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise self._unavailable("upload_ssh_key")

    def delete_ssh_key(self, key_id: str) -> None:
        raise self._unavailable("delete_ssh_key")
