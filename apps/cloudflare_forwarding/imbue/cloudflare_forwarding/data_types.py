from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.cloudflare_forwarding.primitives import AgentId
from imbue.cloudflare_forwarding.primitives import CloudflareTunnelId
from imbue.cloudflare_forwarding.primitives import ServiceName
from imbue.cloudflare_forwarding.primitives import ServiceUrl
from imbue.cloudflare_forwarding.primitives import TunnelName


class CreateTunnelRequest(FrozenModel):
    """Request body for creating a tunnel."""

    agent_id: AgentId = Field(description="The mngr agent ID for this tunnel")


class AddServiceRequest(FrozenModel):
    """Request body for adding a service to a tunnel."""

    service_name: ServiceName = Field(description="User-chosen name for the service")
    service_url: ServiceUrl = Field(description="Local service URL (e.g. http://localhost:8080)")


class ServiceInfo(FrozenModel):
    """Information about a configured service on a tunnel."""

    service_name: ServiceName = Field(description="User-chosen service name")
    hostname: str = Field(description="Public hostname for this service")
    service_url: ServiceUrl = Field(description="Backend service URL")


class TunnelInfo(FrozenModel):
    """Information about a tunnel and its configured services."""

    tunnel_name: TunnelName = Field(description="Tunnel name ({username}--{agent_id})")
    tunnel_id: CloudflareTunnelId = Field(description="Cloudflare tunnel UUID")
    token: str | None = Field(default=None, description="Tunnel token for cloudflared (only on create)")
    services: tuple[ServiceInfo, ...] = Field(default=(), description="Configured services on this tunnel")
