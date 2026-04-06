"""Thin Cloudflare API client for tunnel and DNS operations."""

from typing import Any

import httpx
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.cloudflare_forwarding.errors import CloudflareApiError
from imbue.cloudflare_forwarding.primitives import CloudflareAccountId
from imbue.cloudflare_forwarding.primitives import CloudflareDnsRecordId
from imbue.cloudflare_forwarding.primitives import CloudflareTunnelId
from imbue.cloudflare_forwarding.primitives import CloudflareZoneId
from imbue.imbue_common.mutable_model import MutableModel

_BASE_URL = "https://api.cloudflare.com/client/v4"


def create_cloudflare_client(
    api_token: SecretStr,
    account_id: CloudflareAccountId,
    zone_id: CloudflareZoneId,
) -> "CloudflareClient":
    """Create a CloudflareClient configured with the given credentials."""
    http_client = httpx.Client(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {api_token.get_secret_value()}"},
        timeout=30.0,
    )
    return CloudflareClient(http_client=http_client, account_id=account_id, zone_id=zone_id)


class CloudflareClient(MutableModel):
    """Client for Cloudflare tunnel and DNS API operations."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    http_client: httpx.Client = Field(description="The underlying HTTP client")
    account_id: CloudflareAccountId = Field(description="Cloudflare account ID")
    zone_id: CloudflareZoneId = Field(description="Cloudflare zone ID")

    def _check_response(self, response: httpx.Response) -> dict[str, Any]:
        """Check a Cloudflare API response and raise on error."""
        data: dict[str, Any] = response.json()
        if not data.get("success", False):
            raise CloudflareApiError(
                status_code=response.status_code,
                errors=data.get("errors", [{"message": "Unknown error"}]),
            )
        return data

    def create_tunnel(self, name: str) -> dict[str, Any]:
        """Create a new Cloudflare tunnel. Returns the tunnel result object."""
        response = self.http_client.post(
            f"/accounts/{self.account_id}/cfd_tunnel",
            json={"name": name, "config_src": "cloudflare"},
        )
        data = self._check_response(response)
        return data["result"]

    def list_tunnels(self, name_prefix: str = "") -> list[dict[str, Any]]:
        """List tunnels, optionally filtered by name prefix (client-side). Excludes deleted tunnels."""
        params: dict[str, str] = {"is_deleted": "false"}
        response = self.http_client.get(
            f"/accounts/{self.account_id}/cfd_tunnel",
            params=params,
        )
        data = self._check_response(response)
        results: list[dict[str, Any]] = data["result"]
        if name_prefix:
            results = [t for t in results if t["name"].startswith(name_prefix)]
        return results

    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        """Look up a tunnel by exact name. Returns None if not found."""
        params: dict[str, str] = {"is_deleted": "false", "name": name}
        response = self.http_client.get(
            f"/accounts/{self.account_id}/cfd_tunnel",
            params=params,
        )
        data = self._check_response(response)
        results: list[dict[str, Any]] = data["result"]
        for tunnel in results:
            if tunnel["name"] == name:
                return tunnel
        return None

    def get_tunnel_token(self, tunnel_id: CloudflareTunnelId) -> str:
        """Get the connector token for a tunnel."""
        response = self.http_client.get(
            f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/token",
        )
        data = self._check_response(response)
        return data["result"]

    def delete_tunnel(self, tunnel_id: CloudflareTunnelId) -> None:
        """Delete a tunnel."""
        response = self.http_client.delete(
            f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}",
        )
        self._check_response(response)

    def get_tunnel_configuration(self, tunnel_id: CloudflareTunnelId) -> dict[str, Any]:
        """Get the current tunnel configuration (ingress rules, etc.)."""
        response = self.http_client.get(
            f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
        )
        data = self._check_response(response)
        return data["result"]

    def put_tunnel_configuration(self, tunnel_id: CloudflareTunnelId, config: dict[str, Any]) -> None:
        """Replace the tunnel configuration (full PUT)."""
        response = self.http_client.put(
            f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
            json=config,
        )
        self._check_response(response)

    def create_cname_record(self, name: str, target: str) -> dict[str, Any]:
        """Create a proxied CNAME DNS record. Returns the record result object."""
        response = self.http_client.post(
            f"/zones/{self.zone_id}/dns_records",
            json={
                "type": "CNAME",
                "name": name,
                "content": target,
                "proxied": True,
                "ttl": 1,
            },
        )
        data = self._check_response(response)
        return data["result"]

    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]:
        """List CNAME DNS records, optionally filtered by name."""
        params: dict[str, str] = {"type": "CNAME"}
        if name:
            params["name"] = name
        response = self.http_client.get(
            f"/zones/{self.zone_id}/dns_records",
            params=params,
        )
        data = self._check_response(response)
        return data["result"]

    def delete_dns_record(self, record_id: CloudflareDnsRecordId) -> None:
        """Delete a DNS record."""
        response = self.http_client.delete(
            f"/zones/{self.zone_id}/dns_records/{record_id}",
        )
        self._check_response(response)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self.http_client.close()
