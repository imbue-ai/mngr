"""Test utilities for cloudflare_forwarding."""

import base64
import json
from typing import Any

from imbue.cloudflare_forwarding.app import ForwardingCtx


class FakeCloudflareOps:
    """In-memory fake implementing the CloudflareOps protocol for testing."""

    def __init__(self) -> None:
        self.tunnels: dict[str, dict[str, Any]] = {}
        self.tunnel_configs: dict[str, dict[str, Any]] = {}
        self.dns_records: list[dict[str, Any]] = []
        self.access_apps: dict[str, dict[str, Any]] = {}
        self.access_policies: dict[str, list[dict[str, Any]]] = {}
        self.kv_store: dict[str, str] = {}
        self._next_tunnel_id = 1
        self._next_record_id = 1
        self._next_access_app_id = 1
        self._next_policy_id = 1

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

    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None:
        return self.tunnels.get(tunnel_id)

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

    def create_access_app(self, hostname: str, app_name: str) -> dict[str, Any]:
        app_id = f"access-app-{self._next_access_app_id}"
        self._next_access_app_id += 1
        access_app = {"id": app_id, "domain": hostname, "name": app_name}
        self.access_apps[app_id] = access_app
        self.access_policies[app_id] = []
        return access_app

    def delete_access_app(self, app_id: str) -> None:
        self.access_apps.pop(app_id, None)
        self.access_policies.pop(app_id, None)

    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None:
        for access_app in self.access_apps.values():
            if access_app["domain"] == hostname:
                return access_app
        return None

    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]:
        return list(self.access_policies.get(app_id, []))

    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        policy_id = f"policy-{self._next_policy_id}"
        self._next_policy_id += 1
        stored = {**policy, "id": policy_id}
        if app_id not in self.access_policies:
            self.access_policies[app_id] = []
        self.access_policies[app_id].append(stored)
        return stored

    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        policies = self.access_policies.get(app_id, [])
        for i, p in enumerate(policies):
            if p["id"] == policy_id:
                policies[i] = {**policy, "id": policy_id}
                return policies[i]
        return {**policy, "id": policy_id}

    def delete_access_policy(self, app_id: str, policy_id: str) -> None:
        if app_id in self.access_policies:
            self.access_policies[app_id] = [p for p in self.access_policies[app_id] if p["id"] != policy_id]

    def kv_get(self, key: str) -> str | None:
        return self.kv_store.get(key)

    def kv_put(self, key: str, value: str) -> None:
        self.kv_store[key] = value

    def kv_delete(self, key: str) -> None:
        self.kv_store.pop(key, None)


class FakeForwardingCtx(ForwardingCtx):
    """ForwardingCtx backed by FakeCloudflareOps for testing."""

    fake: FakeCloudflareOps


def make_fake_forwarding_ctx(domain: str = "example.com") -> FakeForwardingCtx:
    """Create a FakeForwardingCtx for testing."""
    fake = FakeCloudflareOps()
    ctx = FakeForwardingCtx(ops=fake, domain=domain)
    ctx.fake = fake
    return ctx


def make_fake_tunnel_token(tunnel_id: str) -> str:
    """Create a fake tunnel token (base64-encoded JSON) for testing."""
    token_data = json.dumps({"a": "test-account", "t": tunnel_id, "s": "test-secret"})
    return base64.b64encode(token_data.encode()).decode()
