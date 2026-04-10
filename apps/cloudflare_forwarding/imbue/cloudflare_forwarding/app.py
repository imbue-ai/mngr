"""Cloudflare tunnel management service, deployed as a Modal function.

This file is entirely self-contained -- it has NO imports from the monorepo.
Only stdlib and 3rd-party packages (installed in the Modal image) are used.
This keeps deployment simple: `modal deploy app.py` ships just this file.
"""

import base64
import binascii
import contextlib
import functools
import json
import logging
import os
import secrets as secrets_module
from collections.abc import Iterator
from typing import Any
from typing import NoReturn
from typing import Protocol

import httpx
import modal
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger(__name__)

_CF_BASE_URL = "https://api.cloudflare.com/client/v4"
TUNNEL_NAME_SEP = "--"
KV_NAMESPACE_TITLE = "cloudflare-forwarding-defaults"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CloudflareApiError(RuntimeError):
    """Raised when the Cloudflare API returns an error response."""

    def __init__(self, status_code: int, errors: list[dict[str, object]]) -> None:
        self.status_code = status_code
        self.cf_errors = errors
        messages = "; ".join(str(e.get("message", e)) for e in errors)
        super().__init__(f"Cloudflare API error ({status_code}): {messages}")


class TunnelNotFoundError(KeyError):
    def __init__(self, tunnel_name: str) -> None:
        self.tunnel_name = tunnel_name
        super().__init__(f"Tunnel not found: {tunnel_name}")


class TunnelOwnershipError(PermissionError):
    def __init__(self, tunnel_name: str, username: str) -> None:
        self.tunnel_name = tunnel_name
        self.username = username
        super().__init__(f"User '{username}' does not own tunnel '{tunnel_name}'")


class ServiceNotFoundError(KeyError):
    def __init__(self, service_name: str, tunnel_name: str) -> None:
        self.service_name = service_name
        self.tunnel_name = tunnel_name
        super().__init__(f"Service '{service_name}' not found on tunnel '{tunnel_name}'")


class InvalidTunnelComponentError(ValueError):
    def __init__(self, component_name: str, value: str, forbidden: str) -> None:
        self.component_name = component_name
        self.value = value
        self.forbidden = forbidden
        super().__init__(
            f"{component_name} '{value}' must not contain '{forbidden}' (used as the tunnel name separator)"
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AuthPolicy(BaseModel):
    rules: list[dict[str, Any]] = Field(description="Cloudflare Access-style policy rules")


class CreateTunnelRequest(BaseModel):
    agent_id: str = Field(description="The mngr agent ID for this tunnel")
    default_auth_policy: AuthPolicy | None = Field(
        default=None, description="Optional default auth policy for new services"
    )


class AddServiceRequest(BaseModel):
    service_name: str = Field(description="User-chosen name for the service")
    service_url: str = Field(description="Local service URL (e.g. http://localhost:8080)")


class ServiceInfo(BaseModel):
    service_name: str = Field(description="User-chosen service name")
    hostname: str = Field(description="Public hostname for this service")
    service_url: str = Field(description="Backend service URL")


class TunnelInfo(BaseModel):
    tunnel_name: str = Field(description="Tunnel name")
    tunnel_id: str = Field(description="Cloudflare tunnel UUID")
    token: str | None = Field(default=None, description="Tunnel token for cloudflared (only on create)")
    services: list[ServiceInfo] = Field(default_factory=list, description="Configured services")


class CreateServiceTokenRequest(BaseModel):
    name: str = Field(description="Human-readable name for the service token")


class ServiceTokenInfo(BaseModel):
    token_id: str = Field(description="Cloudflare service token ID")
    client_id: str = Field(description="Client ID for CF-Access-Client-Id header")
    client_secret: str | None = Field(default=None, description="Client secret (only returned on creation)")
    name: str = Field(description="Token name")


class AdminAuth(BaseModel):
    username: str


class AgentAuth(BaseModel):
    tunnel_id: str
    tunnel_name: str


AuthResult = AdminAuth | AgentAuth


# ---------------------------------------------------------------------------
# Cloudflare API client (pure functions)
# ---------------------------------------------------------------------------


def cf_check(response: httpx.Response) -> dict[str, Any]:
    data: dict[str, Any] = response.json()
    if not data.get("success", False):
        raise CloudflareApiError(
            status_code=response.status_code,
            errors=data.get("errors", [{"message": "Unknown error"}]),
        )
    return data


def cf_list_all_pages(client: httpx.Client, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    page = 1
    while True:
        paginated = {**params, "page": str(page), "per_page": "100"}
        response = client.get(url, params=paginated)
        data = cf_check(response)
        results: list[dict[str, Any]] = data["result"]
        all_results.extend(results)
        total_count = data.get("result_info", {}).get("total_count", len(results))
        if len(all_results) >= total_count:
            break
        page += 1
    return all_results


# --- Tunnel operations ---


def cf_create_tunnel(client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/cfd_tunnel", json={"name": name, "config_src": "cloudflare"})
    return cf_check(response)["result"]


def cf_list_tunnels(client: httpx.Client, account_id: str, include_prefix: str = "") -> list[dict[str, Any]]:
    params: dict[str, str] = {"is_deleted": "false"}
    if include_prefix:
        params["include_prefix"] = include_prefix
    return cf_list_all_pages(client, f"/accounts/{account_id}/cfd_tunnel", params)


def cf_get_tunnel_by_name(client: httpx.Client, account_id: str, name: str) -> dict[str, Any] | None:
    params: dict[str, str] = {"is_deleted": "false", "name": name}
    response = client.get(f"/accounts/{account_id}/cfd_tunnel", params=params)
    for tunnel in cf_check(response)["result"]:
        if tunnel["name"] == name:
            return tunnel
    return None


def cf_get_tunnel_by_id(client: httpx.Client, account_id: str, tunnel_id: str) -> dict[str, Any] | None:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}")
    try:
        data = cf_check(response)
        return data["result"]
    except CloudflareApiError as exc:
        if exc.status_code == 404:
            return None
        raise


def cf_get_tunnel_token(client: httpx.Client, account_id: str, tunnel_id: str) -> str:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
    return cf_check(response)["result"]


def cf_delete_tunnel(client: httpx.Client, account_id: str, tunnel_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"))


def cf_get_tunnel_config(client: httpx.Client, account_id: str, tunnel_id: str) -> dict[str, Any]:
    response = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    return cf_check(response)["result"]


def cf_put_tunnel_config(client: httpx.Client, account_id: str, tunnel_id: str, config: dict[str, Any]) -> None:
    cf_check(client.put(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", json=config))


# --- DNS operations ---


def cf_create_cname(client: httpx.Client, zone_id: str, name: str, target: str) -> dict[str, Any]:
    response = client.post(
        f"/zones/{zone_id}/dns_records",
        json={"type": "CNAME", "name": name, "content": target, "proxied": True, "ttl": 1},
    )
    return cf_check(response)["result"]


def cf_list_dns_records(client: httpx.Client, zone_id: str, name: str = "") -> list[dict[str, Any]]:
    params: dict[str, str] = {"type": "CNAME"}
    if name:
        params["name"] = name
    return cf_list_all_pages(client, f"/zones/{zone_id}/dns_records", params)


def cf_delete_dns_record(client: httpx.Client, zone_id: str, record_id: str) -> None:
    cf_check(client.delete(f"/zones/{zone_id}/dns_records/{record_id}"))


# --- Access operations ---


def cf_create_access_app(client: httpx.Client, account_id: str, hostname: str, app_name: str) -> dict[str, Any]:
    response = client.post(
        f"/accounts/{account_id}/access/apps",
        json={
            "name": app_name,
            "domain": hostname,
            "type": "self_hosted",
            "session_duration": "24h",
        },
    )
    return cf_check(response)["result"]


def cf_delete_access_app(client: httpx.Client, account_id: str, app_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/apps/{app_id}"))


def cf_get_access_app_by_domain(client: httpx.Client, account_id: str, hostname: str) -> dict[str, Any] | None:
    response = client.get(f"/accounts/{account_id}/access/apps")
    data = cf_check(response)
    for app_item in data["result"]:
        if app_item.get("domain") == hostname:
            return app_item
    return None


def cf_list_access_policies(client: httpx.Client, account_id: str, app_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/access/apps/{app_id}/policies")
    return cf_check(response)["result"]


def cf_create_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/access/apps/{app_id}/policies", json=policy)
    return cf_check(response)["result"]


def cf_update_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.put(f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}", json=policy)
    return cf_check(response)["result"]


def cf_delete_access_policy(client: httpx.Client, account_id: str, app_id: str, policy_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}"))


# --- Service token operations ---


def cf_create_service_token(
    client: httpx.Client, account_id: str, name: str, duration: str = "8760h"
) -> dict[str, Any]:
    response = client.post(
        f"/accounts/{account_id}/access/service_tokens",
        json={"name": name, "duration": duration},
    )
    return cf_check(response)["result"]


def cf_list_service_tokens(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/access/service_tokens")
    return cf_check(response)["result"]


def cf_delete_service_token(client: httpx.Client, account_id: str, token_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/service_tokens/{token_id}"))


# --- Workers KV operations ---


def cf_kv_list_namespaces(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/storage/kv/namespaces")
    return cf_check(response)["result"]


def cf_kv_create_namespace(client: httpx.Client, account_id: str, title: str) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/storage/kv/namespaces", json={"title": title})
    return cf_check(response)["result"]


def cf_kv_get(client: httpx.Client, account_id: str, namespace_id: str, key: str) -> str | None:
    response = client.get(f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def cf_kv_put(client: httpx.Client, account_id: str, namespace_id: str, key: str, value: str) -> None:
    response = client.put(
        f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}",
        content=value,
        headers={"Content-Type": "text/plain"},
    )
    cf_check(response)


def cf_kv_delete(client: httpx.Client, account_id: str, namespace_id: str, key: str) -> None:
    response = client.delete(f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}")
    cf_check(response)


def cf_kv_ensure_namespace(client: httpx.Client, account_id: str, title: str) -> str:
    """Find or create a KV namespace by title. Returns the namespace ID."""
    namespaces = cf_kv_list_namespaces(client, account_id)
    for ns in namespaces:
        if ns["title"] == title:
            return ns["id"]
    result = cf_kv_create_namespace(client, account_id, title)
    return result["id"]


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def make_tunnel_name(username: str, agent_id: str) -> str:
    if TUNNEL_NAME_SEP in username:
        raise InvalidTunnelComponentError("Username", username, TUNNEL_NAME_SEP)
    if TUNNEL_NAME_SEP in agent_id:
        raise InvalidTunnelComponentError("Agent ID", agent_id, TUNNEL_NAME_SEP)
    return f"{username}{TUNNEL_NAME_SEP}{agent_id}"


def make_hostname(service_name: str, agent_id: str, username: str, domain: str) -> str:
    return f"{service_name}--{agent_id}--{username}.{domain}"


def extract_agent_id(tunnel_name: str, username: str) -> str:
    prefix = f"{username}{TUNNEL_NAME_SEP}"
    if not tunnel_name.startswith(prefix):
        raise TunnelOwnershipError(tunnel_name, username)
    return tunnel_name[len(prefix) :]


def extract_service_name(hostname: str, agent_id: str, username: str, domain: str) -> str | None:
    expected_suffix = f"--{agent_id}--{username}.{domain}"
    if not hostname.endswith(expected_suffix):
        return None
    return hostname[: -len(expected_suffix)]


def extract_username_from_tunnel_name(tunnel_name: str) -> str:
    """Extract the username portion from a tunnel name."""
    parts = tunnel_name.split(TUNNEL_NAME_SEP, 1)
    return parts[0]


# ---------------------------------------------------------------------------
# Ingress config helpers
# ---------------------------------------------------------------------------


def non_catchall_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rules if "hostname" in r]


def wrap_ingress(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {"config": {"ingress": list(rules) + [{"service": "http_status:404"}]}}


# ---------------------------------------------------------------------------
# Auth policy helpers
# ---------------------------------------------------------------------------


def policy_to_cf_rules(policy: AuthPolicy) -> list[dict[str, Any]]:
    """Convert our AuthPolicy format to Cloudflare Access policy create/update format."""
    cf_policies = []
    for rule in policy.rules:
        cf_policies.append(
            {
                "name": "Policy rule",
                "decision": rule.get("action", "allow"),
                "include": rule.get("include", []),
                "precedence": len(cf_policies) + 1,
            }
        )
    return cf_policies


def cf_policies_to_auth_policy(cf_policies: list[dict[str, Any]]) -> AuthPolicy:
    """Convert Cloudflare Access policies back to our AuthPolicy format."""
    rules = []
    for p in cf_policies:
        rules.append(
            {
                "action": p.get("decision", "allow"),
                "include": p.get("include", []),
            }
        )
    return AuthPolicy(rules=rules)


# ---------------------------------------------------------------------------
# Cloudflare operations protocol
# ---------------------------------------------------------------------------


class CloudflareOps(Protocol):
    """Abstraction over Cloudflare API calls used by ForwardingCtx."""

    def create_tunnel(self, name: str) -> dict[str, Any]: ...
    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]: ...
    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None: ...
    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None: ...
    def get_tunnel_token(self, tunnel_id: str) -> str: ...
    def delete_tunnel(self, tunnel_id: str) -> None: ...
    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]: ...
    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None: ...
    def create_cname(self, name: str, target: str) -> dict[str, Any]: ...
    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]: ...
    def delete_dns_record(self, record_id: str) -> None: ...
    def create_access_app(self, hostname: str, app_name: str) -> dict[str, Any]: ...
    def delete_access_app(self, app_id: str) -> None: ...
    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None: ...
    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]: ...
    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]: ...
    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]: ...
    def delete_access_policy(self, app_id: str, policy_id: str) -> None: ...
    def kv_get(self, key: str) -> str | None: ...
    def kv_put(self, key: str, value: str) -> None: ...
    def kv_delete(self, key: str) -> None: ...
    def create_service_token(self, name: str) -> dict[str, Any]: ...
    def list_service_tokens(self) -> list[dict[str, Any]]: ...
    def delete_service_token(self, token_id: str) -> None: ...


class HttpCloudflareOps:
    """CloudflareOps implementation backed by real Cloudflare HTTP API calls."""

    def __init__(self, api_token: str, account_id: str, zone_id: str) -> None:
        self.client = httpx.Client(
            base_url=_CF_BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
        )
        self.account_id = account_id
        self.zone_id = zone_id
        self._kv_namespace_id: str | None = None

    def _ensure_kv_namespace(self) -> str:
        if self._kv_namespace_id is None:
            self._kv_namespace_id = cf_kv_ensure_namespace(self.client, self.account_id, KV_NAMESPACE_TITLE)
        return self._kv_namespace_id

    def create_tunnel(self, name: str) -> dict[str, Any]:
        return cf_create_tunnel(self.client, self.account_id, name)

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        return cf_list_tunnels(self.client, self.account_id, include_prefix=include_prefix)

    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        return cf_get_tunnel_by_name(self.client, self.account_id, name)

    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None:
        return cf_get_tunnel_by_id(self.client, self.account_id, tunnel_id)

    def get_tunnel_token(self, tunnel_id: str) -> str:
        return cf_get_tunnel_token(self.client, self.account_id, tunnel_id)

    def delete_tunnel(self, tunnel_id: str) -> None:
        cf_delete_tunnel(self.client, self.account_id, tunnel_id)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        return cf_get_tunnel_config(self.client, self.account_id, tunnel_id)

    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None:
        cf_put_tunnel_config(self.client, self.account_id, tunnel_id, config)

    def create_cname(self, name: str, target: str) -> dict[str, Any]:
        return cf_create_cname(self.client, self.zone_id, name, target)

    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]:
        return cf_list_dns_records(self.client, self.zone_id, name=name)

    def delete_dns_record(self, record_id: str) -> None:
        cf_delete_dns_record(self.client, self.zone_id, record_id)

    def create_access_app(self, hostname: str, app_name: str) -> dict[str, Any]:
        return cf_create_access_app(self.client, self.account_id, hostname, app_name)

    def delete_access_app(self, app_id: str) -> None:
        cf_delete_access_app(self.client, self.account_id, app_id)

    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None:
        return cf_get_access_app_by_domain(self.client, self.account_id, hostname)

    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]:
        return cf_list_access_policies(self.client, self.account_id, app_id)

    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        return cf_create_access_policy(self.client, self.account_id, app_id, policy)

    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        return cf_update_access_policy(self.client, self.account_id, app_id, policy_id, policy)

    def delete_access_policy(self, app_id: str, policy_id: str) -> None:
        cf_delete_access_policy(self.client, self.account_id, app_id, policy_id)

    def kv_get(self, key: str) -> str | None:
        ns_id = self._ensure_kv_namespace()
        return cf_kv_get(self.client, self.account_id, ns_id, key)

    def kv_put(self, key: str, value: str) -> None:
        ns_id = self._ensure_kv_namespace()
        cf_kv_put(self.client, self.account_id, ns_id, key, value)

    def kv_delete(self, key: str) -> None:
        ns_id = self._ensure_kv_namespace()
        cf_kv_delete(self.client, self.account_id, ns_id, key)

    def create_service_token(self, name: str) -> dict[str, Any]:
        return cf_create_service_token(self.client, self.account_id, name)

    def list_service_tokens(self) -> list[dict[str, Any]]:
        return cf_list_service_tokens(self.client, self.account_id)

    def delete_service_token(self, token_id: str) -> None:
        cf_delete_service_token(self.client, self.account_id, token_id)


# ---------------------------------------------------------------------------
# Forwarding service (business logic)
# ---------------------------------------------------------------------------


class ForwardingCtx:
    """Holds the Cloudflare ops abstraction and domain config. Created once per container."""

    def __init__(self, ops: CloudflareOps, domain: str) -> None:
        self.ops = ops
        self.domain = domain

    def verify_ownership(self, tunnel_name: str, username: str) -> None:
        if not tunnel_name.startswith(f"{username}{TUNNEL_NAME_SEP}"):
            raise TunnelOwnershipError(tunnel_name, username)

    def get_tunnel_or_raise(self, tunnel_name: str) -> dict[str, Any]:
        tunnel = self.ops.get_tunnel_by_name(tunnel_name)
        if tunnel is None:
            raise TunnelNotFoundError(tunnel_name)
        return tunnel

    def resolve_tunnel_name_by_id(self, tunnel_id: str) -> str:
        """Look up tunnel name from tunnel ID."""
        tunnel = self.ops.get_tunnel_by_id(tunnel_id)
        if tunnel is None:
            raise TunnelNotFoundError(tunnel_id)
        return tunnel["name"]

    def create_tunnel(self, username: str, agent_id: str, default_auth_policy: AuthPolicy | None = None) -> TunnelInfo:
        name = make_tunnel_name(username, agent_id)
        existing = self.ops.get_tunnel_by_name(name)
        if existing is not None:
            tid = existing["id"]
            token = self.ops.get_tunnel_token(tid)
            services = self._list_services(tid, name, username)
            return TunnelInfo(tunnel_name=name, tunnel_id=tid, token=token, services=services)

        result = self.ops.create_tunnel(name)
        tid = result["id"]
        token = self.ops.get_tunnel_token(tid)
        self.ops.put_tunnel_config(tid, wrap_ingress([]))

        if default_auth_policy is not None:
            self.ops.kv_put(name, default_auth_policy.model_dump_json())

        return TunnelInfo(tunnel_name=name, tunnel_id=tid, token=token, services=[])

    def list_tunnels(self, username: str) -> list[TunnelInfo]:
        prefix = f"{username}{TUNNEL_NAME_SEP}"
        tunnels = self.ops.list_tunnels(include_prefix=prefix)
        result: list[TunnelInfo] = []
        for t in tunnels:
            name = t["name"]
            if not name.startswith(prefix):
                continue
            tid = t["id"]
            services = self._list_services(tid, name, username)
            result.append(TunnelInfo(tunnel_name=name, tunnel_id=tid, services=services))
        return result

    def delete_tunnel(self, tunnel_name: str, username: str) -> None:
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        config = self.ops.get_tunnel_config(tid)
        for rule in non_catchall_rules(config.get("config", {}).get("ingress", [])):
            hostname = rule.get("hostname", "")
            if hostname:
                self._delete_access_app_for_hostname(hostname)
                self._delete_dns_by_name(hostname)
        self.ops.put_tunnel_config(tid, wrap_ingress([]))
        self.ops.delete_tunnel(tid)
        self._kv_delete_safe(tunnel_name)

    def add_service(self, tunnel_name: str, username: str, service_name: str, service_url: str) -> ServiceInfo:
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        self.ops.create_cname(hostname, f"{tid}.cfargotunnel.com")
        config = self.ops.get_tunnel_config(tid)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        rules.append({"hostname": hostname, "service": service_url})
        self.ops.put_tunnel_config(tid, wrap_ingress(rules))

        self._apply_default_access_policy(tunnel_name, hostname)

        return ServiceInfo(service_name=service_name, hostname=hostname, service_url=service_url)

    def remove_service(self, tunnel_name: str, username: str, service_name: str) -> None:
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        config = self.ops.get_tunnel_config(tid)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        new_rules = [r for r in rules if r.get("hostname") != hostname]
        if len(new_rules) == len(rules):
            raise ServiceNotFoundError(service_name, tunnel_name)
        self.ops.put_tunnel_config(tid, wrap_ingress(new_rules))
        self._delete_access_app_for_hostname(hostname)
        self._delete_dns_by_name(hostname)

    def get_tunnel_auth(self, tunnel_name: str) -> AuthPolicy | None:
        """Get the default auth policy for a tunnel from KV."""
        raw = self.ops.kv_get(tunnel_name)
        if raw is None:
            return None
        return AuthPolicy.model_validate_json(raw)

    def set_tunnel_auth(self, tunnel_name: str, policy: AuthPolicy) -> None:
        """Set the default auth policy for a tunnel in KV."""
        self.ops.kv_put(tunnel_name, policy.model_dump_json())

    def get_service_auth(self, tunnel_name: str, username: str, service_name: str) -> AuthPolicy | None:
        """Get the auth policy for a specific service from its Access Application."""
        agent_id = extract_agent_id(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            return None
        policies = self.ops.list_access_policies(access_app["id"])
        return cf_policies_to_auth_policy(policies)

    def set_service_auth(self, tunnel_name: str, username: str, service_name: str, policy: AuthPolicy) -> None:
        """Set the auth policy for a specific service on its Access Application."""
        agent_id = extract_agent_id(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            access_app = self.ops.create_access_app(hostname, f"cf-fwd-{service_name}")

        existing_policies = self.ops.list_access_policies(access_app["id"])
        for ep in existing_policies:
            self.ops.delete_access_policy(access_app["id"], ep["id"])

        for cf_policy in policy_to_cf_rules(policy):
            self.ops.create_access_policy(access_app["id"], cf_policy)

    def list_services(self, tunnel_name: str, username: str) -> list[ServiceInfo]:
        """List all services on a tunnel."""
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        return self._list_services(tunnel["id"], tunnel_name, username)

    def _list_services(self, tunnel_id: str, tunnel_name: str, username: str) -> list[ServiceInfo]:
        agent_id = extract_agent_id(tunnel_name, username)
        config = self.ops.get_tunnel_config(tunnel_id)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        services: list[ServiceInfo] = []
        for rule in rules:
            hostname = rule.get("hostname", "")
            svc_url = rule.get("service", "")
            svc_name = extract_service_name(hostname, agent_id, username, self.domain)
            if svc_name is not None:
                services.append(ServiceInfo(service_name=svc_name, hostname=hostname, service_url=svc_url))
        return services

    def _delete_dns_by_name(self, hostname: str) -> None:
        records = self.ops.list_dns_records(name=hostname)
        for record in records:
            self.ops.delete_dns_record(record["id"])

    def _delete_access_app_for_hostname(self, hostname: str) -> None:
        try:
            access_app = self.ops.get_access_app_by_domain(hostname)
            if access_app is not None:
                self.ops.delete_access_app(access_app["id"])
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.warning("Failed to delete Access Application for %s: %s", hostname, exc)

    def _apply_default_access_policy(self, tunnel_name: str, hostname: str) -> None:
        """Apply the tunnel's default auth policy to a new service, if one is set."""
        try:
            raw = self.ops.kv_get(tunnel_name)
            if raw is None:
                return
            policy = AuthPolicy.model_validate_json(raw)
            access_app = self.ops.create_access_app(hostname, f"cf-fwd-{hostname}")
            for cf_policy in policy_to_cf_rules(policy):
                self.ops.create_access_policy(access_app["id"], cf_policy)
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.warning("Failed to apply Access policy for %s: %s", hostname, exc)

    def _kv_delete_safe(self, key: str) -> None:
        try:
            self.ops.kv_delete(key)
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.warning("Failed to delete KV entry for %s: %s", key, exc)

    def create_service_token(self, tunnel_name: str, username: str, name: str) -> ServiceTokenInfo:
        """Create a Cloudflare Access service token and add it to all existing services on the tunnel.

        The service token can be used for programmatic access via
        CF-Access-Client-Id and CF-Access-Client-Secret headers.
        """
        self.verify_ownership(tunnel_name, username)
        result = self.ops.create_service_token(name)
        token_id = result["id"]
        client_id = result["client_id"]
        client_secret = result["client_secret"]

        # Add a non_identity policy for this service token to all existing services
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        config = self.ops.get_tunnel_config(tunnel["id"])
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        for rule in rules:
            hostname = rule.get("hostname", "")
            try:
                access_app = self.ops.get_access_app_by_domain(hostname)
                if access_app is not None:
                    self.ops.create_access_policy(
                        access_app["id"],
                        {
                            "name": f"Service token: {name}",
                            "decision": "non_identity",
                            "include": [{"service_token": {"token_id": token_id}}],
                            "precedence": 10,
                        },
                    )
            except (CloudflareApiError, httpx.HTTPError) as exc:
                logger.warning("Failed to add service token policy for %s: %s", hostname, exc)

        return ServiceTokenInfo(
            token_id=token_id,
            client_id=client_id,
            client_secret=client_secret,
            name=name,
        )

    def list_service_tokens(self) -> list[ServiceTokenInfo]:
        """List all service tokens in the account."""
        tokens = self.ops.list_service_tokens()
        return [
            ServiceTokenInfo(
                token_id=t["id"],
                client_id=t["client_id"],
                client_secret=None,
                name=t["name"],
            )
            for t in tokens
        ]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def authenticate_request(request: Request, ops: CloudflareOps) -> AuthResult:
    """Authenticate a request. Returns AdminAuth or AgentAuth."""
    auth_header = request.headers.get("authorization", "")

    if auth_header.lower().startswith("bearer "):
        return _authenticate_agent(auth_header[7:], ops)

    if auth_header.lower().startswith("basic "):
        return _authenticate_admin(auth_header)

    raise HTTPException(status_code=401, detail="Missing credentials")


def _authenticate_admin(auth_header: str) -> AdminAuth:
    """Validate HTTP Basic Auth credentials. Returns AdminAuth with username."""
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Malformed credentials") from exc
    username, _, password = decoded.partition(":")

    raw = os.environ.get("USER_CREDENTIALS", "")
    if not raw:
        raise HTTPException(status_code=500, detail="USER_CREDENTIALS not configured")
    creds: dict[str, str] = json.loads(raw)

    expected = creds.get(username)
    if expected is None or not secrets_module.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return AdminAuth(username=username)


def _authenticate_agent(token: str, ops: CloudflareOps) -> AgentAuth:
    """Validate a tunnel token. Returns AgentAuth with tunnel_id and tunnel_name."""
    try:
        decoded = base64.b64decode(token).decode("utf-8")
        token_data = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Malformed tunnel token") from exc

    tunnel_id = token_data.get("t")
    if not tunnel_id:
        raise HTTPException(status_code=401, detail="Invalid tunnel token: missing tunnel ID")

    tunnel = ops.get_tunnel_by_id(tunnel_id)
    if tunnel is None:
        raise HTTPException(status_code=401, detail="Invalid tunnel token: tunnel not found")

    return AgentAuth(tunnel_id=tunnel_id, tunnel_name=tunnel["name"])


def require_admin(auth: AuthResult) -> AdminAuth:
    """Require admin auth. Raises 403 if agent auth."""
    if isinstance(auth, AgentAuth):
        raise HTTPException(status_code=403, detail="This operation requires admin credentials")
    return auth


def require_tunnel_access(auth: AuthResult, tunnel_name: str) -> str:
    """Require access to a specific tunnel. Returns the username.
    Admin can access any tunnel. Agent can only access their own tunnel."""
    if isinstance(auth, AdminAuth):
        return auth.username
    if auth.tunnel_name != tunnel_name:
        raise HTTPException(status_code=403, detail=f"Token does not grant access to tunnel '{tunnel_name}'")
    return extract_username_from_tunnel_name(tunnel_name)


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


@functools.cache
def get_ctx() -> ForwardingCtx:
    ops = HttpCloudflareOps(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
    )
    return ForwardingCtx(ops=ops, domain=os.environ["CLOUDFLARE_DOMAIN"])


def raise_as_http(exc: Exception) -> NoReturn:
    """Convert domain exceptions to HTTPException."""
    if isinstance(exc, CloudflareApiError):
        logger.warning("Cloudflare API error: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail={"errors": exc.cf_errors}) from exc
    if isinstance(exc, TunnelNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, TunnelOwnershipError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ServiceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, InvalidTunnelComponentError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.exception("Unexpected error in endpoint handler")
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@contextlib.contextmanager
def handle_endpoint_errors() -> Iterator[None]:
    """Wrap endpoint logic: re-raise HTTPException, convert domain errors via raise_as_http."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise_as_http(exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

web_app = FastAPI()


@web_app.post("/tunnels")
def create_tunnel(request: Request, body: CreateTunnelRequest) -> dict[str, object]:
    """Create a tunnel (idempotent) and return its info with token."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        return get_ctx().create_tunnel(admin.username, body.agent_id, body.default_auth_policy).model_dump()


@web_app.get("/tunnels")
def list_tunnels(request: Request) -> list[dict[str, object]]:
    """List all tunnels belonging to the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        return [t.model_dump() for t in get_ctx().list_tunnels(admin.username)]


@web_app.delete("/tunnels/{tunnel_name}")
def delete_tunnel(request: Request, tunnel_name: str) -> dict[str, str]:
    """Delete a tunnel and all its associated DNS records, Access Applications, ingress rules, and KV entries."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        get_ctx().delete_tunnel(tunnel_name, admin.username)
        return {"status": "deleted"}


@web_app.post("/tunnels/{tunnel_name}/services")
def add_service(request: Request, tunnel_name: str, body: AddServiceRequest) -> dict[str, object]:
    """Add a service to a tunnel. Works with both admin and agent auth."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        username = require_tunnel_access(auth, tunnel_name)
        return get_ctx().add_service(tunnel_name, username, body.service_name, body.service_url).model_dump()


@web_app.delete("/tunnels/{tunnel_name}/services/{service_name}")
def remove_service(request: Request, tunnel_name: str, service_name: str) -> dict[str, str]:
    """Remove a service from a tunnel. Works with both admin and agent auth."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        username = require_tunnel_access(auth, tunnel_name)
        get_ctx().remove_service(tunnel_name, username, service_name)
        return {"status": "deleted"}


@web_app.get("/tunnels/{tunnel_name}/services")
def list_services(request: Request, tunnel_name: str) -> list[dict[str, object]]:
    """List services on a tunnel. Works with both admin and agent auth."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        username = require_tunnel_access(auth, tunnel_name)
        return [s.model_dump() for s in get_ctx().list_services(tunnel_name, username)]


@web_app.get("/tunnels/{tunnel_name}/auth")
def get_tunnel_auth(request: Request, tunnel_name: str) -> dict[str, object]:
    """Get the default auth policy for a tunnel."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        policy = get_ctx().get_tunnel_auth(tunnel_name)
        if policy is None:
            return {"rules": []}
        return policy.model_dump()


@web_app.put("/tunnels/{tunnel_name}/auth")
def set_tunnel_auth(request: Request, tunnel_name: str, body: AuthPolicy) -> dict[str, str]:
    """Set the default auth policy for a tunnel."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        get_ctx().set_tunnel_auth(tunnel_name, body)
        return {"status": "updated"}


@web_app.get("/tunnels/{tunnel_name}/services/{service_name}/auth")
def get_service_auth(request: Request, tunnel_name: str, service_name: str) -> dict[str, object]:
    """Get the auth policy for a specific service."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        policy = get_ctx().get_service_auth(tunnel_name, admin.username, service_name)
        if policy is None:
            return {"rules": []}
        return policy.model_dump()


@web_app.post("/tunnels/{tunnel_name}/service-tokens")
def create_service_token_endpoint(
    request: Request, tunnel_name: str, body: CreateServiceTokenRequest
) -> dict[str, object]:
    """Create a service token for programmatic access to this tunnel's services."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        token = get_ctx().create_service_token(tunnel_name, admin.username, body.name)
        return token.model_dump()


@web_app.get("/tunnels/{tunnel_name}/service-tokens")
def list_service_tokens_endpoint(request: Request, tunnel_name: str) -> list[dict[str, object]]:
    """List service tokens. Note: secrets are not returned."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        return [t.model_dump() for t in get_ctx().list_service_tokens()]


@web_app.put("/tunnels/{tunnel_name}/services/{service_name}/auth")
def set_service_auth(request: Request, tunnel_name: str, service_name: str, body: AuthPolicy) -> dict[str, str]:
    """Set the auth policy for a specific service."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        get_ctx().set_service_auth(tunnel_name, admin.username, service_name, body)
        return {"status": "updated"}


# ---------------------------------------------------------------------------
# Modal deployment
# ---------------------------------------------------------------------------

image = modal.Image.debian_slim().pip_install("fastapi[standard]", "httpx")
app = modal.App(name="cloudflare-forwarding", image=image)


@app.function(secrets=[modal.Secret.from_name("cloudflare-forwarding-secrets")])
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    return web_app
