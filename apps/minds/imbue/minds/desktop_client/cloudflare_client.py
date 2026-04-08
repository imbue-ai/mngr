"""Client for the Cloudflare forwarding API.

Encapsulates authentication, URL construction, and HTTP calls to the
Modal-hosted cloudflare_forwarding service. Created once in runner.py
and passed as a dependency to AgentCreator and the desktop client app.
"""

import base64

import httpx
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.mngr.primitives import AgentId


class CloudflareForwardingUrl(NonEmptyStr):
    """URL of the Cloudflare forwarding API."""

    ...


class CloudflareUsername(NonEmptyStr):
    """Username for Basic auth to the Cloudflare forwarding API."""

    ...


class CloudflareSecret(NonEmptyStr):
    """Secret for Basic auth to the Cloudflare forwarding API."""

    ...


class OwnerEmail(NonEmptyStr):
    """Email address for the default Google OAuth access policy."""

    ...


class CloudflareForwardingClient(FrozenModel):
    """Client for interacting with the Cloudflare forwarding API.

    Uses Basic auth (admin credentials) for tunnel creation and management.
    """

    forwarding_url: CloudflareForwardingUrl = Field(description="Base URL of the cloudflare_forwarding API")
    username: CloudflareUsername = Field(description="Username for admin Basic auth")
    secret: CloudflareSecret = Field(description="Secret for admin Basic auth")
    owner_email: OwnerEmail = Field(description="Email for default Google OAuth policy")

    def _auth_header(self) -> str:
        """Build the Basic auth header value."""
        credentials = f"{self.username}:{self.secret}"
        return "Basic " + base64.b64encode(credentials.encode()).decode()

    def make_tunnel_name(self, agent_id: AgentId) -> str:
        """Build the tunnel name for an agent."""
        return f"{self.username}--{agent_id}"

    def create_tunnel(self, agent_id: AgentId) -> tuple[str | None, str]:
        """Create a Cloudflare tunnel for the agent and return (token, message).

        Sets a default Google OAuth policy for the owner's email.
        Returns (token, success_message) on success, or (None, error_message) on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.post(
                f"{self.forwarding_url}/tunnels",
                headers={"Authorization": self._auth_header()},
                json={
                    "agent_id": str(agent_id),
                    "default_auth_policy": {
                        "rules": [
                            {"action": "allow", "include": [{"email": {"email": str(self.owner_email)}}]},
                        ],
                    },
                },
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services",
                headers={"Authorization": self._auth_header()},
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning("Failed to list services for {}: {} {}", tunnel_name, response.status_code, response.text)
                return None
            services = response.json().get("services", [])
            return {
                s["service_name"]: s["hostname"]
                for s in services
                if "service_name" in s and "hostname" in s
            }
        except (httpx.HTTPError, KeyError) as e:
            logger.warning("Failed to list services for {}: {}", tunnel_name, e)
            return None

    def add_service(self, agent_id: AgentId, service_name: str, service_url: str) -> bool:
        """Add a service to the agent's tunnel. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.post(
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services",
                headers={"Authorization": self._auth_header()},
                json={"service_name": service_name, "service_url": service_url},
                timeout=15.0,
            )
            if response.status_code not in (200, 201):
                logger.warning(
                    "Failed to add service {} to {}: {} {}", service_name, tunnel_name, response.status_code, response.text
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.warning("Failed to add service {} to {}: {}", service_name, tunnel_name, e)
            return False

    def remove_service(self, agent_id: AgentId, service_name: str) -> bool:
        """Remove a service from the agent's tunnel. Returns True on success."""
        tunnel_name = self.make_tunnel_name(agent_id)
        try:
            response = httpx.delete(
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services/{service_name}",
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
