"""Client for the Cloudflare forwarding API.

Encapsulates authentication, URL construction, and HTTP calls to the
Modal-hosted cloudflare_forwarding service. Every request authenticates with
the signed-in user's SuperTokens session: the JWT goes in the ``Authorization:
Bearer ...`` header, the user-id prefix is used for tunnel naming, and the
account email backs the default Cloudflare Access policy.
"""

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId


class CloudflareForwardingUrl(AnyUrl):
    """URL of the Cloudflare forwarding API."""

    ...


class CloudflareForwardingClient(FrozenModel):
    """Client for interacting with the Cloudflare forwarding API.

    All requests authenticate via the caller's SuperTokens session. Callers
    build one client per request by enriching the shared ``forwarding_url``
    with the active account's ``supertokens_token`` / ``user_id_prefix`` /
    ``email`` before issuing Cloudflare operations.
    """

    forwarding_url: CloudflareForwardingUrl = Field(description="Base URL of the cloudflare_forwarding API")
    supertokens_token: str | None = Field(default=None, description="SuperTokens JWT access token for Bearer auth")
    supertokens_user_id_prefix: str | None = Field(
        default=None,
        description="First 16 hex chars of SuperTokens user ID for tunnel naming",
    )
    supertokens_email: str | None = Field(
        default=None, description="Email from SuperTokens session for access policies"
    )

    def _auth_header(self) -> str:
        """Return the Bearer header for the current SuperTokens session."""
        if not self.supertokens_token:
            raise ValueError("No supertokens_token configured for cloudflare_forwarding client")
        return f"Bearer {self.supertokens_token}"

    def _effective_username(self) -> str:
        """Return the username used for tunnel naming (derived from the session's user ID)."""
        if not self.supertokens_user_id_prefix:
            raise ValueError("No supertokens_user_id_prefix configured for tunnel naming")
        return self.supertokens_user_id_prefix

    @staticmethod
    def _truncate_agent_id(agent_id: AgentId) -> str:
        """Truncate an agent ID to a 16-char prefix for use in hostnames."""
        return str(agent_id).removeprefix("agent-")[:16]

    def _url(self, path: str) -> str:
        """Join the forwarding URL with ``path`` without introducing a double slash.

        pydantic's AnyUrl normalizes bare origins to have a trailing slash (e.g.
        ``https://example.com`` -> ``https://example.com/``), so naive
        ``f"{self.forwarding_url}{path}"`` can yield double slashes.
        """
        return str(self.forwarding_url).rstrip("/") + path

    def make_tunnel_name(self, agent_id: AgentId) -> str:
        """Build the tunnel name for an agent."""
        return f"{self._effective_username()}--{self._truncate_agent_id(agent_id)}"

    def create_tunnel(self, agent_id: AgentId) -> tuple[str | None, str]:
        """Create a Cloudflare tunnel for the agent and return (token, message).

        Sets a default access policy that allows the session's email. Returns
        ``(token, success_message)`` on success, or ``(None, error_message)``
        on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        default_policy: dict[str, object] | None = None
        if self.supertokens_email:
            default_policy = {
                "rules": [
                    {"action": "allow", "include": [{"email": {"email": self.supertokens_email}}]},
                ],
            }
        request_body: dict[str, object] = {"agent_id": str(agent_id)}
        if default_policy:
            request_body["default_auth_policy"] = default_policy
        try:
            response = httpx.post(
                self._url("/tunnels"),
                headers={"Authorization": self._auth_header()},
                json=request_body,
                timeout=30.0,
            )
            if response.status_code not in (200, 201):
                msg = f"Tunnel creation failed ({response.status_code}): {response.text[:200]}"
                logger.warning(msg)
                return None, msg

            tunnel_info = response.json()
            token = tunnel_info.get("token")
            actual_name = tunnel_info.get("tunnel_name", tunnel_name)
            msg = f"Cloudflare tunnel created: {actual_name}"
            logger.info(msg)
            return token, msg

        except httpx.HTTPError as e:
            msg = f"Tunnel creation failed: {e}"
            logger.warning(msg)
            return None, msg

    def list_services(self, agent_id: AgentId) -> dict[str, str] | None:
        """Query services registered on the agent's tunnel.

        Returns a dict mapping service_name -> hostname, or None on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.get(
                self._url(f"/tunnels/{tunnel_name}/services"),
                headers={"Authorization": self._auth_header()},
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to list services for {}: {} {}", tunnel_name, response.status_code, response.text
                )
                return None
            data = response.json()
            services = data.get("services", data) if isinstance(data, dict) else data
            if not isinstance(services, list):
                services = []
            return {s["service_name"]: s["hostname"] for s in services if "service_name" in s and "hostname" in s}
        except (httpx.HTTPError, KeyError, AttributeError) as e:
            logger.warning("Failed to list services for {}: {}", tunnel_name, e)
            return None

    def add_service(self, agent_id: AgentId, service_name: str, service_url: str) -> bool:
        """Add a service to the agent's tunnel. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.post(
                self._url(f"/tunnels/{tunnel_name}/services"),
                headers={"Authorization": self._auth_header()},
                json={"service_name": service_name, "service_url": service_url},
                timeout=15.0,
            )
            if response.status_code not in (200, 201):
                logger.warning(
                    "Failed to add service {} to {}: {} {}",
                    service_name,
                    tunnel_name,
                    response.status_code,
                    response.text,
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("Failed to add service {} to {}: {}", service_name, tunnel_name, e)
            return False

    def get_tunnel_auth(self, agent_id: AgentId) -> list[dict[str, object]] | None:
        """Get the default auth policy rules for the agent's tunnel.

        Returns the rules list, or None on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.get(
                self._url(f"/tunnels/{tunnel_name}/auth"),
                headers={"Authorization": self._auth_header()},
                timeout=10.0,
            )
            if response.status_code != 200:
                return None
            return response.json().get("rules", [])
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Failed to get tunnel auth for {}: {}", tunnel_name, e)
            return None

    def get_service_auth(self, agent_id: AgentId, service_name: str) -> list[dict[str, object]] | None:
        """Get the auth policy rules for a specific service.

        Returns the rules list, or None on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.get(
                self._url(f"/tunnels/{tunnel_name}/services/{service_name}/auth"),
                headers={"Authorization": self._auth_header()},
                timeout=10.0,
            )
            if response.status_code != 200:
                return None
            return response.json().get("rules", [])
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Failed to get service auth for {}/{}: {}", tunnel_name, service_name, e)
            return None

    def set_service_auth(self, agent_id: AgentId, service_name: str, rules: list[dict[str, object]]) -> bool:
        """Set the auth policy rules for a specific service. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.put(
                self._url(f"/tunnels/{tunnel_name}/services/{service_name}/auth"),
                headers={"Authorization": self._auth_header()},
                json={"rules": rules},
                timeout=15.0,
            )
            return response.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("Failed to set service auth for {}/{}: {}", tunnel_name, service_name, e)
            return False

    def remove_service(self, agent_id: AgentId, service_name: str) -> bool:
        """Remove a service from the agent's tunnel. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.delete(
                self._url(f"/tunnels/{tunnel_name}/services/{service_name}"),
                headers={"Authorization": self._auth_header()},
                timeout=15.0,
            )
            if response.status_code not in (200, 204):
                logger.warning(
                    "Failed to remove service {} from {}: {} {}",
                    service_name,
                    tunnel_name,
                    response.status_code,
                    response.text,
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("Failed to remove service {} from {}: {}", service_name, tunnel_name, e)
            return False

    def delete_tunnel(self, agent_id: AgentId) -> bool:
        """Delete the entire Cloudflare tunnel for an agent. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.delete(
                f"{self.forwarding_url}/tunnels/{tunnel_name}",
                headers={"Authorization": self._auth_header()},
                timeout=30.0,
            )
            if response.status_code not in (200, 204):
                logger.warning(
                    "Failed to delete tunnel {}: {} {}",
                    tunnel_name,
                    response.status_code,
                    response.text,
                )
                return False
            logger.info("Deleted Cloudflare tunnel: {}", tunnel_name)
            return True
        except httpx.HTTPError as e:
            logger.warning("Failed to delete tunnel {}: {}", tunnel_name, e)
            return False
