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

    Supports two auth modes:
    - Basic auth (admin credentials) via username/secret fields
    - Bearer auth (SuperTokens JWT) via supertokens_token/supertokens_user_id_prefix fields

    When SuperTokens fields are set, they take priority over Basic auth.
    """

    forwarding_url: CloudflareForwardingUrl = Field(description="Base URL of the cloudflare_forwarding API")
    username: CloudflareUsername | None = Field(default=None, description="Username for admin Basic auth")
    secret: CloudflareSecret | None = Field(default=None, description="Secret for admin Basic auth")
    owner_email: OwnerEmail | None = Field(default=None, description="Email for default Google OAuth policy")
    supertokens_token: str | None = Field(default=None, description="SuperTokens JWT access token for Bearer auth")
    supertokens_user_id_prefix: str | None = Field(
        default=None, description="First 16 hex chars of SuperTokens user ID for tunnel naming",
    )
    supertokens_email: str | None = Field(default=None, description="Email from SuperTokens session for access policies")

    def _auth_header(self) -> str:
        """Build the auth header value. Prefers SuperTokens Bearer token over Basic auth."""
        if self.supertokens_token:
            return f"Bearer {self.supertokens_token}"
        if self.username and self.secret:
            credentials = f"{self.username}:{self.secret}"
            return "Basic " + base64.b64encode(credentials.encode()).decode()
        raise ValueError("No auth credentials configured for cloudflare_forwarding")

    def effective_owner_email(self) -> str | None:
        """Return the email to use for default access policies."""
        return self.supertokens_email or (str(self.owner_email) if self.owner_email else None)

    def _effective_username(self) -> str:
        """Return the username for tunnel naming."""
        if self.supertokens_user_id_prefix:
            return self.supertokens_user_id_prefix
        if self.username:
            return str(self.username)
        raise ValueError("No username configured for tunnel naming")

    @staticmethod
    def _truncate_agent_id(agent_id: AgentId) -> str:
        """Truncate an agent ID to a 16-char prefix for use in hostnames."""
        return str(agent_id).removeprefix("agent-")[:16]

    def make_tunnel_name(self, agent_id: AgentId) -> str:
        """Build the tunnel name for an agent."""
        return f"{self._effective_username()}--{self._truncate_agent_id(agent_id)}"

    def create_tunnel(self, agent_id: AgentId) -> tuple[str | None, str]:
        """Create a Cloudflare tunnel for the agent and return (token, message).

        Sets a default Google OAuth policy for the owner's email.
        Returns (token, success_message) on success, or (None, error_message) on failure.
        """
        tunnel_name = self.make_tunnel_name(agent_id)
        owner_email = self.effective_owner_email()
        default_policy: dict[str, object] | None = None
        if owner_email:
            default_policy = {
                "rules": [
                    {"action": "allow", "include": [{"email": {"email": owner_email}}]},
                ],
            }
        request_body: dict[str, object] = {"agent_id": str(agent_id)}
        if default_policy:
            request_body["default_auth_policy"] = default_policy
        try:
            response = httpx.post(
                f"{self.forwarding_url}/tunnels",
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services",
                headers={"Authorization": self._auth_header()},
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to list services for {}: {} {}", tunnel_name, response.status_code, response.text
                )
                return None
            data = response.json()
            # The forwarding API may return {"services": [...]} or a bare list
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services",
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/auth",
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services/{service_name}/auth",
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
                f"{self.forwarding_url}/tunnels/{tunnel_name}/services/{service_name}/auth",
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
