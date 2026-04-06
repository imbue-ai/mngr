"""Business logic for tunnel and service management."""

from typing import Any

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.cloudflare_forwarding.data_types import ServiceInfo
from imbue.cloudflare_forwarding.data_types import TunnelInfo
from imbue.cloudflare_forwarding.errors import InvalidTunnelComponentError
from imbue.cloudflare_forwarding.errors import ServiceNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelOwnershipError
from imbue.cloudflare_forwarding.interfaces import CloudflareClientInterface
from imbue.cloudflare_forwarding.primitives import AgentId
from imbue.cloudflare_forwarding.primitives import CloudflareDnsRecordId
from imbue.cloudflare_forwarding.primitives import CloudflareTunnelId
from imbue.cloudflare_forwarding.primitives import DomainName
from imbue.cloudflare_forwarding.primitives import ServiceName
from imbue.cloudflare_forwarding.primitives import ServiceUrl
from imbue.cloudflare_forwarding.primitives import TunnelName
from imbue.cloudflare_forwarding.primitives import Username
from imbue.imbue_common.frozen_model import FrozenModel


_TUNNEL_NAME_SEPARATOR = "--"


def make_tunnel_name(username: Username, agent_id: AgentId) -> TunnelName:
    """Generate a tunnel name from username and agent ID."""
    if _TUNNEL_NAME_SEPARATOR in username:
        raise InvalidTunnelComponentError("Username", username, _TUNNEL_NAME_SEPARATOR)
    if _TUNNEL_NAME_SEPARATOR in agent_id:
        raise InvalidTunnelComponentError("Agent ID", agent_id, _TUNNEL_NAME_SEPARATOR)
    return TunnelName(f"{username}{_TUNNEL_NAME_SEPARATOR}{agent_id}")


def make_hostname(service_name: ServiceName, agent_id: AgentId, username: Username, domain: DomainName) -> str:
    """Generate the public hostname for a service."""
    return f"{service_name}--{agent_id}--{username}.{domain}"


def _extract_agent_id_from_tunnel_name(tunnel_name: TunnelName, username: Username) -> AgentId:
    """Extract the agent_id portion from a tunnel name of the form '{username}--{agent_id}'."""
    prefix = f"{username}{_TUNNEL_NAME_SEPARATOR}"
    if not tunnel_name.startswith(prefix):
        raise TunnelOwnershipError(tunnel_name, username)
    return AgentId(tunnel_name[len(prefix) :])


def _get_ingress_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the ingress rules list from a tunnel configuration."""
    return config.get("config", {}).get("ingress", [])


def _non_catchall_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the rules that have a hostname (not the catch-all)."""
    return [r for r in rules if "hostname" in r]


def _build_config_with_ingress(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a tunnel configuration dict with the given ingress rules plus the catch-all."""
    all_rules = list(rules) + [{"service": "http_status:404"}]
    return {"config": {"ingress": all_rules}}


class ForwardingService(FrozenModel):
    """Manages tunnels and services via the Cloudflare API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: SkipValidation[CloudflareClientInterface] = Field(description="Cloudflare API client")
    domain: DomainName = Field(description="Base domain for service subdomains")

    def _verify_ownership(self, tunnel_name: TunnelName, username: Username) -> None:
        """Verify that a tunnel belongs to the given user."""
        expected_prefix = f"{username}{_TUNNEL_NAME_SEPARATOR}"
        if not tunnel_name.startswith(expected_prefix):
            raise TunnelOwnershipError(tunnel_name, username)

    def _get_tunnel_or_raise(self, tunnel_name: TunnelName) -> dict[str, Any]:
        """Look up a tunnel by name, raising TunnelNotFoundError if missing."""
        tunnel = self.client.get_tunnel_by_name(tunnel_name)
        if tunnel is None:
            raise TunnelNotFoundError(tunnel_name)
        return tunnel

    def create_tunnel(self, username: Username, agent_id: AgentId) -> TunnelInfo:
        """Create a tunnel (or reuse existing) and return its info with token."""
        tunnel_name = make_tunnel_name(username, agent_id)

        existing = self.client.get_tunnel_by_name(tunnel_name)
        if existing is not None:
            tunnel_id = CloudflareTunnelId(existing["id"])
            token = self.client.get_tunnel_token(tunnel_id)
            services = self._list_services_for_tunnel(tunnel_id, tunnel_name, username)
            return TunnelInfo(
                tunnel_name=tunnel_name,
                tunnel_id=tunnel_id,
                token=token,
                services=services,
            )

        result = self.client.create_tunnel(tunnel_name)
        tunnel_id = CloudflareTunnelId(result["id"])
        token = self.client.get_tunnel_token(tunnel_id)

        self.client.put_tunnel_configuration(tunnel_id, _build_config_with_ingress([]))

        return TunnelInfo(
            tunnel_name=tunnel_name,
            tunnel_id=tunnel_id,
            token=token,
            services=(),
        )

    def list_tunnels(self, username: Username) -> list[TunnelInfo]:
        """List all tunnels belonging to a user."""
        prefix = f"{username}{_TUNNEL_NAME_SEPARATOR}"
        tunnels = self.client.list_tunnels(name_prefix=prefix)

        result: list[TunnelInfo] = []
        for tunnel in tunnels:
            name = tunnel["name"]
            if not name.startswith(prefix):
                continue
            tunnel_name = TunnelName(name)
            tunnel_id = CloudflareTunnelId(tunnel["id"])
            services = self._list_services_for_tunnel(tunnel_id, tunnel_name, username)
            result.append(
                TunnelInfo(
                    tunnel_name=tunnel_name,
                    tunnel_id=tunnel_id,
                    services=services,
                )
            )
        return result

    def delete_tunnel(self, tunnel_name: TunnelName, username: Username) -> None:
        """Delete a tunnel, cascading to remove all DNS records and ingress rules."""
        self._verify_ownership(tunnel_name, username)
        tunnel = self._get_tunnel_or_raise(tunnel_name)
        tunnel_id = CloudflareTunnelId(tunnel["id"])

        config = self.client.get_tunnel_configuration(tunnel_id)
        rules = _non_catchall_rules(_get_ingress_rules(config))

        for rule in rules:
            hostname = rule.get("hostname", "")
            if hostname:
                self._delete_dns_record_by_name(hostname)

        self.client.put_tunnel_configuration(tunnel_id, _build_config_with_ingress([]))
        self.client.delete_tunnel(tunnel_id)

    def add_service(
        self,
        tunnel_name: TunnelName,
        username: Username,
        service_name: ServiceName,
        service_url: ServiceUrl,
    ) -> ServiceInfo:
        """Add a service (ingress rule + DNS record) to a tunnel."""
        self._verify_ownership(tunnel_name, username)
        tunnel = self._get_tunnel_or_raise(tunnel_name)
        tunnel_id = CloudflareTunnelId(tunnel["id"])
        agent_id = _extract_agent_id_from_tunnel_name(tunnel_name, username)

        hostname = make_hostname(service_name, agent_id, username, self.domain)

        self.client.create_cname_record(
            name=hostname,
            target=f"{tunnel_id}.cfargotunnel.com",
        )

        config = self.client.get_tunnel_configuration(tunnel_id)
        rules = _non_catchall_rules(_get_ingress_rules(config))
        rules.append({"hostname": hostname, "service": str(service_url)})
        self.client.put_tunnel_configuration(tunnel_id, _build_config_with_ingress(rules))

        return ServiceInfo(
            service_name=service_name,
            hostname=hostname,
            service_url=service_url,
        )

    def remove_service(
        self,
        tunnel_name: TunnelName,
        username: Username,
        service_name: ServiceName,
    ) -> None:
        """Remove a service (ingress rule + DNS record) from a tunnel."""
        self._verify_ownership(tunnel_name, username)
        tunnel = self._get_tunnel_or_raise(tunnel_name)
        tunnel_id = CloudflareTunnelId(tunnel["id"])
        agent_id = _extract_agent_id_from_tunnel_name(tunnel_name, username)

        hostname = make_hostname(service_name, agent_id, username, self.domain)

        config = self.client.get_tunnel_configuration(tunnel_id)
        rules = _non_catchall_rules(_get_ingress_rules(config))
        new_rules = [r for r in rules if r.get("hostname") != hostname]
        if len(new_rules) == len(rules):
            raise ServiceNotFoundError(service_name, tunnel_name)

        self.client.put_tunnel_configuration(tunnel_id, _build_config_with_ingress(new_rules))
        self._delete_dns_record_by_name(hostname)

    def _list_services_for_tunnel(
        self,
        tunnel_id: CloudflareTunnelId,
        tunnel_name: TunnelName,
        username: Username,
    ) -> tuple[ServiceInfo, ...]:
        """List the configured services for a tunnel by reading its ingress config."""
        agent_id = _extract_agent_id_from_tunnel_name(tunnel_name, username)
        config = self.client.get_tunnel_configuration(tunnel_id)
        rules = _non_catchall_rules(_get_ingress_rules(config))

        services: list[ServiceInfo] = []
        for rule in rules:
            hostname = rule.get("hostname", "")
            service_url = rule.get("service", "")
            service_name = _extract_service_name_from_hostname(hostname, agent_id, username, self.domain)
            if service_name is not None:
                services.append(
                    ServiceInfo(
                        service_name=ServiceName(service_name),
                        hostname=hostname,
                        service_url=ServiceUrl(service_url),
                    )
                )
        return tuple(services)

    def _delete_dns_record_by_name(self, hostname: str) -> None:
        """Find and delete a CNAME DNS record by its hostname."""
        records = self.client.list_dns_records(name=hostname)
        for record in records:
            self.client.delete_dns_record(CloudflareDnsRecordId(record["id"]))


def _extract_service_name_from_hostname(
    hostname: str,
    agent_id: AgentId,
    username: Username,
    domain: DomainName,
) -> str | None:
    """Extract the service name from a hostname matching the pattern '{service}--{agent_id}--{username}.{domain}'.

    Returns None if the hostname doesn't match the expected pattern.
    """
    expected_suffix = f"--{agent_id}--{username}.{domain}"
    if not hostname.endswith(expected_suffix):
        return None
    return hostname[: -len(expected_suffix)]
