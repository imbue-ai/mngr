"""Client for the host pool endpoints of the remote service connector.

Encapsulates HTTP calls to lease, release, and list pre-provisioned SSH hosts
from the Vultr host pool. Authentication uses the caller's SuperTokens JWT.
"""

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError

_DEFAULT_TIMEOUT_SECONDS = 30.0


class HostPoolError(MindError):
    """Raised when a host pool operation fails."""

    ...


class HostPoolEmptyError(HostPoolError):
    """Raised when no hosts are available in the pool (HTTP 503)."""

    ...


class LeaseHostResult(FrozenModel):
    """Result of a successful host lease from the pool."""

    host_db_id: int = Field(description="Database ID of the leased host")
    vps_ip: str = Field(description="IP address of the VPS")
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned agent ID on the host")
    host_id: str = Field(description="Mngr host ID for the leased host")
    version: str = Field(description="Version tag of the host pool entry")


class LeasedHostInfo(FrozenModel):
    """Information about a currently leased host, including lease timestamp."""

    host_db_id: int = Field(description="Database ID of the leased host")
    vps_ip: str = Field(description="IP address of the VPS")
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned agent ID on the host")
    host_id: str = Field(description="Mngr host ID for the leased host")
    version: str = Field(description="Version tag of the host pool entry")
    leased_at: str = Field(description="ISO 8601 timestamp of when the host was leased")


class HostPoolClient(FrozenModel):
    """Client for the host pool endpoints of the remote service connector.

    Leases, releases, and lists pre-provisioned SSH hosts. All requests
    authenticate via the caller's SuperTokens JWT in the Authorization header.
    """

    connector_url: AnyUrl = Field(description="Base URL of the remote service connector")
    timeout_seconds: float = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        description="HTTP request timeout in seconds",
    )

    def _url(self, path: str) -> str:
        """Join the connector URL with path without introducing a double slash."""
        return str(self.connector_url).rstrip("/") + path

    def lease_host(self, access_token: str, ssh_public_key: str, version: str) -> LeaseHostResult:
        """Lease a pre-provisioned host from the pool.

        Raises HostPoolEmptyError when no hosts with the requested version are
        available (HTTP 503). Raises HostPoolError for all other failures.
        """
        try:
            response = httpx.post(
                self._url("/hosts/lease"),
                headers={"Authorization": "Bearer {}".format(access_token)},
                json={"ssh_public_key": ssh_public_key, "version": version},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise HostPoolError("Host pool lease request failed: {}".format(exc)) from exc

        if response.status_code == 503:
            raise HostPoolEmptyError(response.json().get("detail", "No pre-created agents are currently ready."))

        if response.status_code not in (200, 201):
            raise HostPoolError("Host pool lease failed ({}): {}".format(response.status_code, response.text[:200]))

        return LeaseHostResult.model_validate(response.json())

    def release_host(self, access_token: str, host_db_id: int) -> bool:
        """Release a leased host back to the pool.

        Returns True on success. Logs a warning and returns False on failure.
        """
        try:
            response = httpx.post(
                self._url("/hosts/{}/release".format(host_db_id)),
                headers={"Authorization": "Bearer {}".format(access_token)},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Host pool release request failed: {}", exc)
            return False

        if response.status_code not in (200, 204):
            logger.warning(
                "Host pool release failed for host {}: {} {}",
                host_db_id,
                response.status_code,
                response.text[:200],
            )
            return False

        logger.debug("Released host {} from pool", host_db_id)
        return True

    def list_leased_hosts(self, access_token: str) -> list[LeasedHostInfo]:
        """List all hosts currently leased by the authenticated user.

        Returns an empty list on failure.
        """
        try:
            response = httpx.get(
                self._url("/hosts"),
                headers={"Authorization": "Bearer {}".format(access_token)},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Host pool list request failed: {}", exc)
            return []

        if response.status_code != 200:
            logger.warning(
                "Host pool list failed: {} {}",
                response.status_code,
                response.text[:200],
            )
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Host pool list returned non-JSON response: {}", exc)
            return []

        hosts_raw = data.get("hosts", data) if isinstance(data, dict) else data
        if not isinstance(hosts_raw, list):
            return []

        result: list[LeasedHostInfo] = []
        for item in hosts_raw:
            try:
                result.append(LeasedHostInfo.model_validate(item))
            except ValueError:
                logger.debug("Skipped unparseable host entry: {}", item)
        return result
