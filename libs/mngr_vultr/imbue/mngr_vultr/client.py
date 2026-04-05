import base64
import time
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

import requests
from loguru import logger
from pydantic import Field

from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.errors import VpsProvisioningError
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.primitives import VpsSnapshotId
from imbue.mngr_vps_docker.vps_client import VpsClientInterface
from imbue.mngr_vps_docker.vps_client import VpsSnapshotInfo
from imbue.mngr_vps_docker.vps_client import VpsSshKeyInfo

_VULTR_API_BASE: Final[str] = "https://api.vultr.com/v2"

# Mapping from Vultr status strings to our enum
_STATUS_MAP: Final[dict[str, VpsInstanceStatus]] = {
    "pending": VpsInstanceStatus.PENDING,
    "active": VpsInstanceStatus.ACTIVE,
    "halted": VpsInstanceStatus.HALTED,
    "suspended": VpsInstanceStatus.HALTED,
}


class VultrVpsClient(VpsClientInterface):
    """Vultr API v2 client using raw HTTP calls."""

    api_key: str = Field(frozen=True, description="Vultr API key")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Make an HTTP request to the Vultr API."""
        url = f"{_VULTR_API_BASE}{path}"
        logger.trace("Vultr API {} {}", method, path)

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self._headers(),
                json=json_data,
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise VpsApiError(0, f"Request failed: {e}") from e

        if response.status_code == 204:
            return None

        if not response.ok:
            try:
                error_data = response.json()
                error_msg = error_data.get("error", response.text)
            except requests.JSONDecodeError:
                error_msg = response.text
            raise VpsApiError(response.status_code, str(error_msg))

        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return None

    def _get(self, path: str) -> dict[str, Any] | None:
        return self._request("GET", path)

    def _post(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self._request("POST", path, json_data=data)

    def _delete(self, path: str) -> None:
        self._request("DELETE", path)

    # =========================================================================
    # Instance Operations
    # =========================================================================

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        os_id: int,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Sequence[str],
    ) -> VpsInstanceId:
        # Vultr requires user_data to be base64-encoded
        user_data_b64 = base64.b64encode(user_data.encode()).decode()

        data: dict[str, Any] = {
            "region": region,
            "plan": plan,
            "os_id": os_id,
            "label": label,
            "user_data": user_data_b64,
            "sshkey_id": list(ssh_key_ids),
            "tags": list(tags),
            "backups": "disabled",
            "hostname": label,
        }

        result = self._post("/instances", data)
        if result is None or "instance" not in result:
            raise VpsProvisioningError("Failed to create Vultr instance: no instance in response")

        instance_id = result["instance"]["id"]
        logger.info("Created Vultr instance {} (label: {}, region: {}, plan: {})", instance_id, label, region, plan)
        return VpsInstanceId(instance_id)

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self._delete(f"/instances/{instance_id}")
        logger.info("Destroyed Vultr instance {}", instance_id)

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        result = self._get(f"/instances/{instance_id}")
        if result is None or "instance" not in result:
            return VpsInstanceStatus.UNKNOWN

        status_str = result["instance"].get("status", "unknown")
        power_status = result["instance"].get("power_status", "unknown")

        if status_str == "active" and power_status == "running":
            return VpsInstanceStatus.ACTIVE
        elif status_str == "active" and power_status == "stopped":
            return VpsInstanceStatus.HALTED
        else:
            return _STATUS_MAP.get(status_str, VpsInstanceStatus.UNKNOWN)

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        result = self._get(f"/instances/{instance_id}")
        if result is None or "instance" not in result:
            raise VpsApiError(404, f"Instance {instance_id} not found")

        main_ip = result["instance"].get("main_ip", "0.0.0.0")
        if main_ip == "0.0.0.0":
            raise VpsProvisioningError(f"Instance {instance_id} does not have an IP yet")
        return main_ip

    def wait_for_instance_active(
        self,
        instance_id: VpsInstanceId,
        timeout_seconds: float = 300.0,
    ) -> str:
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            status = self.get_instance_status(instance_id)
            if status == VpsInstanceStatus.ACTIVE:
                try:
                    ip = self.get_instance_ip(instance_id)
                    elapsed = time.monotonic() - start
                    if elapsed > 60.0:
                        logger.warning("VPS provisioning took {:.1f}s (threshold: 60s)", elapsed)
                    return ip
                except VpsProvisioningError:
                    pass
            time.sleep(5.0)

        raise VpsProvisioningError(
            f"Vultr instance {instance_id} did not become active within {timeout_seconds}s"
        )

    def get_instance_info(self, instance_id: VpsInstanceId) -> dict[str, Any]:
        """Get full instance info from the API."""
        result = self._get(f"/instances/{instance_id}")
        if result is None or "instance" not in result:
            raise VpsApiError(404, f"Instance {instance_id} not found")
        return result["instance"]

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]:
        """List all instances, optionally filtered by tag."""
        params = f"?tag={tag}" if tag else ""
        result = self._get(f"/instances{params}")
        if result is None or "instances" not in result:
            return []
        return result["instances"]

    # =========================================================================
    # Snapshot Operations
    # =========================================================================

    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        result = self._post("/snapshots", {"instance_id": str(instance_id), "description": description})
        if result is None or "snapshot" not in result:
            raise VpsApiError(500, "Failed to create snapshot")
        snapshot_id = result["snapshot"]["id"]
        logger.info("Created Vultr snapshot {}", snapshot_id)
        return VpsSnapshotId(snapshot_id)

    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        self._delete(f"/snapshots/{snapshot_id}")
        logger.info("Deleted Vultr snapshot {}", snapshot_id)

    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        result = self._get("/snapshots")
        if result is None or "snapshots" not in result:
            return []

        snapshots: list[VpsSnapshotInfo] = []
        for snap in result["snapshots"]:
            created_str = snap.get("date_created", "")
            try:
                created_at = datetime.fromisoformat(created_str)
            except ValueError:
                created_at = datetime.now(timezone.utc)

            snapshots.append(
                VpsSnapshotInfo(
                    id=VpsSnapshotId(snap["id"]),
                    description=snap.get("description", ""),
                    created_at=created_at,
                )
            )
        return snapshots

    # =========================================================================
    # SSH Key Operations
    # =========================================================================

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        result = self._post("/ssh-keys", {"name": name, "ssh_key": public_key})
        if result is None or "ssh_key" not in result:
            raise VpsApiError(500, "Failed to upload SSH key")
        key_id = result["ssh_key"]["id"]
        logger.debug("Uploaded SSH key {} ({})", name, key_id)
        return key_id

    def delete_ssh_key(self, key_id: str) -> None:
        self._delete(f"/ssh-keys/{key_id}")
        logger.debug("Deleted SSH key {}", key_id)

    def list_ssh_keys(self) -> list[VpsSshKeyInfo]:
        result = self._get("/ssh-keys")
        if result is None or "ssh_keys" not in result:
            return []
        return [
            VpsSshKeyInfo(id=k["id"], name=k["name"])
            for k in result["ssh_keys"]
        ]
