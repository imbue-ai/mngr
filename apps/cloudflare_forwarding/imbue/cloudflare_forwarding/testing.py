"""Test utilities for cloudflare_forwarding."""

from typing import Any

from imbue.cloudflare_forwarding.app import ForwardingCtx


class FakeCloudflareOps:
    """In-memory fake implementing the CloudflareOps protocol for testing."""

    def __init__(self) -> None:
        self.tunnels: dict[str, dict[str, Any]] = {}
        self.tunnel_configs: dict[str, dict[str, Any]] = {}
        self.dns_records: list[dict[str, Any]] = []
        self._next_tunnel_id = 1
        self._next_record_id = 1

    def create_tunnel(self, name: str) -> dict[str, Any]:
        tunnel_id = f"tunnel-{self._next_tunnel_id}"
        self._next_tunnel_id += 1
        tunnel = {"id": tunnel_id, "name": name}
        self.tunnels[tunnel_id] = tunnel
        return tunnel

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        results = list(self.tunnels.values())
        if include_prefix:
            results = [t for t in results if t["name"].startswith(include_prefix)]
        return results

    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        for tunnel in self.tunnels.values():
            if tunnel["name"] == name:
                return tunnel
        return None

    def get_tunnel_token(self, tunnel_id: str) -> str:
        return f"token-for-{tunnel_id}"

    def delete_tunnel(self, tunnel_id: str) -> None:
        self.tunnels.pop(tunnel_id, None)
        self.tunnel_configs.pop(tunnel_id, None)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        return self.tunnel_configs.get(tunnel_id, {"config": {"ingress": [{"service": "http_status:404"}]}})

    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None:
        self.tunnel_configs[tunnel_id] = config

    def create_cname(self, name: str, target: str) -> dict[str, Any]:
        record_id = f"record-{self._next_record_id}"
        self._next_record_id += 1
        record = {"id": record_id, "name": name, "content": target, "type": "CNAME"}
        self.dns_records.append(record)
        return record

    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]:
        if name:
            return [r for r in self.dns_records if r["name"] == name]
        return list(self.dns_records)

    def delete_dns_record(self, record_id: str) -> None:
        self.dns_records = [r for r in self.dns_records if r["id"] != record_id]


class FakeForwardingCtx(ForwardingCtx):
    """ForwardingCtx backed by FakeCloudflareOps for testing."""

    fake: FakeCloudflareOps


def make_fake_forwarding_ctx(domain: str = "example.com") -> FakeForwardingCtx:
    """Create a FakeForwardingCtx for testing."""
    fake = FakeCloudflareOps()
    ctx = FakeForwardingCtx(ops=fake, domain=domain)
    ctx.fake = fake
    return ctx
