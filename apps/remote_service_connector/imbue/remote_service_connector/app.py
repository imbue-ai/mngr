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
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any
from typing import NoReturn
from typing import Protocol
from uuid import UUID

import httpx
import modal
import ovh
import paramiko
import psycopg2
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from ovh.exceptions import APIError as OvhApiError
from ovh.exceptions import HTTPError as OvhHttpError
from ovh.exceptions import ResourceNotFoundError
from paramiko.hostkeys import HostKeyEntry
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from supertokens_python import InputAppInfo
from supertokens_python import SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.async_to_sync_wrapper import sync as _supertokens_sync_run
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe import emailpassword as st_emailpassword_recipe
from supertokens_python.recipe import emailverification as st_emailverification_recipe
from supertokens_python.recipe import session as st_session_recipe
from supertokens_python.recipe import thirdparty as st_thirdparty_recipe
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import PasswordPolicyViolationError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailpassword.syncio import consume_password_reset_token
from supertokens_python.recipe.emailpassword.syncio import send_reset_password_email
from supertokens_python.recipe.emailpassword.syncio import sign_in as ep_sign_in
from supertokens_python.recipe.emailpassword.syncio import sign_up as ep_sign_up
from supertokens_python.recipe.emailpassword.syncio import update_email_or_password
from supertokens_python.recipe.emailverification import EmailVerificationClaim
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.syncio import is_email_verified
from supertokens_python.recipe.emailverification.syncio import send_email_verification_email
from supertokens_python.recipe.emailverification.syncio import verify_email_using_token
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import create_new_session_without_request_response
from supertokens_python.recipe.session.syncio import get_session_without_request_response
from supertokens_python.recipe.session.syncio import refresh_session_without_request_response
from supertokens_python.recipe.session.syncio import revoke_all_sessions_for_user
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.recipe.thirdparty.syncio import get_provider
from supertokens_python.recipe.thirdparty.syncio import manually_create_or_update_user
from supertokens_python.syncio import get_user
from supertokens_python.syncio import list_users_by_account_info
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


class InvalidHostNameError(ValueError):
    """Raised when a host_name fails the SafeName regex on the lease request."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(f"host_name must be alphanumeric (with dashes/underscores allowed in the middle): {value!r}")


class InvalidPaidListEntryError(ValueError):
    """Raised when a paid-list domain or email entry is malformed."""

    def __init__(self, value: object, reason: str) -> None:
        self.value = value
        super().__init__(f"Invalid paid-list entry {value!r}: {reason}")


class InvalidR2BucketNameError(ValueError):
    """Raised when a derived R2 bucket name violates Cloudflare's naming rules."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(
            f"R2 bucket name must be 3-63 lowercase alphanumeric/hyphen chars with no leading/trailing hyphen: {value!r}"
        )


class InvalidR2AccessError(ValueError):
    """Raised when a key access scope is neither 'read' nor 'readwrite'."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(f"access must be 'read' or 'readwrite', got {value!r}")


class R2BucketExistsError(RuntimeError):
    """Raised when creating a bucket whose derived name already exists for the user."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket already exists: {bucket_name}")


class R2BucketNotFoundError(KeyError):
    """Raised when a bucket the caller referenced does not exist (or is not theirs)."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket not found: {bucket_name}")


class R2BucketNotEmptyError(RuntimeError):
    """Raised when destroying a bucket that still has objects in it."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        super().__init__(f"Bucket is not empty: {bucket_name}. Empty it before destroying.")


class R2BucketOwnershipError(PermissionError):
    """Raised when a bucket name does not carry the caller's ownership prefix."""

    def __init__(self, bucket_name: str, username: str) -> None:
        self.bucket_name = bucket_name
        self.username = username
        super().__init__(f"User '{username}' does not own bucket '{bucket_name}'")


class R2BucketLimitError(RuntimeError):
    """Raised when an account is already at the per-account bucket cap."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Account is at the maximum of {limit} buckets; destroy one before creating another.")


class PoolHostCleanupError(RuntimeError):
    """Raised when a pool-host release/teardown cannot complete its OVH cleanup.

    Surfaced (rather than swallowed to a warning) so a release that fails to
    actually cancel the VPS reports failure instead of a false success.
    """


class MissingAuthWebsiteDomainError(RuntimeError):
    """Raised when the required AUTH_WEBSITE_DOMAIN secret is not set."""


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
    # Verified email associated with the SuperTokens user, looked up at auth
    # time so that paid-feature endpoints (host pool, LiteLLM keys) can gate
    # access against the ``paid_emails`` / ``paid_domains`` tables. ``None``
    # when the SuperTokens user record has no email or when the lookup
    # failed -- in that case the paid-feature gate denies access.
    email: str | None = None


class AgentAuth(BaseModel):
    tunnel_id: str
    tunnel_name: str


AuthResult = AdminAuth | AgentAuth


# -- Host pool models --

# Mirror of mngr's SafeName regex (libs/mngr/imbue/mngr/primitives.py:_SAFE_NAME_RE).
# Duplicated here -- not imported -- because this file is self-contained and
# must not depend on the monorepo. Keep this in sync if the mngr-side rule
# changes (alphanumeric, dashes/underscores allowed in the middle only).
_HOST_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


def _validate_host_name(value: str) -> str:
    """Field validator: enforce the SafeName regex.

    Rejects empty strings and anything outside the alphanumeric+``-``/``_``
    middle-allowed shape so the connector cannot persist a host_name that
    mngr's ``HostName`` would refuse on the client side.
    """
    stripped = value.strip() if isinstance(value, str) else value
    if not isinstance(stripped, str) or not _HOST_NAME_RE.match(stripped):
        raise InvalidHostNameError(value)
    return stripped


class LeaseHostRequest(BaseModel):
    ssh_public_key: str = Field(description="SSH public key to authorize on the leased host")
    host_name: str = Field(
        description=(
            "User-chosen friendly name for the leased host. Must satisfy mngr's SafeName "
            "regex (alphanumeric, dashes/underscores allowed in the middle). Required."
        )
    )
    attributes: dict[str, Any] = Field(
        description=(
            "Lease-attribute filter. Matches with PostgreSQL '@>' so only fields the request "
            "explicitly sets are constrained; missing fields are unconstrained. Required."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "Hard region requirement (OVH datacenter code, e.g. 'US-EAST-VA'). When set, only "
            "hosts whose region column equals this value are eligible; if none is available the "
            "lease fails. Leave unset to be region-agnostic."
        ),
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class LeaseHostResponse(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description=(
            "SSH-reachable VPS address. Either a public IPv4 or a DNS hostname depending "
            "on what the host's provider returned at bake time (OVH-backed rows are DNS "
            "hostnames like ``vps-eec8860b.vps.ovh.us``)."
        )
    )
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned mngr agent ID")
    host_id: str = Field(description="Host ID in the mngr provider")
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(description="Attributes the row was matched against")
    outer_host_public_key: str = Field(
        description="The VPS/VM-root sshd host public key (port ssh_port), for strict host-key pinning"
    )
    container_host_public_key: str = Field(
        description="The docker container sshd host public key (port container_ssh_port), for strict host-key pinning"
    )


class ReleaseHostResponse(BaseModel):
    status: str = Field(
        description="Release status: 'released' on first call, 'already_released' on idempotent retries"
    )


class LeasedHostInfo(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description=(
            "SSH-reachable VPS address. Either a public IPv4 or a DNS hostname depending "
            "on what the host's provider returned at bake time (OVH-backed rows are DNS "
            "hostnames like ``vps-eec8860b.vps.ovh.us``)."
        )
    )
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned mngr agent ID")
    host_id: str = Field(description="Host ID in the mngr provider")
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(description="Attributes attached to the lease row")
    leased_at: str = Field(description="ISO 8601 timestamp when the host was leased")
    outer_host_public_key: str | None = Field(
        default=None, description="The VPS/VM-root sshd host public key, for strict host-key pinning"
    )
    container_host_public_key: str | None = Field(
        default=None, description="The docker container sshd host public key, for strict host-key pinning"
    )


# -- LiteLLM key management models --


class CreateKeyRequest(BaseModel):
    key_alias: str | None = Field(default=None, description="Optional human-readable alias for the key")
    max_budget: float | None = Field(default=None, description="Optional max budget in USD (no limit if unset)")
    budget_duration: str | None = Field(
        default=None, description="Optional budget reset duration (e.g. '1d', '1h', '1w', '1M')"
    )
    metadata: dict[str, str] | None = Field(
        default=None, description="Optional metadata (e.g. agent_id, host_id) for resource tracking"
    )


class CreateKeyResponse(BaseModel):
    key: str = Field(description="The generated LiteLLM virtual key")
    base_url: str = Field(description="The LiteLLM proxy base URL for ANTHROPIC_BASE_URL")


class KeyInfo(BaseModel):
    token: str = Field(description="Hashed key token identifier")
    key_alias: str | None = Field(default=None, description="Human-readable alias")
    key_name: str | None = Field(default=None, description="Key name")
    spend: float = Field(default=0.0, description="Total spend in USD")
    max_budget: float | None = Field(default=None, description="Max budget in USD")
    budget_duration: str | None = Field(default=None, description="Budget reset duration")
    user_id: str | None = Field(default=None, description="User ID the key belongs to")


class UpdateBudgetRequest(BaseModel):
    max_budget: float | None = Field(default=None, description="New max budget in USD (null to remove limit)")
    budget_duration: str | None = Field(default=None, description="New budget reset duration (null to remove)")


class DeleteKeyResponse(BaseModel):
    status: str = Field(description="Deletion status")


# -- R2 bucket models --

_R2_ACCESS_VALUES = ("read", "readwrite")


def _validate_r2_access(value: str) -> str:
    """Field validator: constrain the per-key access scope to read/readwrite."""
    if value not in _R2_ACCESS_VALUES:
        raise InvalidR2AccessError(value)
    return value


class CreateBucketRequest(BaseModel):
    name: str = Field(description="User's short bucket name (the server prefixes it with the owner id)")
    access: str = Field(default="readwrite", description="Access scope for the default key: 'read' or 'readwrite'")

    _validate_access = field_validator("access")(_validate_r2_access)


class BucketInfo(BaseModel):
    bucket_name: str = Field(description="Full R2 bucket name (<user_id_prefix>--<slug>)")
    s3_endpoint: str = Field(description="S3-compatible endpoint for this account")


class R2KeyMaterial(BaseModel):
    access_key_id: str = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    secret_access_key: str = Field(description="S3 Secret Access Key (sha256 of the token value); shown once")
    s3_endpoint: str = Field(description="S3-compatible endpoint for this account")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: str = Field(description="Access scope: 'read' or 'readwrite'")


class CreateBucketResponse(BaseModel):
    bucket: BucketInfo = Field(description="The created bucket")
    key: R2KeyMaterial = Field(description="The default key minted alongside the bucket")


class CreateR2KeyRequest(BaseModel):
    alias: str | None = Field(default=None, description="Optional human-readable alias for the key")
    access: str = Field(default="readwrite", description="Access scope: 'read' or 'readwrite'")

    _validate_access = field_validator("access")(_validate_r2_access)


class R2KeyInfo(BaseModel):
    access_key_id: str = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: str = Field(description="Access scope: 'read' or 'readwrite'")
    alias: str | None = Field(default=None, description="Human-readable alias")
    created_at: str = Field(description="ISO 8601 timestamp when the key was created")


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


# Env var the deployed connector reads at startup to identify which
# minds env it belongs to. The value is pushed by ``minds env deploy``
# into the per-tier ``litellm-connector-<tier>`` Modal Secret. For
# dev-tier deploys this is the per-developer dev env name (e.g.
# ``josh-3``); for tier deploys it's the tier itself (``staging`` /
# ``production``). Used to tag every Cloudflare tunnel the connector
# creates so the destroy-side can enumerate + delete only the tunnels
# belonging to a specific minds env -- without it, deleting tunnels
# would have to walk every tunnel on the dev-tier CF account
# (potentially clobbering other devs' tunnels).
_MINDS_ENV_NAME_VAR = "MINDS_ENV_NAME"


def _current_minds_env_name() -> str:
    """Return the value of ``MINDS_ENV_NAME`` or empty string.

    Empty when the deploy didn't push one (e.g. a pre-this-branch
    deploy). Callers must treat the empty case as "no env tag" -- the
    tunnel will still be creatable, just without env-aware destroy
    cleanup metadata.
    """
    return os.environ.get(_MINDS_ENV_NAME_VAR, "")


def cf_create_tunnel(client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    """Create a Cloudflare tunnel + tag it with the minds env name in metadata.

    The ``metadata`` field on ``cfd_tunnel`` POST accepts arbitrary
    string-keyed values; we shove ``{"env": "<minds-env-name>"}`` in so
    ``minds env destroy`` can later filter the tier's tunnels by env.
    Empty env_name still creates the tunnel (back-compat with older
    connector deploys); destroy then filters by exact match, so empty
    means "doesn't match any env" -- the operator can clean those up
    manually.
    """
    body: dict[str, Any] = {"name": name, "config_src": "cloudflare"}
    env_name = _current_minds_env_name()
    if env_name:
        body["metadata"] = {"env": env_name}
    response = client.post(f"/accounts/{account_id}/cfd_tunnel", json=body)
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


# --- R2 bucket + account-token operations ---


_R2_READ_PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Read"
_R2_WRITE_PERMISSION_GROUP_NAME = "Workers R2 Storage Bucket Item Write"


def _is_bucket_not_empty_error(exc: CloudflareApiError) -> bool:
    """Detect Cloudflare's 'bucket not empty' rejection from a delete error."""
    for err in exc.cf_errors:
        if "not empty" in str(err.get("message", "")).lower():
            return True
        if err.get("code") == 10040:
            return True
    return False


def cf_create_bucket(client: httpx.Client, account_id: str, name: str) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/r2/buckets", json={"name": name})
    return cf_check(response)["result"]


def cf_list_buckets(client: httpx.Client, account_id: str, name_contains: str = "") -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    cursor = ""
    is_more_pages = True
    while is_more_pages:
        params: dict[str, str] = {"per_page": "1000"}
        if name_contains:
            params["name_contains"] = name_contains
        if cursor:
            params["cursor"] = cursor
        response = client.get(f"/accounts/{account_id}/r2/buckets", params=params)
        data = cf_check(response)
        result = data["result"]
        buckets = result.get("buckets", []) if isinstance(result, dict) else result
        all_results.extend(buckets)
        result_info = data.get("result_info")
        cursor = result_info.get("cursor", "") if isinstance(result_info, dict) else ""
        is_more_pages = bool(cursor)
    return all_results


def cf_delete_bucket(client: httpx.Client, account_id: str, name: str) -> None:
    """Delete an R2 bucket. Raises R2BucketNotEmptyError / R2BucketNotFoundError on the matching CF errors."""
    response = client.delete(f"/accounts/{account_id}/r2/buckets/{name}")
    try:
        cf_check(response)
    except CloudflareApiError as exc:
        if exc.status_code == 404:
            raise R2BucketNotFoundError(name) from exc
        if _is_bucket_not_empty_error(exc):
            raise R2BucketNotEmptyError(name) from exc
        raise


def cf_list_token_permission_groups(client: httpx.Client, account_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/tokens/permission_groups")
    return cf_check(response)["result"]


def cf_create_account_token(
    client: httpx.Client, account_id: str, name: str, policies: list[dict[str, Any]]
) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/tokens", json={"name": name, "policies": policies})
    return cf_check(response)["result"]


def cf_delete_account_token(client: httpx.Client, account_id: str, token_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/tokens/{token_id}"))


def build_r2_bucket_token_policies(
    account_id: str, bucket_name: str, permission_group_id: str
) -> list[dict[str, Any]]:
    """Build the account-token policy list scoping a token to one R2 bucket.

    The resource key mirrors Cloudflare's R2 bucket resource identifier. The
    ``default`` segment is the (default) jurisdiction; revisit if non-default
    jurisdictions are ever exposed.
    """
    resource_key = f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket_name}"
    return [
        {
            "effect": "allow",
            "permission_groups": [{"id": permission_group_id}],
            "resources": {resource_key: "*"},
        }
    ]


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

    # R2 bucket + bucket-scoped-token operations. These are folded into the
    # CloudflareOps surface (rather than a parallel R2Ops abstraction) because
    # they are just more Cloudflare REST calls sharing the same authenticated
    # client + account_id; the genuinely-different concern (the key-metadata DB)
    # lives behind the separate KeyStore abstraction below.
    account_id: str

    def create_bucket(self, name: str) -> dict[str, Any]: ...
    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]: ...
    def delete_bucket(self, name: str) -> None: ...
    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]: ...
    def delete_bucket_token(self, token_id: str) -> None: ...


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
        # Per-container cache of R2 permission-group UUIDs, looked up lazily.
        # Looked up at runtime (not hard-coded) because the connector runs
        # against different Cloudflare accounts across deploy environments.
        self._r2_permission_group_id_by_access: dict[str, str] = {}

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

    def _r2_permission_group_id(self, access: str) -> str:
        if access not in self._r2_permission_group_id_by_access:
            wanted = _R2_WRITE_PERMISSION_GROUP_NAME if access == "readwrite" else _R2_READ_PERMISSION_GROUP_NAME
            groups = cf_list_token_permission_groups(self.client, self.account_id)
            for group in groups:
                if group.get("name") == wanted:
                    self._r2_permission_group_id_by_access[access] = group["id"]
                    break
            else:
                raise CloudflareApiError(500, [{"message": f"R2 permission group not found: {wanted}"}])
        return self._r2_permission_group_id_by_access[access]

    def create_bucket(self, name: str) -> dict[str, Any]:
        return cf_create_bucket(self.client, self.account_id, name)

    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]:
        return cf_list_buckets(self.client, self.account_id, name_contains=name_contains)

    def delete_bucket(self, name: str) -> None:
        cf_delete_bucket(self.client, self.account_id, name)

    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]:
        policies = build_r2_bucket_token_policies(self.account_id, bucket_name, self._r2_permission_group_id(access))
        return cf_create_account_token(self.client, self.account_id, token_name, policies)

    def delete_bucket_token(self, token_id: str) -> None:
        cf_delete_account_token(self.client, self.account_id, token_id)


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
        cname_target = f"{tid}.cfargotunnel.com"
        existing_dns = self.ops.list_dns_records(name=hostname)
        if not existing_dns:
            self.ops.create_cname(hostname, cname_target)
        elif existing_dns[0].get("content") != cname_target:
            raise CloudflareApiError(
                status_code=409,
                errors=[
                    {
                        "message": (
                            f"DNS record for {hostname} already exists pointing to "
                            f"{existing_dns[0].get('content')!r}, not {cname_target!r}"
                        )
                    }
                ],
            )
        else:
            # CNAME already points at this tunnel; idempotent re-add.
            pass
        config = self.ops.get_tunnel_config(tid)
        rules = [
            r for r in non_catchall_rules(config.get("config", {}).get("ingress", [])) if r.get("hostname") != hostname
        ]
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
        """Apply the tunnel's default auth policy to a new service, if one is set.

        Skipped when an Access Application already exists for the hostname:
        on a re-add the service may have a customized per-service policy from
        a prior :meth:`set_service_auth` call, and re-applying the tunnel
        default would clobber it.
        """
        try:
            raw = self.ops.kv_get(tunnel_name)
            if raw is None:
                return
            if self.ops.get_access_app_by_domain(hostname) is not None:
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


def _default_email_getter(
    user_id: str,
    user_getter: Callable[[str], Any] = get_user,
) -> str | None:
    """Return the first **verified** email registered for the given SuperTokens user_id.

    A SuperTokens user may have several login methods (email/password, OAuth
    providers) with independent ``verified`` flags. Only login methods whose
    ``verified`` flag is True are considered, since the paid-feature gate
    authorizes by domain ownership and that requires the email to actually
    have been verified. Returns the first matching email, or ``None`` if the
    user has no verified email.

    Only the SuperTokens SDK's typed errors (``SuperTokensSessionError``,
    ``SuperTokensGeneralError``) are caught and turned into ``None`` (with a
    warning log); any other exception (e.g. transport-level network errors
    that escape the SDK) is allowed to propagate, so that truly unexpected
    failures surface loudly rather than silently denying paid-feature access.

    ``user_getter`` is exposed for tests so they can drive each branch
    (``None`` user, missing emails, SDK exception) without monkeypatching the
    SuperTokens SDK; production callers should rely on the default.
    """
    try:
        user = user_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Failed to fetch SuperTokens user %s: %s", user_id[:8], exc)
        return None
    if user is None:
        return None
    for login_method in user.login_methods:
        if login_method.email and login_method.verified:
            return login_method.email
    return None


def _authenticate_supertokens(
    token: str,
    session_getter: Callable[..., Any] = get_session_without_request_response,
    email_getter: Callable[[str], str | None] = _default_email_getter,
) -> AdminAuth:
    """Validate a SuperTokens JWT access token. Returns AdminAuth with user_id_prefix as username."""
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        raise HTTPException(status_code=401, detail="SuperTokens not configured")

    try:
        # Pass ``override_global_claim_validators=lambda *_: []`` so the
        # session getter does NOT auto-reject unverified-email tokens at
        # the validator step. We want our own explicit
        # ``if not is_verified: raise "Email not verified"`` below to
        # fire instead, so the operator-facing error message tells the
        # user what to fix (the SDK's default rejection surfaces as a
        # generic ``SuperTokensSessionError`` → "Invalid token", which
        # is misleading).
        session = session_getter(
            access_token=token,
            anti_csrf_check=False,
            override_global_claim_validators=lambda *_args, **_kwargs: [],
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
    email = email_getter(user_id)

    return AdminAuth(username=user_id_prefix, email=email)


def _get_user_id_from_access_token(token: str) -> str:
    """Validate a SuperTokens JWT and return the full user_id (not just the prefix).

    Raises ``HTTPException(401)`` on any validation failure. Used by auth-proxy
    endpoints that need the full user_id to drive an API call (e.g. revoke).

    Does NOT enforce email-verification at this layer -- callers like
    ``/auth/session/revoke`` legitimately need to work for unverified
    users (signing out a session you never finished verifying should
    still succeed). The endpoints that DO want email-verified callers
    only go through :func:`_authenticate_supertokens` instead.
    """
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        raise HTTPException(status_code=401, detail="SuperTokens not configured")
    try:
        session = get_session_without_request_response(
            access_token=token,
            anti_csrf_check=False,
            override_global_claim_validators=lambda *_args, **_kwargs: [],
        )
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


# Env var holding the cache TTL (in seconds) for paid-status lookups. The
# paid gate consults two Neon tables (``paid_emails`` / ``paid_domains``)
# on every gated request; this in-memory cache bounds how often that DB
# round-trip happens per container. Set to ``0`` to disable caching
# entirely (every gated request hits the DB) -- useful in tests. Unset
# falls back to ``_DEFAULT_PAID_LIST_CACHE_TTL_SECONDS``. Each Modal
# container caches independently, so a CRUD change to the lists takes up
# to the TTL to be reflected everywhere.
_PAID_LIST_CACHE_TTL_ENV = "MINDS_PAID_LIST_CACHE_TTL_SECONDS"
_DEFAULT_PAID_LIST_CACHE_TTL_SECONDS = 60.0

# Process-local cache mapping a lowercased email -> (expiry_monotonic, is_paid).
# Guarded by a lock since uvicorn serves requests from a thread pool.
_paid_status_cache: dict[str, tuple[float, bool]] = {}
_paid_status_cache_lock = threading.Lock()


def clear_paid_status_cache() -> None:
    """Drop every cached paid-status entry (called after a CRUD write, and in tests)."""
    with _paid_status_cache_lock:
        _paid_status_cache.clear()


def _paid_list_cache_ttl_seconds() -> float:
    """Resolve the paid-status cache TTL from the environment.

    Falls back to the default on an unset/empty value and on an
    unparseable one (logging a warning in the latter case) so a typo'd
    Modal secret degrades to "cache normally" rather than crashing the
    gate.
    """
    raw = os.environ.get(_PAID_LIST_CACHE_TTL_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "Invalid %s=%r; falling back to %.0fs",
            _PAID_LIST_CACHE_TTL_ENV,
            raw,
            _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS,
        )
        return _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS


def _email_domain(email: str) -> str:
    """Return the lowercased domain (part after the last ``@``) of an email, or ``""``."""
    return email.strip().lower().rpartition("@")[2]


def is_email_paid_in_db(
    email: str,
    connection_factory: Callable[[], Any] | None = None,
) -> bool:
    """Return whether ``email`` is paid per the ``paid_emails`` / ``paid_domains`` tables.

    Paid when either an exact (lowercased) full-email match exists in
    ``paid_emails`` with ``is_paid = true``, OR the email's exact domain
    matches a ``paid_domains`` row with ``is_paid = true``. ``connection_factory``
    is injected so unit tests can supply an in-memory backend; it defaults
    to :func:`_get_pool_db_connection` (resolved lazily because that helper
    is defined further down this module).

    Raises ``psycopg2.Error`` on any database failure; the caller
    (:func:`require_paid_account`) converts that into a fail-closed 403.
    """
    factory = connection_factory if connection_factory is not None else _get_pool_db_connection
    email_lower = email.strip().lower()
    domain = _email_domain(email_lower)
    conn = factory()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM paid_emails WHERE email = %s AND is_paid = TRUE", (email_lower,))
            if cur.fetchone() is not None:
                return True
            if domain:
                cur.execute("SELECT 1 FROM paid_domains WHERE domain = %s AND is_paid = TRUE", (domain,))
                if cur.fetchone() is not None:
                    return True
        return False
    finally:
        conn.close()


def is_email_paid(
    email: str,
    db_lookup: Callable[[str], bool] = is_email_paid_in_db,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Cached wrapper around :func:`is_email_paid_in_db`.

    Honors the ``MINDS_PAID_LIST_CACHE_TTL_SECONDS`` TTL: a non-positive
    TTL bypasses the cache entirely, otherwise both positive and negative
    results are cached for the TTL window. ``db_lookup`` / ``monotonic``
    are injected for tests.
    """
    email_lower = email.strip().lower()
    ttl_seconds = _paid_list_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return db_lookup(email_lower)
    now = monotonic()
    with _paid_status_cache_lock:
        cached = _paid_status_cache.get(email_lower)
        if cached is not None and cached[0] > now:
            return cached[1]
    is_paid = db_lookup(email_lower)
    with _paid_status_cache_lock:
        _paid_status_cache[email_lower] = (now + ttl_seconds, is_paid)
    return is_paid


def require_paid_account(
    auth: AdminAuth,
    paid_checker: Callable[[str], bool] = is_email_paid,
) -> None:
    """Gate paid features on the caller's email appearing in the paid lists.

    Raises ``HTTPException(403)`` when the caller has no verified email,
    when their email is not in the ``paid_emails`` / ``paid_domains``
    tables, or when the database lookup fails (fail closed). ``/tunnels/*``
    (Cloudflare forwarding) intentionally does NOT call this gate --
    email-verified accounts can still use forwarding regardless.
    ``paid_checker`` is injected for tests; production callers use the
    cached, table-backed default.
    """
    if not auth.email:
        raise HTTPException(
            status_code=403,
            detail="Account email unavailable; cannot authorize paid feature access",
        )
    try:
        is_paid = paid_checker(auth.email)
    except psycopg2.Error as exc:
        logger.warning("Paid-status lookup failed for %s: %s", auth.email, exc)
        raise HTTPException(
            status_code=403,
            detail="Could not verify paid-feature access (database error); please try again",
        ) from exc
    if not is_paid:
        raise HTTPException(
            status_code=403,
            detail="Account is not authorized for paid features",
        )


# Env var holding the single fixed API key that authenticates the paid-list
# CRUD endpoints (``/paid/*``). Distinct from the SuperTokens / tunnel-token
# auth used by every other route: those routes reject this key, and the
# ``/paid/*`` routes reject SuperTokens JWTs / tunnel tokens. Folded into the
# ``supertokens-<env>`` Modal secret (see .minds/template/supertokens.sh).
_PAID_ADMIN_KEY_ENV = "MINDS_PAID_ADMIN_KEY"


def require_paid_admin_key(request: Request) -> None:
    """Authenticate a paid-list CRUD request against the fixed admin API key.

    Expects ``Authorization: Bearer <MINDS_PAID_ADMIN_KEY>`` and compares
    in constant time. Raises ``HTTPException(403)`` when the server has no
    key configured (the paid-list admin API is disabled), and
    ``HTTPException(401)`` when credentials are missing or wrong.
    """
    expected = os.environ.get(_PAID_ADMIN_KEY_ENV, "")
    if not expected:
        raise HTTPException(status_code=403, detail="Paid-list admin API is not enabled on this server")
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    provided = auth_header[len("bearer ") :]
    # Compare over UTF-8 bytes: hmac.compare_digest raises TypeError on str
    # operands containing non-ASCII characters, and HTTP header values can
    # legitimately carry non-ASCII bytes. Encoding keeps the comparison both
    # total (a malformed key cleanly yields 401, not a 500) and constant-time.
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid paid-list admin API key")


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
    if isinstance(exc, PoolHostCleanupError):
        # A release that could not finish its teardown -- surface as a server
        # error so the client retries rather than treating the lease as gone.
        logger.error("Pool host cleanup error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if isinstance(exc, (OvhApiError, OvhHttpError)):
        # OVH calls during teardown (tag strip / cancel) failed. Surface as a
        # bad-gateway so the failed cancel is visible and retryable instead of
        # being swallowed into a false "released" success.
        logger.error("OVH API error during pool-host teardown: %s", exc)
        raise HTTPException(status_code=502, detail=f"OVH API error during teardown: {exc}") from exc
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
    if isinstance(exc, InvalidPaidListEntryError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, InvalidR2BucketNameError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, R2BucketOwnershipError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, R2BucketNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, R2BucketNotEmptyError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, R2BucketExistsError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, R2BucketLimitError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.error("Unexpected error in endpoint handler", exc_info=exc)
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
# Host pool helpers
# ---------------------------------------------------------------------------


def _get_pool_db_connection() -> Any:
    """Open a psycopg2 connection to the Neon pool database."""
    database_url = os.environ["DATABASE_URL"]
    return psycopg2.connect(database_url)


def _pin_expected_host_key(client: paramiko.SSHClient, host: str, port: int, expected_host_public_key: str) -> None:
    """Pin ``expected_host_public_key`` for ``host:port`` and reject any other host key.

    paramiko keys non-default ports under the ``[host]:port`` known-hosts name, so
    a container/forwarded port must be pinned under that bracketed name to match
    what ``connect`` looks up. Replaces trust-on-first-use: a mismatched or
    unknown host key is rejected.
    """
    known_hosts_name = host if port == 22 else f"[{host}]:{port}"
    entry = HostKeyEntry.from_line(f"{known_hosts_name} {expected_host_public_key.strip()}")
    if entry is None or entry.key is None:
        # An SSHException (not PoolHostCleanupError) so this is handled uniformly by
        # every caller: the teardown sweep and reconcile already treat it as an SSH
        # failure, and the lease path's `except (paramiko.SSHException, OSError)`
        # maps it to a 502 (SSH key injection failed) rather than a misleading 500.
        raise paramiko.SSHException(
            f"could not parse expected host key for {known_hosts_name}: {expected_host_public_key!r}"
        )
    client.get_host_keys().add(known_hosts_name, entry.key.get_name(), entry.key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


@contextlib.contextmanager
def _management_ssh_client(
    host: str,
    port: int,
    user: str,
    management_key_pem: str,
    timeout_seconds: float,
    expected_host_public_key: str,
) -> Iterator[paramiko.SSHClient]:
    """Yield an SSHClient connected to ``host`` with the pool management key, closed on exit.

    The host is authenticated against ``expected_host_public_key`` (strict pinning,
    no trust-on-first-use); callers fail closed when no pinned key is available.
    """
    private_key = paramiko.Ed25519Key.from_private_key(io.StringIO(management_key_pem))
    client = paramiko.SSHClient()
    _pin_expected_host_key(client, host, port, expected_host_public_key)
    try:
        client.connect(hostname=host, port=port, username=user, pkey=private_key, timeout=timeout_seconds)
        yield client
    finally:
        client.close()


def _append_authorized_key(
    host: str,
    port: int,
    user: str,
    management_key_pem: str,
    public_key_to_add: str,
    expected_host_public_key: str,
) -> None:
    """SSH into a host using the management key and append a public key to authorized_keys."""
    with _management_ssh_client(
        host, port, user, management_key_pem, timeout_seconds=15, expected_host_public_key=expected_host_public_key
    ) as client:
        key_line = public_key_to_add.strip()
        commands = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {} >> ~/.ssh/authorized_keys && ".format(
                shlex.quote(key_line)
            )
            + "chmod 600 ~/.ssh/authorized_keys"
        )
        _stdin, _stdout, stderr = client.exec_command(commands)
        exit_status = _stdout.channel.recv_exit_status()
        if exit_status != 0:
            stderr_text = stderr.read().decode()
            raise paramiko.SSHException(f"SSH command failed (exit {exit_status}): {stderr_text}")


# ---------------------------------------------------------------------------
# OVH pool-host cleanup
#
# Releasing a pool host (and the periodic sweep that mops up interrupted
# releases) must (a) strip the per-lease OVH IAM tags so the VPS reads as a
# clean, recyclable host and (b) cancel the VPS in OVH so it stops renewing.
# We do this with direct OVH REST calls rather than running ``mngr`` here so
# the connector image stays light; the call surface is intentionally tiny.
# Keep the tag keys / endpoint defaults in sync with ``libs/mngr_ovh``.
# ---------------------------------------------------------------------------

# Always kept on a recyclable host so the OVH provider can still discover it.
OVH_PROVIDER_TAG_KEY = "mngr-provider"
# Per-lease tags stripped on cleanup (everything except the provider tag).
_OVH_STALE_TAG_KEYS: tuple[str, ...] = ("minds_env", "mngr-host-id")
_OVH_DEFAULT_ENDPOINT = "ovh-us"


class OvhVpsResource(BaseModel):
    """A single OVH IAM ``vps`` resource with its tags."""

    urn: str = Field(description="IAM URN like urn:v1:us:resource:vps:<serviceName>")
    name: str = Field(description="OVH VPS service name")
    tags: dict[str, str] = Field(default_factory=dict, description="IAM resource tags")


class OvhOps(Protocol):
    """Abstraction over the few OVH REST calls the cleanup path needs."""

    def delete_tag(self, urn: str, key: str) -> None: ...
    def set_delete_at_expiration(self, service_name: str, delete_at_expiration: bool) -> None: ...
    def list_vps_resources(self) -> list[OvhVpsResource]: ...


class OvhClientCaller(Protocol):
    """The single python-ovh entrypoint HttpOvhOps depends on (a DI seam for tests)."""

    def call(self, method: str, path: str, data: object, need_auth: bool) -> Any: ...


class HttpOvhOps:
    """OvhOps implementation backed by the official ``ovh`` SDK (signed calls)."""

    def __init__(self, application_key: str, application_secret: str, consumer_key: str, endpoint: str) -> None:
        self.client: OvhClientCaller = ovh.Client(
            endpoint=endpoint,
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
        )

    def delete_tag(self, urn: str, key: str) -> None:
        # Idempotent: a missing tag means the strip already happened.
        try:
            self.client.call("DELETE", f"/v2/iam/resource/{urn}/tag/{key}", None, True)
        except ResourceNotFoundError:
            pass

    def set_delete_at_expiration(self, service_name: str, delete_at_expiration: bool) -> None:
        # Read-modify-write so we don't clobber unrelated serviceInfos fields.
        # Idempotent: a missing service means OVH already removed the VPS, so
        # there is nothing left to cancel (treat as success, like delete_tag).
        try:
            info = dict(self.client.call("GET", f"/vps/{service_name}/serviceInfos", None, True) or {})
            renew = dict(info.get("renew") or {})
            renew["deleteAtExpiration"] = delete_at_expiration
            info["renew"] = renew
            self.client.call("PUT", f"/vps/{service_name}/serviceInfos", info, True)
        except ResourceNotFoundError:
            pass

    def list_vps_resources(self) -> list[OvhVpsResource]:
        payload = self.client.call("GET", "/v2/iam/resource?resourceType=vps", None, True)
        if not isinstance(payload, list):
            return []
        resources: list[OvhVpsResource] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            urn = str(raw.get("urn") or "")
            if not urn:
                continue
            tags = {str(k): str(v) for k, v in (raw.get("tags") or {}).items()}
            resources.append(OvhVpsResource(urn=urn, name=str(raw.get("name") or ""), tags=tags))
        return resources


def ovh_region_code_for_endpoint(endpoint: str) -> str:
    """Map an OVH endpoint id (``ovh-us``) to the URN region segment (``us``)."""
    if endpoint.startswith("ovh-"):
        return endpoint.removeprefix("ovh-")
    return "us"


def _get_ovh_endpoint() -> str:
    return os.environ.get("OVH_ENDPOINT", _OVH_DEFAULT_ENDPOINT)


def vps_urn_for(service_name: str, region_code: str) -> str:
    """Build the IAM resource URN for an OVH VPS owned by this account."""
    return f"urn:v1:{region_code}:resource:vps:{service_name}"


@functools.cache
def _get_ovh_ops() -> OvhOps:
    return HttpOvhOps(
        application_key=os.environ["OVH_APPLICATION_KEY"],
        application_secret=os.environ["OVH_APPLICATION_SECRET"],
        consumer_key=os.environ["OVH_CONSUMER_KEY"],
        endpoint=_get_ovh_endpoint(),
    )


def clean_up_pool_host_in_ovh(ovh_ops: OvhOps, vps_instance_id: str, region_code: str) -> None:
    """Strip the per-lease tags (keeping ``mngr-provider``) then cancel the VPS.

    Tags are stripped first (per the cleanup contract) so a mid-crash leaves a
    recyclable-looking host that the next sweep finishes cancelling. Each call
    is idempotent, so re-running is safe.
    """
    urn = vps_urn_for(vps_instance_id, region_code)
    for tag_key in _OVH_STALE_TAG_KEYS:
        ovh_ops.delete_tag(urn, tag_key)
    ovh_ops.set_delete_at_expiration(vps_instance_id, True)


def _delete_pool_host_row(conn: Any, host_db_id: Any) -> None:
    """Delete a single pool_hosts row by id (committing immediately)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(host_db_id),))
    conn.commit()


# pool_hosts.backend_kind values (kept in sync with migration 009 and the
# mngr_imbue_cloud primitives). A real OVH VPS is cancelled in OVH on release;
# a "slice" is a lima VM on one of our bare-metal boxes and is destroyed by
# SSHing the box and running limactl.
BACKEND_KIND_OVH_VPS = "ovh_vps"
BACKEND_KIND_SLICE = "slice"


def build_slice_teardown_commands(lima_instance_name: str, lima_disk_name: str | None) -> tuple[str, ...]:
    """Commands to run on the bare-metal box to destroy a slice's lima VM + data disk."""
    commands = [f"limactl delete --force {shlex.quote(lima_instance_name)}"]
    if lima_disk_name:
        commands.append(f"limactl disk delete --force {shlex.quote(lima_disk_name)}")
    return tuple(commands)


def _run_ssh_commands_on_box(
    host: str, port: int, user: str, management_key_pem: str, commands: tuple[str, ...], box_host_public_key: str
) -> None:
    """SSH into the box with the pool management key and run each command, raising on failure."""
    with _management_ssh_client(
        host, port, user, management_key_pem, timeout_seconds=30, expected_host_public_key=box_host_public_key
    ) as client:
        for command in commands:
            _stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                stderr_text = stderr.read().decode()
                raise PoolHostCleanupError(
                    f"slice teardown command {command!r} failed (exit {exit_status}): {stderr_text}"
                )


def clean_up_slice_on_box(
    conn: Any,
    host_db_id: Any,
    bare_metal_server_id: Any,
    lima_instance_name: str | None,
    lima_disk_name: str | None,
) -> None:
    """Destroy a slice's lima VM (and data disk) on its owning bare-metal box.

    Looks up the box's address + lima service user from ``bare_metal_servers``,
    then SSHes in with the pool management key and runs limactl. Raises
    ``PoolHostCleanupError`` if the slice's bookkeeping is incomplete or the box
    can't be reached, so the row stays ``removing`` and the sweep retries (the
    slot is only freed once the VM is really gone).
    """
    if not (bare_metal_server_id and lima_instance_name):
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id} is missing bare_metal_server_id or lima_instance_name; cannot tear down its VM"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_address, lima_service_user, box_host_public_key FROM bare_metal_servers WHERE id = %s",
            (str(bare_metal_server_id),),
        )
        server_row = cur.fetchone()
    if server_row is None or not server_row[0]:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} is missing or has no public_address"
        )
    box_address, lima_service_user, box_host_public_key = server_row[0], server_row[1] or "root", server_row[2]
    # Fail closed: without the box's pinned host key we cannot reach it without
    # trust-on-first-use. The row stays ``removing`` and the sweep retries once
    # the one-time keyscan backfill has populated the column.
    if not box_host_public_key:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} has no box_host_public_key "
            "(run the one-time `mngr imbue_cloud admin` host-key backfill)"
        )
    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
    commands = build_slice_teardown_commands(lima_instance_name, lima_disk_name)
    _run_ssh_commands_on_box(box_address, 22, lima_service_user, management_key_pem, commands, box_host_public_key)


# Slice lima resources are named ``mngr-slice-<env>-<host-hex>`` (the data disk
# adds a ``-data`` suffix). The host hex is a hyphen-free uuid, so the env stamp is
# everything between the prefix and the trailing ``-<host-hex>``. Mirrors
# ``mngr_imbue_cloud.slices.bare_metal`` (the connector has no dependency on it).
_SLICE_LIMA_PREFIX = "mngr-slice-"
_SLICE_LIMA_DISK_SUFFIX = "-data"
_STAMPED_SLICE_CORE_RE = re.compile(r"^(?P<env>.+)-(?P<host>[0-9a-f]{32})$")
# Non-login SSH may not source the lima user's profile, so set PATH explicitly
# (limactl is extracted to /usr/local/bin by box prep).
_BOX_LIMACTL_PATH_PREFIX = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH"


def slice_name_env_owner(name: str) -> str | None:
    """The env a slice instance/disk name is stamped for, or None if legacy/foreign/not-a-slice."""
    if not name.startswith(_SLICE_LIMA_PREFIX):
        return None
    core = name[len(_SLICE_LIMA_PREFIX) :]
    if core.endswith(_SLICE_LIMA_DISK_SUFFIX):
        core = core[: -len(_SLICE_LIMA_DISK_SUFFIX)]
    match = _STAMPED_SLICE_CORE_RE.match(core)
    return match.group("env") if match else None


def _list_box_lima_names(
    host: str, user: str, management_key_pem: str, json_subcommand: str, box_host_public_key: str
) -> set[str]:
    """SSH the box and return the ``name`` of every lima instance or disk (per ``json_subcommand``).

    ``json_subcommand`` is ``list --json`` (instances) or ``disk list --json`` (disks);
    both emit one JSON object per line. Raises ``PoolHostCleanupError`` on a non-zero exit
    so the caller skips this box rather than mistaking an SSH failure for "no VMs".
    """
    names: set[str] = set()
    command = f"{_BOX_LIMACTL_PATH_PREFIX} limactl {json_subcommand}"
    with _management_ssh_client(
        host, 22, user, management_key_pem, timeout_seconds=30, expected_host_public_key=box_host_public_key
    ) as client:
        _stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        if exit_status != 0:
            raise PoolHostCleanupError(
                f"`limactl {json_subcommand}` on {host} failed (exit {exit_status}): {stderr.read().decode()}"
            )
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable lima JSON line on %s: %r", host, stripped)
            continue
        name = parsed.get("name")
        if name:
            names.add(name)
    return names


def reconcile_slice_boxes(conn: Any, env_name: str) -> int:
    """Audit each box's lima slices against the DB, scoped to ``env_name``'s stamped slices.

    Returns the number of divergences found (and logged). Alert-only by design: for
    every bare-metal box it logs, at error level,

    * a slice stamped for ``env_name`` present on the box with no pool_hosts row, and
    * a pool_hosts row whose VM is absent from the box.

    It deliberately does NOT auto-delete. A row-less stamped slice is most often a
    bake mid-flight (the carve creates the instance ~10-30 min before it inserts the
    row), and this cron runs on a fixed hourly schedule independent of bakes -- so
    auto-reaping here would race a live bake and destroy its slice. Actual reaping is
    left to the bake-time reaper (which runs in the bake's own ``finally``, where the
    in-flight set is known). If a box's lima resources cannot be listed, this raises:
    a box we could not inspect was NOT reconciled, and that failure must surface
    rather than be mistaken for a clean audit. Other envs' slices and legacy
    un-stamped slices are never inspected, so this is safe on a shared box.
    """
    if not env_name:
        logger.info("Slice reconcile skipped: connector has no MINDS_ENV_NAME to scope to")
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT id, public_address, lima_service_user, box_host_public_key FROM bare_metal_servers")
        servers = cur.fetchall()
    # Read the pool key only once we know there are boxes to inspect: a deployment
    # with no slice infrastructure (no boxes, no POOL_SSH_PRIVATE_KEY) must not fail
    # here just because the cron also covers the OVH pool-host cleanup.
    if not servers:
        return 0
    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
    divergence_count = 0
    for server_id, public_address, lima_service_user, box_host_public_key in servers:
        if not public_address:
            continue
        # Fail closed on a box with no pinned host key: skipping it would look like
        # a clean audit, so surface it loudly instead. Cleared once the one-time
        # keyscan backfill populates the column.
        if not box_host_public_key:
            logger.error(
                "Slice reconcile skipped box %s: no box_host_public_key (run the one-time host-key backfill)",
                public_address,
            )
            divergence_count += 1
            continue
        user = lima_service_user or "root"
        # If we cannot list a box's lima resources we did NOT reconcile it; let the
        # failure propagate rather than silently skipping (which would look like a
        # clean audit and could mask vanished/leaked VMs).
        box_instances = _list_box_lima_names(
            public_address, user, management_key_pem, "list --json", box_host_public_key
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lima_instance_name FROM pool_hosts WHERE backend_kind = %s AND bare_metal_server_id = %s",
                (BACKEND_KIND_SLICE, str(server_id)),
            )
            tracked_instances = {row[0] for row in cur.fetchall() if row[0]}

        # This env's stamped slices on the box with no DB row (often a bake mid-flight).
        untracked = {
            name for name in box_instances if slice_name_env_owner(name) == env_name and name not in tracked_instances
        }
        for instance_name in sorted(untracked):
            divergence_count += 1
            logger.error(
                "Slice reconcile divergence on %s: stamped slice %s has no pool_hosts row "
                "(in-flight bake, or an orphan for the bake-time reaper)",
                public_address,
                instance_name,
            )
        # A DB row whose VM is gone is the other divergence direction.
        for missing_instance in sorted(tracked_instances - box_instances):
            divergence_count += 1
            logger.error(
                "Slice reconcile divergence on %s: pool_hosts row for %s has no VM on the box (needs manual rebake/cleanup)",
                public_address,
                missing_instance,
            )
    return divergence_count


def run_pool_host_cleanup_sweep(conn: Any, ovh_ops: OvhOps, region_code: str) -> tuple[int, int]:
    """Clean up every ``removing`` pool host: strip tags, cancel, delete the row.

    Returns ``(success_count, failure_count)``. Per-host failures are logged
    and skipped (the row stays ``removing`` for the next run); ``FOR UPDATE
    SKIP LOCKED`` keeps a concurrent inline release and the sweep from
    double-processing the same row.
    """
    success_count = 0
    failure_count = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, vps_instance_id, backend_kind, lima_instance_name, lima_disk_name, bare_metal_server_id "
            "FROM pool_hosts WHERE status = 'removing' FOR UPDATE SKIP LOCKED"
        )
        rows = cur.fetchall()
        for (
            host_db_id,
            vps_instance_id,
            backend_kind,
            lima_instance_name,
            lima_disk_name,
            bare_metal_server_id,
        ) in rows:
            # Per-host savepoint so a DB error on one host's DELETE doesn't
            # abort the whole transaction (which would roll back every other
            # host's already-issued DELETE in this run and poison subsequent
            # statements). Rollback-to-savepoint leaves the transaction usable.
            cur.execute("SAVEPOINT pool_host_cleanup")
            try:
                # Branch on backend: slices are torn down on their box via
                # limactl; real VPSes are cancelled in OVH. A slice whose VM
                # isn't destroyed must NOT have its row deleted (that would leak
                # the VM and the slot), so clean_up_slice_on_box raises on any
                # problem and the row stays ``removing`` for the next run.
                if backend_kind == BACKEND_KIND_SLICE:
                    clean_up_slice_on_box(conn, host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name)
                elif vps_instance_id:
                    clean_up_pool_host_in_ovh(ovh_ops, vps_instance_id, region_code)
                else:
                    logger.warning("Removing pool host %s has no vps_instance_id; skipping OVH cleanup", host_db_id)
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(host_db_id),))
                cur.execute("RELEASE SAVEPOINT pool_host_cleanup")
                success_count += 1
            except (
                OvhApiError,
                OvhHttpError,
                psycopg2.Error,
                PoolHostCleanupError,
                paramiko.SSHException,
                OSError,
            ) as exc:
                cur.execute("ROLLBACK TO SAVEPOINT pool_host_cleanup")
                logger.warning("Cleanup failed for removing pool host %s; will retry next run: %s", host_db_id, exc)
                failure_count += 1
    conn.commit()
    return success_count, failure_count


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

web_app = FastAPI()


# Public env var name the deployed connector reads at startup to expose
# the tier's generation id via ``GET /generation``. The id is minted by
# ``minds env deploy`` and stored in HCP Vault at
# ``secrets/minds/<tier>/generation``; the per-tier ``litellm-connector-<tier>``
# Modal Secret carries it into the container. See
# ``apps/minds/imbue/minds/envs/generation.py`` for the full lifecycle.
# An empty string is the **steady state** for any tier whose
# ``deploy.toml`` has ``[lifecycle].tracks_generation = false`` (dev tier
# today) -- ``deploy_env`` only mints + pushes a generation id when the
# flag is true, so the connector sees no value and ``/generation``
# answers ``{"generation_id": ""}``. The activate-time auto-wipe in
# ``minds env activate`` skips the wipe on empty, which is the right
# no-op for untracked tiers. (Empty is also what an older pre-generation-
# lifecycle deploy would produce, hence the matching legacy fallback.)
_GENERATION_ID_ENV_VAR = "MINDS_TIER_GENERATION_ID"

# Per-deploy timestamp threaded into the connector's process env by
# ``minds env deploy``. Read at module-import time below to build the
# Modal Secret bundle names, and re-read at request time by ``/version``
# (see ``get_version``); kept in one place so the literal string never
# drifts between those two call sites.
_MINDS_DEPLOY_ID_ENV_VAR = "MINDS_DEPLOY_ID"

# Test-only env var honored by ``/health/liveness``. When set to ``"1"``,
# the liveness probe returns 500 unconditionally so the deployment-test
# suite can drive the auto-rollback path in ``minds env deploy`` without
# editing source. Unset in every non-test deploy. See
# ``specs/minds-deployment-tests.md`` (``test_deploy_auto_rollback_on_broken_healthcheck``).
_INJECT_BROKEN_HEALTHCHECK_ENV_VAR = "MINDS_INJECT_BROKEN_HEALTHCHECK"


@web_app.get("/health/liveness")
def get_health_liveness() -> dict[str, str]:
    """Lightweight no-auth liveness probe.

    Used by ``minds env deploy``'s post-deploy health check to confirm
    the connector is reachable. Returns a fixed body so the poller has
    something to assert on beyond a 200 status.

    Honors ``MINDS_INJECT_BROKEN_HEALTHCHECK=1`` per-request so the
    deployment-test suite can drive the auto-rollback flow. The env
    var is unset in every non-test deploy.
    """
    if os.environ.get(_INJECT_BROKEN_HEALTHCHECK_ENV_VAR) == "1":
        raise HTTPException(status_code=500, detail="liveness probe failed: MINDS_INJECT_BROKEN_HEALTHCHECK=1")
    return {"status": "ok"}


@web_app.get("/generation")
def get_generation() -> dict[str, str]:
    """Return the tier generation id minted at ``minds env deploy`` time.

    ``minds env activate <tier>`` polls this on the client side: if the
    returned id differs from the per-env ``last_seen_generation``
    marker the dev has on disk, the tier has been destroyed + redeployed
    since they last activated, and local state needs to be wiped.

    Doesn't require auth -- the generation id is non-sensitive (just a
    uuid the operator can read off ``minds env list`` or Vault anyway).
    """
    return {"generation_id": os.environ.get(_GENERATION_ID_ENV_VAR, "")}


@web_app.get("/version")
def get_version() -> dict[str, str]:
    """Return the connector's deploy id + tier generation id.

    Used by the deployment-test suite to assert that a re-deploy
    actually advances the live Modal app version (the ``deploy_id``
    field) and as part of the logged-in smoke test's "is this env
    healthy" sanity check.

    Reads two env vars that are already populated by ``minds env
    deploy`` for every tier:

    * ``MINDS_DEPLOY_ID`` -- the compact ISO-8601 timestamp minted by
      ``secret_lifecycle.make_deploy_id`` and threaded through the
      Modal Secret bundle; advances on every successful deploy.
    * ``MINDS_TIER_GENERATION_ID`` -- the tier generation uuid;
      empty for tiers that don't track generations (dev today).

    No auth required (mirrors ``/generation`` -- the values are
    non-sensitive and surfaceable from any operator's machine via
    ``modal app describe``).
    """
    return {
        "deploy_id": os.environ.get(_MINDS_DEPLOY_ID_ENV_VAR, ""),
        "generation_id": os.environ.get(_GENERATION_ID_ENV_VAR, ""),
    }


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
    """Delete a tunnel and all its associated DNS records, Access Applications, ingress rules, and KV entries.

    Idempotent at the HTTP layer -- a second DELETE on an already-gone
    tunnel returns 200 with ``status: already_deleted`` rather than
    404. Clients retrying after a transient error therefore don't have
    to special-case ``404 Not Found``.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        try:
            get_ctx().delete_tunnel(tunnel_name, admin.username)
        except HTTPException as exc:
            if exc.status_code == 404:
                return {"status": "already_deleted"}
            raise
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
# Host pool endpoints
# ---------------------------------------------------------------------------


@web_app.post("/hosts/lease")
def lease_host(request: Request, body: LeaseHostRequest) -> dict[str, object]:
    """Lease an available host from the pool, injecting the caller's SSH public key."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Build the lease selection dynamically. A hard ``region``
                    # adds an equality filter; when unset the lease is
                    # region-agnostic. The selection stays a single round-trip
                    # (the fast path must not pay an extra query).
                    where_clauses = ["status = 'available'", "attributes @> %s::jsonb"]
                    query_params: list[object] = [json.dumps(body.attributes)]
                    if body.region is not None:
                        where_clauses.append("region = %s")
                        query_params.append(body.region)
                    order_by = "created_at ASC"
                    lease_select_sql = (
                        "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, attributes, "
                        "outer_host_public_key, container_host_public_key "
                        "FROM pool_hosts "
                        f"WHERE {' AND '.join(where_clauses)} "
                        f"ORDER BY {order_by} LIMIT 1 FOR UPDATE SKIP LOCKED"
                    )
                    cur.execute(lease_select_sql, tuple(query_params))
                    row = cur.fetchone()
                    if row is None:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "No pre-created agents match the requested attributes. "
                                "Please ask Josh to provision more, or relax the attribute filter."
                            ),
                        )
                    (
                        host_db_id,
                        vps_address,
                        ssh_port,
                        ssh_user,
                        container_ssh_port,
                        agent_id,
                        host_id,
                        attributes,
                        outer_host_public_key,
                        container_host_public_key,
                    ) = row

                    # Fail closed: a row without both pinned host keys cannot be
                    # leased without trust-on-first-use. This only happens for rows
                    # baked before the host-key columns existed; the one-time
                    # keyscan backfill populates them. Surface it as no-capacity so
                    # the caller (and the fast/slow path retry) treats it like an
                    # unavailable host rather than a hard error.
                    if not outer_host_public_key or not container_host_public_key:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"Pool host {host_db_id} has no pinned SSH host keys yet; "
                                "run the one-time `mngr imbue_cloud admin` host-key backfill."
                            ),
                        )

                    # Inject the user's SSH public key on VPS and container, pinning
                    # each sshd's recorded host key (strict, no trust-on-first-use).
                    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
                    try:
                        _append_authorized_key(
                            vps_address,
                            ssh_port,
                            ssh_user,
                            management_key_pem,
                            body.ssh_public_key,
                            outer_host_public_key,
                        )
                        _append_authorized_key(
                            vps_address,
                            container_ssh_port,
                            ssh_user,
                            management_key_pem,
                            body.ssh_public_key,
                            container_host_public_key,
                        )
                    except (paramiko.SSHException, OSError) as exc:
                        logger.warning("SSH key injection failed for host %s: %s", host_db_id, exc)
                        raise HTTPException(
                            status_code=502, detail=f"Failed to inject SSH key on host: {exc}"
                        ) from exc

                    # ``host_name`` is mutable per-lease: it gets overwritten with the
                    # user-supplied name each time the pool row is leased (and could
                    # later be patched by a rename endpoint).
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'leased', leased_to_user = %s, "
                        "leased_at = NOW(), host_name = %s WHERE id = %s",
                        (admin.username, body.host_name, host_db_id),
                    )
        finally:
            conn.close()
        attrs_dict = attributes if isinstance(attributes, dict) else {}
        return LeaseHostResponse(
            host_db_id=host_db_id,
            vps_address=vps_address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            agent_id=agent_id,
            host_id=host_id,
            host_name=body.host_name,
            attributes=attrs_dict,
            outer_host_public_key=outer_host_public_key,
            container_host_public_key=container_host_public_key,
        ).model_dump()


@web_app.post("/hosts/{host_db_id}/release")
def release_host(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Release a leased host: cancel the OVH VPS, strip its tags, drop the row.

    Runs the full cleanup chain inline and **synchronously**: flip the row to
    ``removing`` (the durable, retryable in-progress marker), strip the
    per-lease OVH tags, cancel the VPS, then delete the row.

    Returns 200 only once *every* step has succeeded -- a "released" result
    truly means the VPS is cancelled. If any teardown step fails, the row stays
    ``removing`` and the endpoint returns an error (5xx) so the client (or the
    hourly sweep backstop) retries; we never report success on a failed cancel.
    A failure before ``removing`` is committed (lookup, ownership, the status
    flip) surfaces as an error too.

    Idempotent at the HTTP layer: a release on a row that is already gone
    (deleted) or no longer leased returns 200 ``status: already_released``.
    Ownership is still enforced -- a row leased by another user returns 403.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                # ``str(host_db_id)`` because psycopg2 can't adapt the
                # Python ``UUID`` type that FastAPI parsed from the path
                # (it raises "can't adapt type 'UUID'").
                cur.execute(
                    "SELECT leased_to_user, status, vps_instance_id, backend_kind, "
                    "lima_instance_name, lima_disk_name, bare_metal_server_id "
                    "FROM pool_hosts WHERE id = %s",
                    (str(host_db_id),),
                )
                row = cur.fetchone()
                # A missing row means cleanup already finished (idempotent).
                if row is None:
                    return ReleaseHostResponse(status="already_released").model_dump()
                (
                    leased_to_user,
                    status,
                    vps_instance_id,
                    backend_kind,
                    lima_instance_name,
                    lima_disk_name,
                    bare_metal_server_id,
                ) = row
                # Ownership check first: we don't want to leak a status
                # signal to other users via the response code.
                if leased_to_user != admin.username:
                    raise HTTPException(status_code=403, detail="You do not own this host lease")
                # Only a leased or already-removing row is eligible for
                # cleanup; anything else is treated as already released.
                if status not in ("leased", "removing"):
                    return ReleaseHostResponse(status="already_released").model_dump()
                if status == "leased":
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'removing', released_at = NOW() WHERE id = %s",
                        (str(host_db_id),),
                    )
                    conn.commit()
            # Past the commit point: the row is durably ``removing`` and the
            # sweep will finish anything that fails below, so we always
            # return 200 from here.
            _finish_releasing_pool_host(
                conn,
                host_db_id,
                vps_instance_id,
                backend_kind,
                lima_instance_name,
                lima_disk_name,
                bare_metal_server_id,
            )
        finally:
            conn.close()
        return ReleaseHostResponse(status="released").model_dump()


def _finish_releasing_pool_host(
    conn: Any,
    host_db_id: Any,
    vps_instance_id: str | None,
    backend_kind: str | None,
    lima_instance_name: str | None,
    lima_disk_name: str | None,
    bare_metal_server_id: Any,
) -> None:
    """Tear down a host already marked ``removing``, then delete the row.

    Branches on ``backend_kind``: a real OVH VPS is cancelled in OVH; a slice
    has its lima VM destroyed on its bare-metal box. **Raises** on any failure
    rather than swallowing it -- the caller has already committed the row to
    ``removing`` (a durable, retryable in-progress marker), so a failure here
    propagates to the HTTP layer: the release reports failure, the row stays
    ``removing``, and the client (or the hourly sweep) retries. A release that
    cannot actually destroy the underlying machine must never report success.
    """
    if backend_kind == BACKEND_KIND_SLICE:
        clean_up_slice_on_box(conn, host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name)
    elif vps_instance_id:
        clean_up_pool_host_in_ovh(_get_ovh_ops(), vps_instance_id, ovh_region_code_for_endpoint(_get_ovh_endpoint()))
    else:
        raise PoolHostCleanupError(f"pool host {host_db_id} has no vps_instance_id; cannot cancel its VPS")
    _delete_pool_host_row(conn, host_db_id)


@web_app.get("/hosts")
def list_leased_hosts(request: Request) -> list[dict[str, object]]:
    """List all hosts currently leased by the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, "
                    "host_name, attributes, leased_at, outer_host_public_key, container_host_public_key "
                    "FROM pool_hosts "
                    "WHERE status = 'leased' AND leased_to_user = %s",
                    (admin.username,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            LeasedHostInfo(
                host_db_id=r[0],
                vps_address=r[1],
                ssh_port=r[2],
                ssh_user=r[3],
                container_ssh_port=r[4],
                agent_id=r[5],
                host_id=r[6],
                host_name=r[7],
                attributes=r[8] if isinstance(r[8], dict) else {},
                leased_at=str(r[9]) if r[9] is not None else "",
                outer_host_public_key=r[10],
                container_host_public_key=r[11],
            ).model_dump()
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Paid-list CRUD (admin-key authenticated)
# ---------------------------------------------------------------------------


class PaidListEntryRequest(BaseModel):
    value: str = Field(description="The domain or email to add/remove (normalized to lowercase server-side)")


class PaidDomainInfo(BaseModel):
    domain: str = Field(description="The allowed domain (lowercased)")
    is_paid: bool = Field(description="Whether this domain currently grants paid access")
    created_at: str = Field(description="When the row was first inserted")
    updated_at: str = Field(description="When is_paid was last changed")


class PaidEmailInfo(BaseModel):
    email: str = Field(description="The allowed email (lowercased)")
    is_paid: bool = Field(description="Whether this email currently grants paid access")
    created_at: str = Field(description="When the row was first inserted")
    updated_at: str = Field(description="When is_paid was last changed")


def _normalize_paid_domain(value: str) -> str:
    """Lowercase + validate a domain entry (no ``@``, no internal whitespace, non-empty)."""
    normalized = value.strip().lower()
    if not normalized:
        raise InvalidPaidListEntryError(value, "domain must not be empty")
    if "@" in normalized:
        raise InvalidPaidListEntryError(value, "domain must not contain '@' (use the email list for full addresses)")
    if any(character.isspace() for character in normalized):
        raise InvalidPaidListEntryError(value, "domain must not contain whitespace")
    return normalized


def _normalize_paid_email(value: str) -> str:
    """Lowercase + validate an email entry (exactly one ``@`` with non-empty local + domain parts)."""
    normalized = value.strip().lower()
    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain or "@" in domain or any(c.isspace() for c in normalized):
        raise InvalidPaidListEntryError(value, "email must be of the form 'local@domain'")
    return normalized


def _list_paid_entries(table: str, value_column: str, paid_only: bool) -> list[tuple[str, bool, str, str]]:
    """Return all rows of a paid-list table as ``(value, is_paid, created_at, updated_at)`` tuples."""
    where_clause = " WHERE is_paid = TRUE" if paid_only else ""
    conn = _get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {value_column}, is_paid, created_at, updated_at FROM {table}{where_clause} "
                f"ORDER BY {value_column} ASC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(row[0], bool(row[1]), str(row[2]), str(row[3])) for row in rows]


def _activate_paid_entry(table: str, value_column: str, value: str) -> None:
    """Upsert a paid-list entry to ``is_paid = true`` (reactivating in place, keeping created_at)."""
    conn = _get_pool_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} ({value_column}, is_paid, created_at, updated_at) "
                    "VALUES (%s, TRUE, NOW(), NOW()) "
                    f"ON CONFLICT ({value_column}) DO UPDATE SET is_paid = TRUE, updated_at = NOW()",
                    (value,),
                )
    finally:
        conn.close()
    clear_paid_status_cache()


def _deactivate_paid_entry(table: str, value_column: str, value: str) -> None:
    """Soft-delete a paid-list entry (``is_paid = false``). A no-op when the row is absent."""
    conn = _get_pool_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET is_paid = FALSE, updated_at = NOW() WHERE {value_column} = %s",
                    (value,),
                )
    finally:
        conn.close()
    clear_paid_status_cache()


@web_app.get("/paid/domains")
def list_paid_domains(request: Request, paid_only: bool = False) -> list[dict[str, object]]:
    """List paid-domain rows. ``paid_only=true`` filters to currently-active entries."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        rows = _list_paid_entries("paid_domains", "domain", paid_only)
        return [
            PaidDomainInfo(domain=value, is_paid=is_paid, created_at=created_at, updated_at=updated_at).model_dump()
            for (value, is_paid, created_at, updated_at) in rows
        ]


@web_app.post("/paid/domains/add")
def add_paid_domain(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Add (or reactivate) a paid domain. Idempotent."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        domain = _normalize_paid_domain(body.value)
        _activate_paid_entry("paid_domains", "domain", domain)
        return {"status": "added", "domain": domain}


@web_app.post("/paid/domains/remove")
def remove_paid_domain(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Soft-remove a paid domain (set is_paid=false). Idempotent."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        domain = _normalize_paid_domain(body.value)
        _deactivate_paid_entry("paid_domains", "domain", domain)
        return {"status": "removed", "domain": domain}


@web_app.get("/paid/emails")
def list_paid_emails(request: Request, paid_only: bool = False) -> list[dict[str, object]]:
    """List paid-email rows. ``paid_only=true`` filters to currently-active entries."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        rows = _list_paid_entries("paid_emails", "email", paid_only)
        return [
            PaidEmailInfo(email=value, is_paid=is_paid, created_at=created_at, updated_at=updated_at).model_dump()
            for (value, is_paid, created_at, updated_at) in rows
        ]


@web_app.post("/paid/emails/add")
def add_paid_email(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Add (or reactivate) a paid email. Idempotent."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        email = _normalize_paid_email(body.value)
        _activate_paid_entry("paid_emails", "email", email)
        return {"status": "added", "email": email}


@web_app.post("/paid/emails/remove")
def remove_paid_email(request: Request, body: PaidListEntryRequest) -> dict[str, object]:
    """Soft-remove a paid email (set is_paid=false). Idempotent."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        email = _normalize_paid_email(body.value)
        _deactivate_paid_entry("paid_emails", "email", email)
        return {"status": "removed", "email": email}


# ---------------------------------------------------------------------------
# LiteLLM key management helpers
# ---------------------------------------------------------------------------


def _litellm_proxy_url() -> str:
    """Return the LiteLLM proxy URL from environment. Raises 503 if not configured."""
    url = os.environ.get("LITELLM_PROXY_URL")
    if not url:
        raise HTTPException(status_code=503, detail="LiteLLM proxy not configured")
    return url.rstrip("/")


def _litellm_master_key() -> str:
    """Return the LiteLLM master key from environment. Raises 503 if not configured."""
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="LiteLLM master key not configured")
    return key


def _litellm_request(
    method: str,
    path: str,
    json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Make an authenticated request to the LiteLLM proxy admin API."""
    url = _litellm_proxy_url() + path
    headers = {"Authorization": "Bearer {}".format(_litellm_master_key())}
    response = httpx.request(
        method=method,
        url=url,
        headers=headers,
        json=json_body,
        params=params,
        timeout=60.0,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        logger.warning("LiteLLM API error: %s %s -> %s %s", method, path, response.status_code, detail)
        raise HTTPException(status_code=response.status_code, detail="LiteLLM error: {}".format(detail))
    return response


def _litellm_base_url_for_agents() -> str:
    """Return the base URL agents should use as ANTHROPIC_BASE_URL."""
    return _litellm_proxy_url()


# ---------------------------------------------------------------------------
# LiteLLM key management endpoints
# ---------------------------------------------------------------------------


@web_app.post("/keys/create")
def create_litellm_key(request: Request, body: CreateKeyRequest) -> dict[str, object]:
    """Create a new LiteLLM virtual key for the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)

        litellm_body: dict[str, object] = {"user_id": user_id}
        if body.key_alias is not None:
            litellm_body["key_alias"] = body.key_alias
        if body.max_budget is not None:
            litellm_body["max_budget"] = body.max_budget
        if body.budget_duration is not None:
            litellm_body["budget_duration"] = body.budget_duration
        if body.metadata is not None:
            litellm_body["metadata"] = body.metadata

        resp = _litellm_request("POST", "/key/generate", json_body=litellm_body)
        data = resp.json()

        return CreateKeyResponse(
            key=data["key"],
            base_url=_litellm_base_url_for_agents(),
        ).model_dump()


@web_app.get("/keys")
def list_litellm_keys(request: Request) -> list[dict[str, object]]:
    """List all LiteLLM virtual keys owned by the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)

        # Without ``return_full_object=true`` LiteLLM returns the keys as a
        # bare list of token-id strings (and the ``KeyInfo`` mapping below
        # would crash on ``entry.get(...)``); with it, each entry is a dict
        # carrying alias / spend / budget / etc.
        resp = _litellm_request(
            "GET",
            "/key/list",
            params={"user_id": user_id, "return_full_object": "true"},
        )
        data = resp.json()

        keys_raw = data if isinstance(data, list) else data.get("keys", [])
        result: list[dict[str, object]] = []
        for entry in keys_raw:
            if not isinstance(entry, dict):
                # Defensive: if LiteLLM ever flips back to bare token strings,
                # surface what we have rather than 500ing.
                result.append(KeyInfo(token=str(entry)).model_dump())
                continue
            result.append(
                KeyInfo(
                    token=entry.get("token", ""),
                    key_alias=entry.get("key_alias"),
                    key_name=entry.get("key_name"),
                    spend=entry.get("spend", 0.0),
                    max_budget=entry.get("max_budget"),
                    budget_duration=entry.get("budget_duration"),
                    user_id=entry.get("user_id"),
                ).model_dump()
            )
        return result


@web_app.get("/keys/{key_id}")
def get_litellm_key_info(request: Request, key_id: str) -> dict[str, object]:
    """Get info (including spend and budget) for a specific LiteLLM key."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)

        resp = _litellm_request("GET", "/key/info", params={"key": key_id})
        data = resp.json()

        info = data.get("info", data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        return KeyInfo(
            token=info.get("token", ""),
            key_alias=info.get("key_alias"),
            key_name=info.get("key_name"),
            spend=info.get("spend", 0.0),
            max_budget=info.get("max_budget"),
            budget_duration=info.get("budget_duration"),
            user_id=info.get("user_id"),
        ).model_dump()


@web_app.put("/keys/{key_id}/budget")
def update_litellm_key_budget(request: Request, key_id: str, body: UpdateBudgetRequest) -> dict[str, object]:
    """Update the budget for a LiteLLM key owned by the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)

        # Verify ownership
        info_resp = _litellm_request("GET", "/key/info", params={"key": key_id})
        info_data = info_resp.json()
        info = info_data.get("info", info_data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        update_body: dict[str, object] = {"key": key_id}
        update_body["max_budget"] = body.max_budget
        if body.budget_duration is not None:
            update_body["budget_duration"] = body.budget_duration

        _litellm_request("POST", "/key/update", json_body=update_body)

        return {"status": "updated"}


@web_app.delete("/keys/{key_id}")
def delete_litellm_key(request: Request, key_id: str) -> dict[str, object]:
    """Delete a LiteLLM key owned by the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)

        # Verify ownership
        info_resp = _litellm_request("GET", "/key/info", params={"key": key_id})
        info_data = info_resp.json()
        info = info_data.get("info", info_data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        _litellm_request("POST", "/key/delete", json_body={"keys": [key_id]})

        return DeleteKeyResponse(status="deleted").model_dump()


# ---------------------------------------------------------------------------
# R2 bucket naming + ownership helpers
# ---------------------------------------------------------------------------


_MAX_BUCKETS_PER_ACCOUNT = 50
_R2_BUCKET_NAME_SEP = "--"
_R2_BUCKET_MIN_LENGTH = 3
_R2_BUCKET_MAX_LENGTH = 63
_R2_BUCKET_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_DEFAULT_R2_KEY_ALIAS = "default"


def slugify_r2_name(value: str) -> str:
    """Lowercase + collapse non-alphanumeric runs into single hyphens; strip edge hyphens."""
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _validate_r2_bucket_name(name: str) -> None:
    if not (_R2_BUCKET_MIN_LENGTH <= len(name) <= _R2_BUCKET_MAX_LENGTH) or not _R2_BUCKET_NAME_RE.match(name):
        raise InvalidR2BucketNameError(name)


def bucket_owner_prefix(username: str) -> str:
    return f"{username}{_R2_BUCKET_NAME_SEP}"


def make_bucket_name(username: str, short_name: str) -> str:
    """Derive the full R2 bucket name from the owner prefix and the user's short name."""
    name = f"{bucket_owner_prefix(username)}{slugify_r2_name(short_name)}"
    _validate_r2_bucket_name(name)
    return name


def verify_bucket_ownership(bucket_name: str, username: str) -> None:
    if not bucket_name.startswith(bucket_owner_prefix(username)):
        raise R2BucketOwnershipError(bucket_name, username)


def r2_s3_endpoint(account_id: str) -> str:
    return f"https://{account_id}.r2.cloudflarestorage.com"


def derive_s3_secret_access_key(token_value: str) -> str:
    """R2 derives the S3 Secret Access Key as the SHA-256 hex digest of the API token value."""
    return hashlib.sha256(token_value.encode()).hexdigest()


def _r2_token_name(bucket_name: str, alias: str | None) -> str:
    return f"mngr-r2:{bucket_name}:{alias or _DEFAULT_R2_KEY_ALIAS}"


# ---------------------------------------------------------------------------
# R2 key-metadata store (DB)
#
# Tracks the *existence* of each bucket-scoped key (access key id, owner,
# bucket, scope, alias) so the connector can list + revoke them. The secret
# (sha256 of the token value) is never persisted -- only the non-secret access
# key id is stored.
# ---------------------------------------------------------------------------


_R2_KEY_COLUMNS = "access_key_id, owner_user_id, bucket_name, access, alias, created_at"


def _r2_key_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "access_key_id": row[0],
        "owner_user_id": row[1],
        "bucket_name": row[2],
        "access": row[3],
        "alias": row[4],
        "created_at": str(row[5]) if row[5] is not None else "",
    }


class KeyStore(Protocol):
    """Abstraction over the r2_keys table so endpoints are unit-testable."""

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None: ...
    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]: ...
    def get_key(self, access_key_id: str) -> dict[str, Any] | None: ...
    def delete_key(self, access_key_id: str) -> None: ...
    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]: ...


class PostgresKeyStore:
    """KeyStore backed by the connector's existing Neon DB (same DB as pool_hosts)."""

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO r2_keys (access_key_id, owner_user_id, bucket_name, access, alias) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (access_key_id, owner_user_id, bucket_name, access, alias),
                    )
        finally:
            conn.close()

    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                if bucket_name is None:
                    cur.execute(
                        f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys WHERE owner_user_id = %s ORDER BY created_at",
                        (owner_user_id,),
                    )
                else:
                    cur.execute(
                        f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys "
                        "WHERE owner_user_id = %s AND bucket_name = %s ORDER BY created_at",
                        (owner_user_id, bucket_name),
                    )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_r2_key_row_to_dict(row) for row in rows]

    def get_key(self, access_key_id: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys WHERE access_key_id = %s", (access_key_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        return _r2_key_row_to_dict(row) if row is not None else None

    def delete_key(self, access_key_id: str) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM r2_keys WHERE access_key_id = %s", (access_key_id,))
        finally:
            conn.close()

    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM r2_keys WHERE owner_user_id = %s AND bucket_name = %s RETURNING {_R2_KEY_COLUMNS}",
                        (owner_user_id, bucket_name),
                    )
                    rows = cur.fetchall()
        finally:
            conn.close()
        return [_r2_key_row_to_dict(row) for row in rows]


@functools.cache
def get_key_store() -> KeyStore:
    return PostgresKeyStore()


# ---------------------------------------------------------------------------
# R2 bucket endpoints
# ---------------------------------------------------------------------------


def _list_owned_buckets(ops: CloudflareOps, username: str) -> list[dict[str, Any]]:
    """List the caller's buckets: R2 name_contains filter, then re-verify the prefix in code."""
    prefix = bucket_owner_prefix(username)
    return [b for b in ops.list_buckets(name_contains=prefix) if str(b.get("name", "")).startswith(prefix)]


def _owned_bucket_exists(ops: CloudflareOps, username: str, full_name: str) -> bool:
    return any(b.get("name") == full_name for b in _list_owned_buckets(ops, username))


def _best_effort_revoke_token(ops: CloudflareOps, token_id: str) -> None:
    try:
        ops.delete_bucket_token(token_id)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        logger.warning("Failed to revoke R2 token %s: %s", token_id, exc)


def _best_effort_delete_bucket(ops: CloudflareOps, bucket_name: str) -> None:
    try:
        ops.delete_bucket(bucket_name)
    except (CloudflareApiError, R2BucketNotEmptyError, R2BucketNotFoundError, httpx.HTTPError) as exc:
        logger.warning("Failed to roll back bucket %s: %s", bucket_name, exc)


def _key_info_from_row(row: dict[str, Any]) -> R2KeyInfo:
    return R2KeyInfo(
        access_key_id=row["access_key_id"],
        bucket_name=row["bucket_name"],
        access=row["access"],
        alias=row["alias"],
        created_at=row["created_at"],
    )


def _mint_and_record_key(
    ops: CloudflareOps,
    store: KeyStore,
    owner_user_id: str,
    bucket_name: str,
    access: str,
    alias: str | None,
    rollback_bucket: bool,
) -> R2KeyMaterial:
    """Mint a bucket-scoped Cloudflare token, record its metadata, and return the S3 material.

    On any failure, best-effort revokes a partially-created token and (when
    ``rollback_bucket``) deletes the just-created bucket so ``bucket create``
    stays atomic.
    """
    created_token_id: str | None = None
    try:
        token_result = ops.create_bucket_token(bucket_name, access, _r2_token_name(bucket_name, alias))
        access_key_id = str(token_result["id"])
        created_token_id = access_key_id
        secret_access_key = derive_s3_secret_access_key(str(token_result["value"]))
        store.add_key(access_key_id, owner_user_id, bucket_name, access, alias)
        return R2KeyMaterial(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            s3_endpoint=r2_s3_endpoint(ops.account_id),
            bucket_name=bucket_name,
            access=access,
        )
    except (CloudflareApiError, httpx.HTTPError, psycopg2.Error) as exc:
        if created_token_id is not None:
            _best_effort_revoke_token(ops, created_token_id)
        if rollback_bucket:
            _best_effort_delete_bucket(ops, bucket_name)
        raise HTTPException(status_code=502, detail=f"Failed to provision bucket key: {exc}") from exc


@web_app.post("/buckets")
def create_bucket_endpoint(request: Request, body: CreateBucketRequest) -> dict[str, object]:
    """Create an R2 bucket for the caller and mint its default key (returned inline)."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, body.name)
        owned = _list_owned_buckets(ops, admin.username)
        if any(b.get("name") == full_name for b in owned):
            raise R2BucketExistsError(full_name)
        if len(owned) >= _MAX_BUCKETS_PER_ACCOUNT:
            raise R2BucketLimitError(_MAX_BUCKETS_PER_ACCOUNT)
        ops.create_bucket(full_name)
        material = _mint_and_record_key(
            ops, get_key_store(), owner_user_id, full_name, body.access, _DEFAULT_R2_KEY_ALIAS, rollback_bucket=True
        )
        return CreateBucketResponse(
            bucket=BucketInfo(bucket_name=full_name, s3_endpoint=r2_s3_endpoint(ops.account_id)),
            key=material,
        ).model_dump()


@web_app.get("/buckets")
def list_buckets_endpoint(request: Request) -> list[dict[str, object]]:
    """List all R2 buckets owned by the caller."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        ops = get_ctx().ops
        endpoint = r2_s3_endpoint(ops.account_id)
        return [
            BucketInfo(bucket_name=str(b["name"]), s3_endpoint=endpoint).model_dump()
            for b in _list_owned_buckets(ops, admin.username)
        ]


@web_app.get("/buckets/{name}")
def get_bucket_endpoint(request: Request, name: str) -> dict[str, object]:
    """Return metadata for one of the caller's buckets (keys come from the keys endpoints)."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, name)
        if not _owned_bucket_exists(ops, admin.username, full_name):
            raise R2BucketNotFoundError(full_name)
        return BucketInfo(bucket_name=full_name, s3_endpoint=r2_s3_endpoint(ops.account_id)).model_dump()


@web_app.delete("/buckets/{name}")
def delete_bucket_endpoint(request: Request, name: str) -> dict[str, str]:
    """Destroy one of the caller's buckets (refuses non-empty) and cascade-revoke its keys."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, name)
        verify_bucket_ownership(full_name, admin.username)
        ops.delete_bucket(full_name)
        revoked = get_key_store().delete_keys_for_bucket(owner_user_id, full_name)
        for row in revoked:
            _best_effort_revoke_token(ops, str(row["access_key_id"]))
        return {"status": "deleted"}


@web_app.post("/buckets/{name}/keys")
def create_bucket_key_endpoint(request: Request, name: str, body: CreateR2KeyRequest) -> dict[str, object]:
    """Mint an additional bucket-scoped key for one of the caller's buckets."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, name)
        if not _owned_bucket_exists(ops, admin.username, full_name):
            raise R2BucketNotFoundError(full_name)
        material = _mint_and_record_key(
            ops, get_key_store(), owner_user_id, full_name, body.access, body.alias, rollback_bucket=False
        )
        return material.model_dump()


@web_app.get("/buckets/{name}/keys")
def list_bucket_keys_endpoint(request: Request, name: str) -> list[dict[str, object]]:
    """List the caller's keys scoped to one bucket."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        full_name = make_bucket_name(admin.username, name)
        rows = get_key_store().list_keys(owner_user_id, full_name)
        return [_key_info_from_row(row).model_dump() for row in rows]


@web_app.get("/bucket-keys")
def list_all_bucket_keys_endpoint(request: Request) -> list[dict[str, object]]:
    """List all of the caller's bucket keys across every bucket."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        rows = get_key_store().list_keys(owner_user_id, None)
        return [_key_info_from_row(row).model_dump() for row in rows]


@web_app.delete("/bucket-keys/{access_key_id}")
def delete_bucket_key_endpoint(request: Request, access_key_id: str) -> dict[str, str]:
    """Revoke one of the caller's bucket keys (by Access Key ID) and drop its DB row."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        require_paid_account(admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        store = get_key_store()
        row = store.get_key(access_key_id)
        if row is None or row["owner_user_id"] != owner_user_id:
            raise HTTPException(status_code=404, detail="Key not found")
        get_ctx().ops.delete_bucket_token(access_key_id)
        store.delete_key(access_key_id)
        return {"status": "deleted"}


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


def _build_session_tokens(user_id: str) -> SessionTokens:
    """Create a new SuperTokens session for the given user and return the tokens."""
    session = create_new_session_without_request_response(
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
def auth_signup(body: SignUpRequest) -> AuthResponse:
    """Create a new email/password account and return a session + user info.

    Any exception from the SuperTokens SDK (core unreachable, schema mismatch,
    etc.) is caught and surfaced as a structured ``AuthResponse(status="ERROR")``
    so the desktop client receives a stable JSON shape rather than a FastAPI
    default 500 body that its typed client cannot parse.
    """
    with handle_endpoint_errors():
        _require_supertokens_configured()
        email = body.email.strip()
        if not email or not body.password:
            return AuthResponse(status="FIELD_ERROR", message="Email and password are required")

        try:
            result = ep_sign_up(tenant_id=_AUTH_TENANT_ID, email=email, password=body.password)

            if isinstance(result, EmailAlreadyExistsError):
                return AuthResponse(status="EMAIL_ALREADY_EXISTS", message="An account with this email already exists")

            if not isinstance(result, EPSignUpOkResult):
                return AuthResponse(status="ERROR", message="Sign-up failed")

            user = result.user
            recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
            tokens = _build_session_tokens(user.id)
            send_email_verification_email(
                tenant_id=_AUTH_TENANT_ID,
                user_id=user.id,
                recipe_user_id=recipe_user_id,
                email=email,
            )
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during signup", exc_info=exc)
            return AuthResponse(status="ERROR", message="Auth backend unavailable")
        return AuthResponse(
            status="OK",
            user=AuthUser(user_id=user.id, email=email),
            tokens=tokens,
            needs_email_verification=True,
        )


@web_app.post("/auth/signin", response_model=AuthResponse)
def auth_signin(body: SignInRequest) -> AuthResponse:
    """Authenticate with email/password and return a session + user info.

    Any exception from the SuperTokens SDK is caught and returned as
    ``AuthResponse(status="ERROR")`` -- see the ``auth_signup`` docstring for
    the rationale.
    """
    with handle_endpoint_errors():
        _require_supertokens_configured()
        email = body.email.strip()
        if not email or not body.password:
            return AuthResponse(status="FIELD_ERROR", message="Email and password are required")

        try:
            result = ep_sign_in(tenant_id=_AUTH_TENANT_ID, email=email, password=body.password)

            if isinstance(result, WrongCredentialsError):
                return AuthResponse(status="WRONG_CREDENTIALS", message="Incorrect email or password")

            if not isinstance(result, EPSignInOkResult):
                return AuthResponse(status="ERROR", message="Sign-in failed")

            user = result.user
            recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
            verified = is_email_verified(recipe_user_id=recipe_user_id, email=email)
            tokens = _build_session_tokens(user.id)
            if not verified:
                send_email_verification_email(
                    tenant_id=_AUTH_TENANT_ID,
                    user_id=user.id,
                    recipe_user_id=recipe_user_id,
                    email=email,
                )
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during signin", exc_info=exc)
            return AuthResponse(status="ERROR", message="Auth backend unavailable")
        return AuthResponse(
            status="OK",
            user=AuthUser(user_id=user.id, email=email),
            tokens=tokens,
            needs_email_verification=not verified,
        )


@web_app.post("/auth/session/refresh", response_model=RefreshSessionResponse)
def auth_refresh_session(body: RefreshSessionRequest) -> RefreshSessionResponse:
    """Exchange a refresh token for a fresh access/refresh token pair."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        try:
            new_session = refresh_session_without_request_response(refresh_token=body.refresh_token)
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
def auth_revoke_sessions(request: Request) -> dict[str, object]:
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
    with handle_endpoint_errors():
        _require_supertokens_configured()
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer credentials")
        user_id = _get_user_id_from_access_token(auth_header[7:])
        revoked = revoke_all_sessions_for_user(user_id=user_id)
        logger.info("Revoked %d sessions for user %s...", len(revoked), user_id[:8])
        return {"status": "OK", "revoked_count": len(revoked)}


@web_app.post("/auth/email/send-verification")
def auth_send_verification_email(body: SendVerificationEmailRequest) -> dict[str, str]:
    """(Re)send the verification email for a given user."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        user = get_user(body.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(body.user_id)
        send_email_verification_email(
            tenant_id=_AUTH_TENANT_ID,
            user_id=body.user_id,
            recipe_user_id=recipe_user_id,
            email=body.email,
        )
        return {"status": "OK"}


@web_app.post("/auth/email/is-verified")
def auth_is_email_verified(body: IsEmailVerifiedRequest) -> dict[str, bool]:
    """Return whether the given user's email is verified."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        user = get_user(body.user_id)
        if user is None:
            return {"verified": False}
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(body.user_id)
        verified = is_email_verified(recipe_user_id=recipe_user_id, email=body.email)
        return {"verified": verified}


@web_app.get("/auth/verify-email", response_class=HTMLResponse)
def auth_verify_email_page(request: Request) -> HTMLResponse:
    """Handle an email verification link click from an email.

    Returns a human-readable HTML page indicating success or failure. Reads the
    ``token`` and ``tenantId`` query parameters directly rather than declaring
    them as function arguments, since SuperTokens camel-cases ``tenantId`` in
    emitted links and we do not want that to leak into the Python identifier.
    """
    with handle_endpoint_errors():
        _require_supertokens_configured()
        token = request.query_params.get("token", "")
        tenant_id = request.query_params.get("tenantId") or _AUTH_TENANT_ID
        if not token:
            return HTMLResponse(_VERIFY_EMAIL_FAILED_HTML, status_code=400)
        try:
            result = verify_email_using_token(tenant_id=tenant_id, token=token)
        except (SuperTokensSessionError, SuperTokensGeneralError, ValueError) as exc:
            logger.error("Email verification error", exc_info=exc)
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
def auth_forgot_password(body: ForgotPasswordRequest) -> dict[str, str]:
    """Send a password reset email for the given address (always succeeds).

    Swallows any backend error (SuperTokens core unreachable, schema mismatch,
    etc.) so that this endpoint's response is byte-identical whether or not an
    account exists for the given address -- a non-200 response for "unknown
    email" vs a 200 for "known email" would leak enumeration signal, and a
    500 on intermittent SuperTokens outages would violate the docstring's
    "always succeeds" contract.
    """
    with handle_endpoint_errors():
        _require_supertokens_configured()
        email = body.email.strip()
        success = {"status": "OK", "message": "If an account exists, a reset email has been sent"}
        if not email:
            return success
        try:
            users = list_users_by_account_info(
                tenant_id=_AUTH_TENANT_ID,
                account_info=AccountInfoInput(email=email),
            )
            if not users:
                return success
            user_id = users[0].id
            result = send_reset_password_email(tenant_id=_AUTH_TENANT_ID, user_id=user_id, email=email)
            if result == "UNKNOWN_USER_ID_ERROR":
                logger.warning("Failed to send password reset email for user %s", user_id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.warning("Auth backend error during forgot-password; returning generic success: %s", exc)
        return success


@web_app.post("/auth/password/reset")
def auth_reset_password(body: ResetPasswordRequest) -> dict[str, str]:
    """Consume a password reset token and set a new password."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        if not body.token or not body.new_password:
            raise HTTPException(status_code=400, detail="Token and new password are required")

        consume_result = consume_password_reset_token(tenant_id=_AUTH_TENANT_ID, token=body.token)
        if not isinstance(consume_result, ConsumePasswordResetTokenOkResult):
            return {"status": "INVALID_TOKEN", "message": "Invalid or expired reset token"}

        update_result = update_email_or_password(
            recipe_user_id=RecipeUserId(consume_result.user_id),
            password=body.new_password,
        )
        if isinstance(update_result, PasswordPolicyViolationError):
            return {"status": "FIELD_ERROR", "message": update_result.failure_reason}
        if not isinstance(update_result, UpdateEmailOrPasswordOkResult):
            raise HTTPException(status_code=500, detail="Failed to update password")
        return {"status": "OK", "message": "Password has been reset"}


@web_app.post("/auth/oauth/authorize", response_model=OAuthAuthorizeResponse)
def auth_oauth_authorize(body: OAuthAuthorizeRequest) -> OAuthAuthorizeResponse:
    """Return the URL to which the user should be redirected to begin OAuth."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        provider = get_provider(tenant_id=_AUTH_TENANT_ID, third_party_id=body.provider_id)
        if provider is None:
            return OAuthAuthorizeResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")
        # ``Provider.get_authorisation_redirect_url`` is async-only on the
        # SuperTokens SDK (the ``syncio`` module exposes a sync ``get_provider``
        # but the Provider object's methods are coroutines). We're inside a
        # sync def endpoint that FastAPI runs in a threadpool worker -- the
        # worker has no running event loop, so the SDK's own async-to-sync
        # wrapper can spin up a fresh loop safely. Same pattern SuperTokens'
        # own ``syncio`` helpers use internally.
        redirect = _supertokens_sync_run(
            provider.get_authorisation_redirect_url(
                redirect_uri_on_provider_dashboard=body.callback_url,
                user_context={},
            )
        )
        return OAuthAuthorizeResponse(status="OK", url=redirect.url_with_query_params)


@web_app.post("/auth/oauth/callback", response_model=AuthResponse)
def auth_oauth_callback(body: OAuthCallbackRequest) -> AuthResponse:
    """Exchange an OAuth callback's query params for a supertokens session."""
    with handle_endpoint_errors():
        _require_supertokens_configured()
        provider = get_provider(tenant_id=_AUTH_TENANT_ID, third_party_id=body.provider_id)
        if provider is None:
            return AuthResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")

        try:
            # ``Provider.exchange_auth_code_for_oauth_tokens`` and
            # ``Provider.get_user_info`` are async-only on the SuperTokens SDK
            # (see ``auth_oauth_authorize`` for the rationale). FastAPI runs
            # this sync endpoint in a threadpool worker with no running event
            # loop, so the SDK's async-to-sync wrapper is safe here.
            oauth_tokens = _supertokens_sync_run(
                provider.exchange_auth_code_for_oauth_tokens(
                    redirect_uri_info=RedirectUriInfo(
                        redirect_uri_on_provider_dashboard=body.callback_url,
                        redirect_uri_query_params=dict(body.query_params),
                        pkce_code_verifier=None,
                    ),
                    user_context={},
                )
            )
            oauth_user = _supertokens_sync_run(provider.get_user_info(oauth_tokens=oauth_tokens, user_context={}))
        except (ValueError, KeyError, OSError) as exc:
            logger.error("OAuth callback failed for %s", body.provider_id, exc_info=exc)
            return AuthResponse(status="ERROR", message=str(exc))

        if oauth_user.email is None or oauth_user.email.id is None:
            return AuthResponse(status="ERROR", message="No email provided by the OAuth provider")

        email = oauth_user.email.id
        result = manually_create_or_update_user(
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

        tokens = _build_session_tokens(result.user.id)
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

# Per-deploy timestamp baked into the deployed function spec by ``minds
# env deploy`` so the connector pins to the matching ``<svc>-<tier>-<id>``
# Modal Secrets. Falls back to a sentinel value when unset so unit tests
# can import the module without raising; the resulting
# ``<svc>-<tier>-MINDS_DEPLOY_ID_UNSET`` secret name doesn't exist in
# any Modal env so a real ``modal deploy`` invocation outside of
# ``minds env deploy`` will fail with "Secret not found" -- the safety
# property the timestamped-secret rollback model needs.
_MINDS_DEPLOY_ID = os.environ.get(_MINDS_DEPLOY_ID_ENV_VAR, "MINDS_DEPLOY_ID_UNSET")

# Warm-pool size for the deployed function. ``minds env deploy`` reads
# the tier's ``[min_containers].connector`` from its committed
# ``deploy.toml`` and threads the value here as
# ``MINDS_CONNECTOR_MIN_CONTAINERS`` at ``modal deploy`` time -- which
# is when this module is imported and the function spec is serialized.
# Defaults to 0 so a deploy that forgets to set the env var gets the
# cheapest possible warm pool (cold start on first hit).
_MIN_CONTAINERS = int(os.environ.get("MINDS_CONNECTOR_MIN_CONTAINERS", "0"))

# How long (seconds) an idle container stays alive before Modal scales it
# down. ``minds env deploy`` threads the tier's
# ``[scaledown_window].connector`` from its committed ``deploy.toml`` here as
# ``MINDS_CONNECTOR_SCALEDOWN_WINDOW`` at ``modal deploy`` time. Dev tiers set
# this high (~10 min) so the no-warm-pool connector stays hot across a dev
# session instead of cold-booting on every request; staging / production
# leave it unset and rely on ``min_containers`` instead. ``0`` (the default,
# and what the ci/test tier uses) means "don't pin it" -- the function falls
# back to Modal's own default scaledown window. Modal requires the value to
# be > 0, so 0 is normalized to ``None`` at the call site below.
_SCALEDOWN_WINDOW = int(os.environ.get("MINDS_CONNECTOR_SCALEDOWN_WINDOW", "0"))

image = modal.Image.debian_slim().pip_install(
    "fastapi[standard]", "httpx", "supertokens-python", "psycopg2-binary", "paramiko", "ovh"
)
app = modal.App(name=f"rsc-{_DEPLOY_ENV}", image=image)


def _get_auth_website_domain() -> str:
    """Return the public URL used in outbound email links (verification, reset).

    Reads ``AUTH_WEBSITE_DOMAIN`` from the per-tier ``supertokens-<env>``
    Modal secret. The value is **required**: it is the URL embedded into
    password-reset and email-verification links, and it must match the
    workspace this app is actually deployed under. Raises
    :class:`RuntimeError` if the secret forgot to set it -- silently
    falling back to a hardcoded workspace would be wrong for every
    non-default tier.
    """
    value = os.environ.get("AUTH_WEBSITE_DOMAIN")
    if not value:
        raise MissingAuthWebsiteDomainError(
            "AUTH_WEBSITE_DOMAIN is not set. Populate it in the "
            f"`supertokens-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}` Modal secret (the deploy script "
            "pushes it from the tier's Vault entry)."
        )
    return value


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


def _connector_secrets() -> list[modal.Secret]:
    """The Modal secrets attached to every connector function (web app + cron).

    Includes ``ovh-<env>`` so the release route and the cleanup cron can make
    signed OVH calls (tag strip + cancel) at runtime.
    """
    return [
        modal.Secret.from_name(f"cloudflare-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"supertokens-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"neon-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"pool-ssh-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"litellm-connector-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"ovh-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV, _MINDS_DEPLOY_ID_ENV_VAR: _MINDS_DEPLOY_ID}),
    ]


@app.function(
    name="api",
    secrets=_connector_secrets(),
    # Warm-pool size driven by ``_MIN_CONTAINERS`` at the top of this
    # module: defaults to 1 for production / staging (avoid cold-boot
    # penalty on auth / lease / tunnel hits from the desktop client) and
    # 0 for dev (per-developer envs sit idle most of the time). Override
    # at deploy time with ``MINDS_MIN_CONTAINERS=<n>``. Mirrors the
    # equivalent block in apps/modal_litellm/app.py.
    min_containers=_MIN_CONTAINERS,
    # Idle-before-scaledown window driven by ``_SCALEDOWN_WINDOW``. ``0``
    # (default / ci) -> ``None`` so Modal uses its own default; dev pins this
    # high so the no-warm-pool connector stays hot across a dev session.
    scaledown_window=_SCALEDOWN_WINDOW or None,
)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    _init_supertokens()
    return web_app


@app.function(
    name="cleanup_removing_pool_hosts",
    secrets=_connector_secrets(),
    # Hourly mop-up of any pool host left in ``removing`` by a crashed or
    # timed-out inline release. The happy path deletes the row inline, so this
    # is purely a safety net.
    schedule=modal.Cron("0 * * * *"),
    timeout=900,
)
def cleanup_removing_pool_hosts() -> dict[str, int]:
    conn = _get_pool_db_connection()
    try:
        success_count, failure_count = run_pool_host_cleanup_sweep(
            conn, _get_ovh_ops(), ovh_region_code_for_endpoint(_get_ovh_endpoint())
        )
        # Audit this env's slices on every box against the DB (alert-only: it never
        # auto-deletes, to avoid racing an in-flight bake). Scoped to MINDS_ENV_NAME so
        # it is safe on a box shared by multiple dev envs. A reconcile failure (DB,
        # SSH, or a missing POOL_SSH_PRIVATE_KEY while boxes exist) is a real failure:
        # let it propagate and fail the cron run rather than silently swallowing it.
        divergence_count = reconcile_slice_boxes(conn, _current_minds_env_name())
    finally:
        conn.close()
    logger.info(
        "Pool host cleanup sweep done: cleaned=%d failed=%d slice_divergences=%d",
        success_count,
        failure_count,
        divergence_count,
    )
    return {
        "cleaned": success_count,
        "failed": failure_count,
        "slice_divergences": divergence_count,
    }
