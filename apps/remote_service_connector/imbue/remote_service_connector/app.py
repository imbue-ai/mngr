"""Remote service connector, deployed as a Modal function.

Exposes authenticated HTTP endpoints for managing remote services used by the
minds desktop client: Cloudflare tunnels (`/tunnels/*`) and SuperTokens-backed
authentication (`/auth/*`). More remote-service capabilities (e.g. creating
remote hosts on behalf of users) will be added here over time.

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
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any
from typing import NoReturn
from typing import Protocol

import httpx
import modal
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydantic import Field
from supertokens_python import InputAppInfo
from supertokens_python import SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.asyncio import list_users_by_account_info
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe import emailpassword as st_emailpassword_recipe
from supertokens_python.recipe import emailverification as st_emailverification_recipe
from supertokens_python.recipe import session as st_session_recipe
from supertokens_python.recipe import thirdparty as st_thirdparty_recipe
from supertokens_python.recipe.emailpassword.asyncio import consume_password_reset_token
from supertokens_python.recipe.emailpassword.asyncio import send_reset_password_email
from supertokens_python.recipe.emailpassword.asyncio import sign_in as ep_sign_in
from supertokens_python.recipe.emailpassword.asyncio import sign_up as ep_sign_up
from supertokens_python.recipe.emailpassword.asyncio import update_email_or_password
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import PasswordPolicyViolationError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailverification import EmailVerificationClaim
from supertokens_python.recipe.emailverification.asyncio import is_email_verified
from supertokens_python.recipe.emailverification.asyncio import send_email_verification_email
from supertokens_python.recipe.emailverification.asyncio import verify_email_using_token
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.session.asyncio import create_new_session_without_request_response
from supertokens_python.recipe.session.asyncio import refresh_session_without_request_response
from supertokens_python.recipe.session.asyncio import revoke_all_sessions_for_user
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import get_session_without_request_response
from supertokens_python.recipe.thirdparty.asyncio import get_provider
from supertokens_python.recipe.thirdparty.asyncio import manually_create_or_update_user
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.syncio import get_user
from supertokens_python.types import RecipeUserId
from supertokens_python.types.base import AccountInfoInput

logger = logging.getLogger(__name__)

_CF_BASE_URL = "https://api.cloudflare.com/client/v4"
TUNNEL_NAME_SEP = "--"
KV_NAMESPACE_TITLE = "cloudflare-forwarding-defaults"

_HTML_SHARED_STYLES = (
    "body{font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;"
    "display:flex;justify-content:center;align-items:center;min-height:100vh;"
    "margin:0;padding:20px}"
    ".card{background:white;border-radius:12px;padding:40px;max-width:420px;"
    "width:100%;box-shadow:0 1px 3px rgba(0,0,0,0.1);text-align:center}"
    "h1{margin:0 0 8px;font-size:22px;color:#0f172a}"
    "p{margin:0 0 16px;color:#475569;font-size:14px}"
    "label{display:block;text-align:left;font-size:13px;color:#334155;margin:8px 0 6px}"
    "input{width:100%;padding:10px 12px;border:1px solid #e2e8f0;border-radius:8px;"
    "font-size:14px;font-family:inherit;box-sizing:border-box}"
    "button{width:100%;padding:12px;background:#1e293b;color:white;border:none;"
    "border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;"
    "font-family:inherit;margin-top:12px}"
    "button:disabled{background:#94a3b8;cursor:not-allowed}"
    ".error{color:#dc2626;font-size:13px;margin-top:12px;display:none}"
    ".success{color:#15803d;font-size:13px;margin-top:12px;display:none}"
)

_VERIFY_EMAIL_SUCCESS_HTML = (
    "<!doctype html><html><head><title>Email verified</title><style>"
    + _HTML_SHARED_STYLES
    + "</style></head><body><div class='card'>"
    "<h1 style='color:#15803d'>Email verified</h1>"
    "<p>Your email has been verified. You may close this tab and return to the app.</p>"
    "</div></body></html>"
)

_VERIFY_EMAIL_FAILED_HTML = (
    "<!doctype html><html><head><title>Verification failed</title><style>"
    + _HTML_SHARED_STYLES
    + "</style></head><body><div class='card'>"
    "<h1 style='color:#dc2626'>Verification failed</h1>"
    "<p>The verification link is invalid or has expired. "
    "Request a new one from the app.</p>"
    "</div></body></html>"
)

_RESET_PASSWORD_PAGE_TEMPLATE = (
    "<!doctype html><html><head><title>Reset password</title><style>"
    + _HTML_SHARED_STYLES
    + "</style></head><body><div class='card'>"
    "<h1>Set new password</h1><p>Enter your new password below.</p>"
    "<form id='f' onsubmit='return submitForm(event)'>"
    "<label for='p'>New password</label>"
    "<input id='p' type='password' minlength='8' autocomplete='new-password' required>"
    "<label for='c'>Confirm password</label>"
    "<input id='c' type='password' minlength='8' autocomplete='new-password' required>"
    "<button id='b' type='submit'>Reset password</button>"
    "<div id='err' class='error'></div>"
    "<div id='ok' class='success'></div>"
    "</form>"
    "<script>"
    "const TOKEN=__TOKEN_JSON__;"
    "async function submitForm(ev){ev.preventDefault();"
    "const p=document.getElementById('p').value;"
    "const c=document.getElementById('c').value;"
    "const err=document.getElementById('err');err.style.display='none';"
    "if(p!==c){err.textContent='Passwords do not match';err.style.display='block';return false;}"
    "const btn=document.getElementById('b');btn.disabled=true;"
    "try{const r=await fetch('/auth/password/reset',{method:'POST',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({token:TOKEN,new_password:p})});"
    "const d=await r.json();"
    "if(d.status==='OK'){document.getElementById('ok').textContent='Password reset. You can sign in now.';"
    "document.getElementById('ok').style.display='block';"
    "document.getElementById('f').style.display='none';}"
    "else{err.textContent=d.message||'Reset failed';err.style.display='block';btn.disabled=false;}}"
    "catch(e){err.textContent='Network error';err.style.display='block';btn.disabled=false;}"
    "return false;}"
    "</script>"
    "</div></body></html>"
)


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


class TunnelComponentTooLongError(ValueError):
    """Raised when a tunnel component exceeds the maximum length."""

    def __init__(self, component_name: str, value: str, max_length: int) -> None:
        self.component_name = component_name
        self.value = value
        self.max_length = max_length
        super().__init__(f"{component_name} '{value}' exceeds maximum length of {max_length}")


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


def cf_create_access_app(
    client: httpx.Client,
    account_id: str,
    hostname: str,
    app_name: str,
    allowed_idps: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": app_name,
        "domain": hostname,
        "type": "self_hosted",
        "session_duration": "24h",
    }
    if allowed_idps is not None:
        body["allowed_idps"] = allowed_idps
    response = client.post(
        f"/accounts/{account_id}/access/apps",
        json=body,
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


_MAX_USERNAME_LENGTH = 22
_MAX_SERVICE_NAME_LENGTH = 21
_AGENT_ID_PREFIX_LENGTH = 16


def truncate_agent_id(agent_id: str) -> str:
    """Truncate an agent ID to a short prefix for use in hostnames.

    Strips the "agent-" prefix (if present) and takes the first 16 hex chars.
    16 chars of hex provides sufficient uniqueness per user.
    """
    raw = agent_id.removeprefix("agent-")
    return raw[:_AGENT_ID_PREFIX_LENGTH]


def _validate_username(username: str) -> None:
    if TUNNEL_NAME_SEP in username:
        raise InvalidTunnelComponentError("Username", username, TUNNEL_NAME_SEP)
    if len(username) > _MAX_USERNAME_LENGTH:
        raise TunnelComponentTooLongError("Username", username, _MAX_USERNAME_LENGTH)


def _validate_service_name(service_name: str) -> None:
    if TUNNEL_NAME_SEP in service_name:
        raise InvalidTunnelComponentError("Service name", service_name, TUNNEL_NAME_SEP)
    if len(service_name) > _MAX_SERVICE_NAME_LENGTH:
        raise TunnelComponentTooLongError("Service name", service_name, _MAX_SERVICE_NAME_LENGTH)


def make_tunnel_name(username: str, agent_id: str) -> str:
    _validate_username(username)
    short_id = truncate_agent_id(agent_id)
    return f"{username}{TUNNEL_NAME_SEP}{short_id}"


def make_hostname(service_name: str, agent_id: str, username: str, domain: str) -> str:
    _validate_service_name(service_name)
    short_id = truncate_agent_id(agent_id)
    return f"{service_name}--{short_id}--{username}.{domain}"


def extract_agent_id_prefix(tunnel_name: str, username: str) -> str:
    """Extract the truncated agent ID prefix from a tunnel name."""
    prefix = f"{username}{TUNNEL_NAME_SEP}"
    if not tunnel_name.startswith(prefix):
        raise TunnelOwnershipError(tunnel_name, username)
    return tunnel_name[len(prefix) :]


def extract_service_name(hostname: str, agent_id_prefix: str, username: str, domain: str) -> str | None:
    expected_suffix = f"--{agent_id_prefix}--{username}.{domain}"
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
    def create_access_app(
        self, hostname: str, app_name: str, allowed_idps: list[str] | None = None
    ) -> dict[str, Any]: ...
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

    def create_access_app(self, hostname: str, app_name: str, allowed_idps: list[str] | None = None) -> dict[str, Any]:
        return cf_create_access_app(self.client, self.account_id, hostname, app_name, allowed_idps=allowed_idps)

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

    def __init__(self, ops: CloudflareOps, domain: str, allowed_idps: list[str] | None = None) -> None:
        self.ops = ops
        self.domain = domain
        self.allowed_idps = allowed_idps

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
            # Update the default auth policy if provided (may have been missing
            # from the original creation or may need updating)
            if default_auth_policy is not None:
                self.ops.kv_put(name, default_auth_policy.model_dump_json())
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
        agent_id = extract_agent_id_prefix(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        self.ops.create_cname(hostname, f"{tid}.cfargotunnel.com")
        config = self.ops.get_tunnel_config(tid)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        rules.append(
            {
                "hostname": hostname,
                "service": service_url,
                "originRequest": {"noTLSVerify": True},
            }
        )
        self.ops.put_tunnel_config(tid, wrap_ingress(rules))

        self._apply_default_access_policy(tunnel_name, hostname)

        return ServiceInfo(service_name=service_name, hostname=hostname, service_url=service_url)

    def remove_service(self, tunnel_name: str, username: str, service_name: str) -> None:
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id_prefix(tunnel_name, username)
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
        agent_id = extract_agent_id_prefix(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            return None
        policies = self.ops.list_access_policies(access_app["id"])
        return cf_policies_to_auth_policy(policies)

    def set_service_auth(self, tunnel_name: str, username: str, service_name: str, policy: AuthPolicy) -> None:
        """Set the auth policy for a specific service on its Access Application."""
        agent_id = extract_agent_id_prefix(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            access_app = self.ops.create_access_app(hostname, f"cf-fwd-{service_name}", allowed_idps=self.allowed_idps)

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
        agent_id = extract_agent_id_prefix(tunnel_name, username)
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
            access_app = self.ops.create_access_app(hostname, f"cf-fwd-{hostname}", allowed_idps=self.allowed_idps)
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
    """Authenticate a request. Returns AdminAuth or AgentAuth.

    Supports two Bearer-token auth methods:
    1. Base64-encoded Cloudflare tunnel token (agent auth, scoped to one tunnel).
    2. SuperTokens JWT (user auth, treated as admin; user_id_prefix is the username).
    """
    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")

    token = auth_header[7:]
    # Try tunnel token first.
    agent_exc: HTTPException | None = None
    try:
        return _authenticate_agent(token, ops)
    except HTTPException as exc:
        agent_exc = exc
    # Only try SuperTokens JWT if it is configured; otherwise preserve the
    # original agent auth error so callers receive a meaningful message.
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        assert agent_exc is not None
        raise agent_exc
    # If SuperTokens also fails, raise the SuperTokens error since the
    # token is clearly a JWT (not a base64 tunnel token).
    try:
        return _authenticate_supertokens(token)
    except HTTPException as st_exc:
        raise st_exc from None


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


_USER_ID_PREFIX_LENGTH = 16


def _authenticate_supertokens(
    token: str,
    session_getter: Callable[..., Any] = get_session_without_request_response,
) -> AdminAuth:
    """Validate a SuperTokens JWT access token. Returns AdminAuth with user_id_prefix as username."""
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        raise HTTPException(status_code=401, detail="SuperTokens not configured")

    try:
        session = session_getter(
            access_token=token,
            anti_csrf_check=False,
        )
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")

    # Reject tokens where the email is not verified
    payload = session.get_access_token_payload()
    is_verified = EmailVerificationClaim.get_value_from_payload(payload)
    if not is_verified:
        raise HTTPException(status_code=401, detail="Email not verified")

    user_id = session.get_user_id()
    # Derive 16-char hex prefix from UUID
    user_id_prefix = user_id.replace("-", "")[:_USER_ID_PREFIX_LENGTH]

    return AdminAuth(username=user_id_prefix)


def _get_user_id_from_access_token(token: str) -> str:
    """Validate a SuperTokens JWT and return the full user_id (not just the prefix).

    Raises ``HTTPException(401)`` on any validation failure. Used by auth-proxy
    endpoints that need the full user_id to drive an API call (e.g. revoke).
    """
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        raise HTTPException(status_code=401, detail="SuperTokens not configured")
    try:
        session = get_session_without_request_response(access_token=token, anti_csrf_check=False)
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")
    return session.get_user_id()


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
    raw_idps = os.environ.get("CLOUDFLARE_ALLOWED_IDPS", "")
    allowed_idps = [s.strip() for s in raw_idps.split(",") if s.strip()] or None
    return ForwardingCtx(ops=ops, domain=os.environ["CLOUDFLARE_DOMAIN"], allowed_idps=allowed_idps)


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
    if isinstance(exc, TunnelComponentTooLongError):
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
# SuperTokens auth proxy endpoints
#
# These endpoints front the SuperTokens core so that clients (e.g. the minds
# desktop client) never need to know the ``SUPERTOKENS_API_KEY``. All endpoints
# here are unauthenticated: signing in is itself the authentication flow, and
# the sensitive operations (core API key, OAuth client secrets) stay on this
# server.
# ---------------------------------------------------------------------------


_AUTH_TENANT_ID = "public"


class SessionTokens(BaseModel):
    access_token: str = Field(description="SuperTokens JWT access token")
    refresh_token: str | None = Field(default=None, description="SuperTokens refresh token")


class AuthUser(BaseModel):
    user_id: str = Field(description="SuperTokens user ID (UUID v4)")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="Display name from OAuth provider, if any")


class SignUpRequest(BaseModel):
    email: str = Field(description="Email address to register")
    password: str = Field(description="Password for the new account")


class SignInRequest(BaseModel):
    email: str = Field(description="Email address")
    password: str = Field(description="Password")


class AuthResponse(BaseModel):
    status: str = Field(description="OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, FIELD_ERROR, or ERROR")
    message: str | None = Field(default=None, description="Human-readable message for non-OK statuses")
    user: AuthUser | None = Field(default=None, description="User info when status is OK")
    tokens: SessionTokens | None = Field(default=None, description="Session tokens when status is OK")
    needs_email_verification: bool = Field(
        default=False,
        description="True when the account's email has not yet been verified",
    )


class RefreshSessionRequest(BaseModel):
    refresh_token: str = Field(description="Existing refresh token")


class RefreshSessionResponse(BaseModel):
    status: str = Field(description="OK or ERROR")
    tokens: SessionTokens | None = Field(default=None, description="New tokens when status is OK")
    message: str | None = Field(default=None, description="Error detail if status is not OK")


class SendVerificationEmailRequest(BaseModel):
    user_id: str = Field(description="SuperTokens user ID")
    email: str = Field(description="Email address to send verification to")


class IsEmailVerifiedRequest(BaseModel):
    user_id: str = Field(description="SuperTokens user ID")
    email: str = Field(description="Email address to check")


class ForgotPasswordRequest(BaseModel):
    email: str = Field(description="Email address to send reset link to")


class ResetPasswordRequest(BaseModel):
    token: str = Field(description="Password reset token from email")
    new_password: str = Field(description="New password to set")


class OAuthAuthorizeRequest(BaseModel):
    provider_id: str = Field(description="Third-party provider ID (e.g. 'google', 'github')")
    callback_url: str = Field(description="Callback URL registered with the provider")


class OAuthAuthorizeResponse(BaseModel):
    status: str = Field(description="OK or ERROR")
    url: str | None = Field(default=None, description="URL to redirect the user to when status is OK")
    message: str | None = Field(default=None, description="Error detail if status is not OK")


class OAuthCallbackRequest(BaseModel):
    provider_id: str = Field(description="Third-party provider ID")
    callback_url: str = Field(description="Same callback URL used when starting the flow")
    query_params: dict[str, str] = Field(description="Query params the provider sent back to the callback URL")


class UserProviderInfo(BaseModel):
    user_id: str = Field(description="SuperTokens user ID")
    email: str | None = Field(default=None, description="Primary email if known")
    provider: str = Field(description="Login method: 'email' or a third-party provider ID")


async def _build_session_tokens(user_id: str) -> SessionTokens:
    """Create a new SuperTokens session for the given user and return the tokens."""
    session = await create_new_session_without_request_response(
        tenant_id=_AUTH_TENANT_ID,
        recipe_user_id=RecipeUserId(user_id),
    )
    raw = session.get_all_session_tokens_dangerously()
    return SessionTokens(
        access_token=raw["accessToken"],
        refresh_token=raw["refreshToken"] or None,
    )


def _require_supertokens_configured() -> None:
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        raise HTTPException(status_code=503, detail="SuperTokens not configured on the server")


@web_app.post("/auth/signup", response_model=AuthResponse)
async def auth_signup(body: SignUpRequest) -> AuthResponse:
    """Create a new email/password account and return a session + user info.

    Any exception from the SuperTokens SDK (core unreachable, schema mismatch,
    etc.) is caught and surfaced as a structured ``AuthResponse(status="ERROR")``
    so the desktop client receives a stable JSON shape rather than a FastAPI
    default 500 body that its typed client cannot parse.
    """
    _require_supertokens_configured()
    email = body.email.strip()
    if not email or not body.password:
        return AuthResponse(status="FIELD_ERROR", message="Email and password are required")

    try:
        result = await ep_sign_up(tenant_id=_AUTH_TENANT_ID, email=email, password=body.password)

        if isinstance(result, EmailAlreadyExistsError):
            return AuthResponse(status="EMAIL_ALREADY_EXISTS", message="An account with this email already exists")

        if not isinstance(result, EPSignUpOkResult):
            return AuthResponse(status="ERROR", message="Sign-up failed")

        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        tokens = await _build_session_tokens(user.id)
        await send_email_verification_email(
            tenant_id=_AUTH_TENANT_ID,
            user_id=user.id,
            recipe_user_id=recipe_user_id,
            email=email,
        )
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.error("SuperTokens SDK error during signup: %s", exc)
        return AuthResponse(status="ERROR", message="Auth backend unavailable")
    return AuthResponse(
        status="OK",
        user=AuthUser(user_id=user.id, email=email),
        tokens=tokens,
        needs_email_verification=True,
    )


@web_app.post("/auth/signin", response_model=AuthResponse)
async def auth_signin(body: SignInRequest) -> AuthResponse:
    """Authenticate with email/password and return a session + user info.

    Any exception from the SuperTokens SDK is caught and returned as
    ``AuthResponse(status="ERROR")`` -- see the ``auth_signup`` docstring for
    the rationale.
    """
    _require_supertokens_configured()
    email = body.email.strip()
    if not email or not body.password:
        return AuthResponse(status="FIELD_ERROR", message="Email and password are required")

    try:
        result = await ep_sign_in(tenant_id=_AUTH_TENANT_ID, email=email, password=body.password)

        if isinstance(result, WrongCredentialsError):
            return AuthResponse(status="WRONG_CREDENTIALS", message="Incorrect email or password")

        if not isinstance(result, EPSignInOkResult):
            return AuthResponse(status="ERROR", message="Sign-in failed")

        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        verified = await is_email_verified(recipe_user_id=recipe_user_id, email=email)
        tokens = await _build_session_tokens(user.id)
        if not verified:
            await send_email_verification_email(
                tenant_id=_AUTH_TENANT_ID,
                user_id=user.id,
                recipe_user_id=recipe_user_id,
                email=email,
            )
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.error("SuperTokens SDK error during signin: %s", exc)
        return AuthResponse(status="ERROR", message="Auth backend unavailable")
    return AuthResponse(
        status="OK",
        user=AuthUser(user_id=user.id, email=email),
        tokens=tokens,
        needs_email_verification=not verified,
    )


@web_app.post("/auth/session/refresh", response_model=RefreshSessionResponse)
async def auth_refresh_session(body: RefreshSessionRequest) -> RefreshSessionResponse:
    """Exchange a refresh token for a fresh access/refresh token pair."""
    _require_supertokens_configured()
    try:
        new_session = await refresh_session_without_request_response(refresh_token=body.refresh_token)
    except (SuperTokensSessionError, SuperTokensGeneralError, ValueError, TypeError) as exc:
        return RefreshSessionResponse(status="ERROR", message=str(exc))
    raw = new_session.get_all_session_tokens_dangerously()
    return RefreshSessionResponse(
        status="OK",
        tokens=SessionTokens(
            access_token=raw["accessToken"],
            refresh_token=raw["refreshToken"] or None,
        ),
    )


@web_app.post("/auth/session/revoke")
async def auth_revoke_sessions(request: Request) -> dict[str, object]:
    """Revoke every SuperTokens session for the caller's user.

    Authentication: the caller must send their own SuperTokens access token as
    ``Authorization: Bearer <access_token>``. The user_id is derived from that
    session, not trusted from the request body -- otherwise an anonymous
    attacker could terminate arbitrary users' sessions just by guessing /
    learning their user_id UUID.

    Called by the minds client on sign-out so the access/refresh tokens stored
    on the user's machine become useless even if copied off-box. Idempotent --
    no-op when the caller has no other active sessions.
    """
    _require_supertokens_configured()
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    user_id = _get_user_id_from_access_token(auth_header[7:])
    revoked = await revoke_all_sessions_for_user(user_id=user_id)
    logger.info("Revoked %d sessions for user %s...", len(revoked), user_id[:8])
    return {"status": "OK", "revoked_count": len(revoked)}


@web_app.post("/auth/email/send-verification")
async def auth_send_verification_email(body: SendVerificationEmailRequest) -> dict[str, str]:
    """(Re)send the verification email for a given user."""
    _require_supertokens_configured()
    user = get_user(body.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(body.user_id)
    await send_email_verification_email(
        tenant_id=_AUTH_TENANT_ID,
        user_id=body.user_id,
        recipe_user_id=recipe_user_id,
        email=body.email,
    )
    return {"status": "OK"}


@web_app.post("/auth/email/is-verified")
async def auth_is_email_verified(body: IsEmailVerifiedRequest) -> dict[str, bool]:
    """Return whether the given user's email is verified."""
    _require_supertokens_configured()
    user = get_user(body.user_id)
    if user is None:
        return {"verified": False}
    recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(body.user_id)
    verified = await is_email_verified(recipe_user_id=recipe_user_id, email=body.email)
    return {"verified": verified}


@web_app.get("/auth/verify-email", response_class=HTMLResponse)
async def auth_verify_email_page(request: Request) -> HTMLResponse:
    """Handle an email verification link click from an email.

    Returns a human-readable HTML page indicating success or failure. Reads the
    ``token`` and ``tenantId`` query parameters directly rather than declaring
    them as function arguments, since SuperTokens camel-cases ``tenantId`` in
    emitted links and we do not want that to leak into the Python identifier.
    """
    _require_supertokens_configured()
    token = request.query_params.get("token", "")
    tenant_id = request.query_params.get("tenantId") or _AUTH_TENANT_ID
    if not token:
        return HTMLResponse(_VERIFY_EMAIL_FAILED_HTML, status_code=400)
    try:
        result = await verify_email_using_token(tenant_id=tenant_id, token=token)
    except (SuperTokensSessionError, SuperTokensGeneralError, ValueError) as exc:
        logger.error("Email verification error: %s", exc)
        return HTMLResponse(_VERIFY_EMAIL_FAILED_HTML, status_code=400)
    if isinstance(result, VerifyEmailUsingTokenOkResult):
        return HTMLResponse(_VERIFY_EMAIL_SUCCESS_HTML)
    return HTMLResponse(_VERIFY_EMAIL_FAILED_HTML, status_code=400)


@web_app.get("/auth/reset-password", response_class=HTMLResponse)
def auth_reset_password_page(token: str = "") -> HTMLResponse:
    """Render the password-reset form linked from a password-reset email."""
    _require_supertokens_configured()
    safe_token = json.dumps(token)
    return HTMLResponse(_RESET_PASSWORD_PAGE_TEMPLATE.replace("__TOKEN_JSON__", safe_token))


@web_app.post("/auth/password/forgot")
async def auth_forgot_password(body: ForgotPasswordRequest) -> dict[str, str]:
    """Send a password reset email for the given address (always succeeds).

    Swallows any backend error (SuperTokens core unreachable, schema mismatch,
    etc.) so that this endpoint's response is byte-identical whether or not an
    account exists for the given address -- a non-200 response for "unknown
    email" vs a 200 for "known email" would leak enumeration signal, and a
    500 on intermittent SuperTokens outages would violate the docstring's
    "always succeeds" contract.
    """
    _require_supertokens_configured()
    email = body.email.strip()
    success = {"status": "OK", "message": "If an account exists, a reset email has been sent"}
    if not email:
        return success
    try:
        users = await list_users_by_account_info(
            tenant_id=_AUTH_TENANT_ID,
            account_info=AccountInfoInput(email=email),
        )
        if not users:
            return success
        user_id = users[0].id
        result = await send_reset_password_email(tenant_id=_AUTH_TENANT_ID, user_id=user_id, email=email)
        if result == "UNKNOWN_USER_ID_ERROR":
            logger.warning("Failed to send password reset email for user %s", user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Auth backend error during forgot-password; returning generic success: %s", exc)
    return success


@web_app.post("/auth/password/reset")
async def auth_reset_password(body: ResetPasswordRequest) -> dict[str, str]:
    """Consume a password reset token and set a new password."""
    _require_supertokens_configured()
    if not body.token or not body.new_password:
        raise HTTPException(status_code=400, detail="Token and new password are required")

    consume_result = await consume_password_reset_token(tenant_id=_AUTH_TENANT_ID, token=body.token)
    if not isinstance(consume_result, ConsumePasswordResetTokenOkResult):
        return {"status": "INVALID_TOKEN", "message": "Invalid or expired reset token"}

    update_result = await update_email_or_password(
        recipe_user_id=RecipeUserId(consume_result.user_id),
        password=body.new_password,
    )
    if isinstance(update_result, PasswordPolicyViolationError):
        return {"status": "FIELD_ERROR", "message": update_result.failure_reason}
    if not isinstance(update_result, UpdateEmailOrPasswordOkResult):
        raise HTTPException(status_code=500, detail="Failed to update password")
    return {"status": "OK", "message": "Password has been reset"}


@web_app.post("/auth/oauth/authorize", response_model=OAuthAuthorizeResponse)
async def auth_oauth_authorize(body: OAuthAuthorizeRequest) -> OAuthAuthorizeResponse:
    """Return the URL to which the user should be redirected to begin OAuth."""
    _require_supertokens_configured()
    provider = await get_provider(tenant_id=_AUTH_TENANT_ID, third_party_id=body.provider_id)
    if provider is None:
        return OAuthAuthorizeResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")
    redirect = await provider.get_authorisation_redirect_url(
        redirect_uri_on_provider_dashboard=body.callback_url,
        user_context={},
    )
    return OAuthAuthorizeResponse(status="OK", url=redirect.url_with_query_params)


@web_app.post("/auth/oauth/callback", response_model=AuthResponse)
async def auth_oauth_callback(body: OAuthCallbackRequest) -> AuthResponse:
    """Exchange an OAuth callback's query params for a supertokens session."""
    _require_supertokens_configured()
    provider = await get_provider(tenant_id=_AUTH_TENANT_ID, third_party_id=body.provider_id)
    if provider is None:
        return AuthResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")

    try:
        oauth_tokens = await provider.exchange_auth_code_for_oauth_tokens(
            redirect_uri_info=RedirectUriInfo(
                redirect_uri_on_provider_dashboard=body.callback_url,
                redirect_uri_query_params=dict(body.query_params),
                pkce_code_verifier=None,
            ),
            user_context={},
        )
        oauth_user = await provider.get_user_info(oauth_tokens=oauth_tokens, user_context={})
    except (ValueError, KeyError, OSError) as exc:
        logger.error("OAuth callback failed for %s: %s", body.provider_id, exc)
        return AuthResponse(status="ERROR", message=str(exc))

    if oauth_user.email is None or oauth_user.email.id is None:
        return AuthResponse(status="ERROR", message="No email provided by the OAuth provider")

    email = oauth_user.email.id
    result = await manually_create_or_update_user(
        tenant_id=_AUTH_TENANT_ID,
        third_party_id=body.provider_id,
        third_party_user_id=oauth_user.third_party_user_id,
        email=email,
        is_verified=oauth_user.email.is_verified,
    )
    if not isinstance(result, ManuallyCreateOrUpdateUserOkResult):
        return AuthResponse(status="ERROR", message="Could not create or update account")

    display_name: str | None = None
    if oauth_user.raw_user_info_from_provider and oauth_user.raw_user_info_from_provider.from_user_info_api:
        raw = oauth_user.raw_user_info_from_provider.from_user_info_api
        display_name = raw.get("name") or raw.get("login") or raw.get("displayName")

    tokens = await _build_session_tokens(result.user.id)
    return AuthResponse(
        status="OK",
        user=AuthUser(user_id=result.user.id, email=email, display_name=display_name),
        tokens=tokens,
        needs_email_verification=not oauth_user.email.is_verified,
    )


@web_app.get("/auth/users/{user_id}", response_model=UserProviderInfo)
def auth_get_user(user_id: str) -> UserProviderInfo:
    """Return basic info about a user, including the provider used to sign in."""
    _require_supertokens_configured()
    user = get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    provider = "email"
    email: str | None = None
    for login_method in user.login_methods:
        if login_method.third_party is not None and provider == "email":
            provider = login_method.third_party.id
        if email is None and login_method.email:
            email = login_method.email
    return UserProviderInfo(user_id=user_id, email=email, provider=provider)


# ---------------------------------------------------------------------------
# Modal deployment
#
# Secrets are environment-scoped so the same code can back a production, staging,
# or ad-hoc deploy without editing this file. ``MNGR_DEPLOY_ENV`` is resolved at
# local ``modal deploy`` time (``modal.is_local()``) from the deployer's shell
# and used to select the correct ``cloudflare-<env>`` / ``supertokens-<env>``
# Modal secrets. The same value is also baked into a ``Secret.from_dict`` so the
# running container can read ``os.environ["MNGR_DEPLOY_ENV"]`` at runtime.
# ---------------------------------------------------------------------------


_DEPLOY_ENV = os.environ.get("MNGR_DEPLOY_ENV", "production")

image = modal.Image.debian_slim().pip_install("fastapi[standard]", "httpx", "supertokens-python")
app = modal.App(name=f"remote-service-connector-{_DEPLOY_ENV}", image=image)


# Modal URLs follow ``{workspace}--{app-name}-{function-name}.modal.run``, with
# underscores in identifiers normalized to hyphens. For this deployment that's
# ``joshalbrecht--remote-service-connector-<env>-fastapi-app.modal.run``. This
# fallback is only used when AUTH_WEBSITE_DOMAIN is not set in the secret; in
# practice we set it explicitly from ``.minds/<env>/supertokens.sh``.
_MODAL_WORKSPACE = "joshalbrecht"
_DEFAULT_CONNECTOR_DOMAIN = f"https://{_MODAL_WORKSPACE}--remote-service-connector-{_DEPLOY_ENV}-fastapi-app.modal.run"


def _get_auth_website_domain() -> str:
    """Return the public URL used in outbound email links (verification, reset)."""
    return os.environ.get("AUTH_WEBSITE_DOMAIN", _DEFAULT_CONNECTOR_DOMAIN)


def _build_oauth_providers() -> list[ProviderInput]:
    """Build the OAuth provider list from env vars."""
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    github_client_id = os.environ.get("GITHUB_CLIENT_ID")
    github_client_secret = os.environ.get("GITHUB_CLIENT_SECRET")

    providers: list[ProviderInput] = []
    if google_client_id and google_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="google",
                    clients=[
                        ProviderClientConfig(
                            client_id=google_client_id,
                            client_secret=google_client_secret,
                        )
                    ],
                ),
            )
        )
    if github_client_id and github_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="github",
                    clients=[
                        ProviderClientConfig(
                            client_id=github_client_id,
                            client_secret=github_client_secret,
                        )
                    ],
                ),
            )
        )
    return providers


def _init_supertokens() -> None:
    """Initialize SuperTokens SDK with all recipes used by the minds auth flow.

    Includes emailpassword, thirdparty (OAuth), emailverification, and session.
    The SDK keeps its API key (``SUPERTOKENS_API_KEY``) server-side so clients
    never see it. OAuth client credentials (``GOOGLE_CLIENT_ID``/``SECRET``,
    ``GITHUB_CLIENT_ID``/``SECRET``) likewise live only on the server.
    """
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        return

    api_key = os.environ.get("SUPERTOKENS_API_KEY")
    website_domain = _get_auth_website_domain()
    providers = _build_oauth_providers()

    thirdparty_recipe_init = (
        st_thirdparty_recipe.init(
            sign_in_and_up_feature=st_thirdparty_recipe.SignInAndUpFeature(providers=providers),
        )
        if providers
        else st_thirdparty_recipe.init()
    )

    supertokens_init(
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key,
        ),
        app_info=InputAppInfo(
            app_name="Minds",
            api_domain=website_domain,
            website_domain=website_domain,
            api_base_path="/auth",
            website_base_path="/auth",
        ),
        framework="fastapi",
        recipe_list=[
            st_session_recipe.init(),
            st_emailpassword_recipe.init(),
            thirdparty_recipe_init,
            st_emailverification_recipe.init(mode="REQUIRED"),
        ],
        mode="asgi",
    )
    logger.info("SuperTokens SDK initialized (providers=%d)", len(providers))


@app.function(
    secrets=[
        modal.Secret.from_name(f"cloudflare-{_DEPLOY_ENV}"),
        modal.Secret.from_name(f"supertokens-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ]
)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    _init_supertokens()
    return web_app
