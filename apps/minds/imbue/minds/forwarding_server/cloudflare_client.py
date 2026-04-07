"""Client for the Cloudflare forwarding API.

Encapsulates authentication, URL construction, and HTTP calls to the
Modal-hosted cloudflare_forwarding service. Created once in runner.py
and passed as a dependency to AgentCreator and the forwarding server app.
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

    def create_tunnel(self, agent_id: AgentId) -> str | None:
        """Create a Cloudflare tunnel for the agent and return the tunnel token.

        Sets a default Google OAuth policy for the owner's email.
        Returns the tunnel token string, or None if creation fails.
        """
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
                logger.warning("Failed to create tunnel: {} {}", response.status_code, response.text)
                return None

            tunnel_info = response.json()
            token = tunnel_info.get("token")
            tunnel_name = tunnel_info.get("tunnel_name", "")
            logger.info("Cloudflare tunnel created: {}", tunnel_name)
            return token

        except httpx.HTTPError as e:
            logger.warning("Failed to create tunnel: {}", e)
            return None

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
                return None
            services = response.json().get("services", [])
            return {
                s["service_name"]: s["hostname"]
                for s in services
                if "service_name" in s and "hostname" in s
            }
        except (httpx.HTTPError, KeyError):
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
            return response.status_code in (200, 201)
        except httpx.HTTPError:
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
            return response.status_code in (200, 204)
        except httpx.HTTPError:
            return False
