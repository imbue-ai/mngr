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
import concurrent.futures
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
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Final
from typing import NoReturn
from typing import Protocol
from uuid import UUID

import httpx
import modal
import paramiko
import psycopg2
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
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
from supertokens_python.recipe.emailverification.interfaces import CreateEmailVerificationTokenOkResult
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.syncio import create_email_verification_token
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
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_exponential

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


class QuotaExceededError(RuntimeError):
    """Raised when an operation would exceed one of the account's entitlements.

    Mapped to a structured 403 (``code: quota_exceeded`` plus the entitlement
    name, limit, and current usage) so clients can render "N of M used".
    """

    def __init__(self, entitlement: str, limit: float, current: float, message: str) -> None:
        self.entitlement = entitlement
        self.limit = limit
        self.current = current
        self.message = message
        super().__init__(message)


class R2StorageResultTruncatedError(RuntimeError):
    """Raised when the sweep's GraphQL usage response fills its row budget and may be truncated."""

    def __init__(self, row_count: int, row_limit: int) -> None:
        self.row_count = row_count
        self.row_limit = row_limit
        super().__init__(
            f"R2 storage GraphQL response returned {row_count} rows, filling the {row_limit}-row budget; "
            "the result may be truncated so the sweep must not enforce from it. The query returns one row "
            "per bucket -- shard it into bucketName_in chunks to raise the ceiling."
        )


class CleanupGrantBudgetExhaustedError(RuntimeError):
    """Raised when an account has burned its failed-cleanup-grant budget for the rolling window.

    Mapped to a structured 403 (``code: cleanup_grant_budget_exhausted``) so
    clients can message it separately from quota errors.
    """

    def __init__(self, limit: int, current: int, window_hours: int) -> None:
        self.limit = limit
        self.current = current
        self.window_hours = window_hours
        super().__init__(
            f"Cleanup-grant budget exhausted: {current} grants in the last {window_hours} hours ended "
            f"without any usage decrease (limit {limit}). The budget frees up as those grants age out "
            "of the window; grants that actually reduce usage never count against it."
        )


class PlanNotFoundError(KeyError):
    """Raised when a referenced plan has no row in the plans table."""

    def __init__(self, plan_name: str) -> None:
        self.plan_name = plan_name
        super().__init__(
            f"Plan '{plan_name}' is not seeded in the plans table; "
            "run `minds env deploy` (which writes the [plans] blocks from deploy.toml)."
        )


class InvalidAuthPolicyError(ValueError):
    """Raised when an auth policy has no identity constraint (would expose the service publicly)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Auth policy rejected: {reason}")


class UnknownEntitlementColumnError(ValueError):
    """Raised when an entitlements update names a column that does not exist."""

    def __init__(self, unknown_columns: list[str]) -> None:
        super().__init__(f"Unknown entitlement columns: {unknown_columns}")


class ServicePolicyMissingError(RuntimeError):
    """Raised when a service would be added with no Access policy available at all."""

    def __init__(self, tunnel_name: str) -> None:
        self.tunnel_name = tunnel_name
        super().__init__(
            f"Tunnel '{tunnel_name}' has no default auth policy and no owner policy could be derived; "
            "set a default auth policy on the tunnel before adding services."
        )


class PoolHostCleanupError(RuntimeError):
    """Raised when a pool-host release/teardown cannot destroy the slice's lima VM.

    Surfaced (rather than swallowed to a warning) so a release that fails to
    actually tear down the VM reports failure instead of a false success.
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


class EnableSharingRequest(BaseModel):
    agent_id: str = Field(description="The mngr agent ID whose tunnel hosts the service")
    service_name: str = Field(description="User-chosen name for the service")
    service_url: str = Field(description="Local service URL (e.g. http://localhost:8080)")
    auth_policy: AuthPolicy = Field(description="Access policy applied to the shared service; must carry identity")


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
            "Hard region requirement (lease-region label, e.g. 'US-EAST-VA'). When set, only "
            "hosts whose region column equals this value are eligible; if none is available the "
            "lease fails. Leave unset to be region-agnostic."
        ),
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class LeaseHostResponse(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description="SSH-reachable address of the leased host's bare-metal box (reaches the slice VM)."
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


class RenameHostRequest(BaseModel):
    host_name: str = Field(
        description=(
            "New user-chosen friendly name for the leased host. Must satisfy mngr's SafeName "
            "regex (alphanumeric, dashes/underscores allowed in the middle). Required."
        )
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class RenameHostResponse(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the renamed host")
    host_name: str = Field(description="The new user-chosen friendly name")


class LeasedHostInfo(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description="SSH-reachable address of the leased host's bare-metal box (reaches the slice VM)."
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


class R2KeyInfo(BaseModel):
    access_key_id: str = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: str = Field(description="Access scope: 'read' or 'readwrite'")
    alias: str | None = Field(default=None, description="Human-readable alias")
    created_at: str = Field(description="ISO 8601 timestamp when the key was created")
    enforced_access: str | None = Field(
        default=None,
        description=(
            "Storage-quota enforcement state: 'read' when the sweep downgraded this key because the "
            "owner is over their storage quota; None when the live token policy matches ``access``."
        ),
    )


class CleanupGrantResponse(BaseModel):
    """Result of a storage-cleanup-grant request."""

    status: str = Field(description="'granted' when a grant is active (new or pre-existing), 'not_needed' otherwise")
    expires_at: str | None = Field(default=None, description="When the active grant expires (settlement fallback)")
    baseline_bytes: int | None = Field(default=None, description="Live usage recorded at grant time")
    keys: list[R2KeyInfo] = Field(description="The caller's bucket keys after the grant was applied")


class StorageRecheckResponse(BaseModel):
    """Result of an on-demand storage-enforcement recheck."""

    usage_bytes: int = Field(description="Live total bucket bytes (real-time REST usage)")
    limit_bytes: int = Field(description="The account's max_total_bucket_bytes entitlement")
    is_over_quota: bool = Field(description="Whether live usage exceeds the limit")
    is_grant_settled: bool = Field(description="Whether this recheck settled an outstanding cleanup grant")
    keys: list[R2KeyInfo] = Field(description="The caller's bucket keys after enforcement was applied")


# -- Plans + account entitlements models --

# The quota entitlements every plan (and every per-user row) carries. This
# tuple is the single authority for which columns exist; the admin set-quota
# endpoint validates entitlement names against it.
QUOTA_ENTITLEMENT_NAMES: tuple[str, ...] = (
    "max_remote_workspaces",
    "max_tunnels",
    "max_services_per_tunnel",
    "max_buckets",
    "max_total_bucket_bytes",
    "monthly_llm_spend_usd",
    "max_active_synced_workspaces",
)

# Entitlement columns holding integer counts/bytes (everything except the
# monthly LLM spend, which is a USD amount).
_INTEGER_ENTITLEMENT_NAMES: frozenset[str] = frozenset(QUOTA_ENTITLEMENT_NAMES) - {"monthly_llm_spend_usd"}


class PlanEntitlements(BaseModel):
    """The quota values a plan grants (also the per-user entitlement values)."""

    max_remote_workspaces: int = Field(description="Max concurrent pool-host leases (running or stopped)")
    max_tunnels: int = Field(description="Max Cloudflare tunnels")
    max_services_per_tunnel: int = Field(description="Max forwarded services per tunnel")
    max_buckets: int = Field(description="Max R2 buckets")
    max_total_bucket_bytes: int = Field(description="Max total bytes across all the account's buckets")
    monthly_llm_spend_usd: float = Field(description="Monthly LLM spend cap in USD (rolling; 0 disables key minting)")
    max_active_synced_workspaces: int = Field(description="Max ACTIVE synced workspace records")


class AccountUsage(BaseModel):
    """Live usage numbers for the account, one per quota entitlement."""

    remote_workspaces: int = Field(description="Current pool-host leases")
    tunnels: int = Field(description="Current Cloudflare tunnels")
    buckets: int = Field(description="Current R2 buckets")
    total_bucket_bytes: int = Field(description="Total bytes across the account's buckets (live REST usage)")
    llm_spend_usd_this_period: float = Field(description="LiteLLM aggregate spend in the current budget period")
    llm_budget_resets_at: str | None = Field(
        default=None, description="When the rolling LLM budget period resets (from LiteLLM), if known"
    )
    active_synced_workspaces: int = Field(description="Current ACTIVE synced workspace records")


class AccountInfoResponse(BaseModel):
    """The caller's plan, entitlement values, and live usage."""

    user_id: str = Field(description="SuperTokens user id")
    email: str = Field(description="The caller's verified email")
    plan_name: str = Field(description="Current plan name")
    entitlements: PlanEntitlements = Field(description="The account's current entitlement values")
    usage: AccountUsage = Field(description="Live usage, computed at request time")
    available_plans: list[str] = Field(
        default_factory=list, description="Every plan name currently seeded (for plan-selector UIs)"
    )


class SetPlanRequest(BaseModel):
    plan: str = Field(description="Plan name to switch to (e.g. 'explorer' or 'ally')")


class AdminSetPlanRequest(BaseModel):
    plan: str = Field(description="Plan name to assign (resets the user's entitlements to the plan's defaults)")


class AdminSetQuotaRequest(BaseModel):
    entitlement: str = Field(description="Quota entitlement name (one of QUOTA_ENTITLEMENT_NAMES)")
    value: float = Field(description="New value (must be a whole number for count/byte entitlements)")


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


def _is_transient_cloudflare_access_error(exc: BaseException) -> bool:
    """Whether a Cloudflare Access failure is worth retrying after a short wait.

    Cloudflare's Access control plane is eventually consistent around
    application deletion: recreating (or mutating) an app for a hostname whose
    previous app was deleted seconds earlier intermittently makes the API
    itself fail with its generic ``access.api.error.internal_server_error``
    (code 10001). Those 5xx responses are transient -- the same call succeeds
    once the teardown settles -- so the Access operations retry them.
    """
    return isinstance(exc, CloudflareApiError) and exc.status_code >= 500


_retry_transient_access_errors = retry(
    retry=retry_if_exception(_is_transient_cloudflare_access_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


@_retry_transient_access_errors
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


@_retry_transient_access_errors
def cf_delete_access_app(client: httpx.Client, account_id: str, app_id: str) -> None:
    cf_check(client.delete(f"/accounts/{account_id}/access/apps/{app_id}"))


@_retry_transient_access_errors
def cf_get_access_app_by_domain(client: httpx.Client, account_id: str, hostname: str) -> dict[str, Any] | None:
    response = client.get(f"/accounts/{account_id}/access/apps")
    data = cf_check(response)
    for app_item in data["result"]:
        if app_item.get("domain") == hostname:
            return app_item
    return None


@_retry_transient_access_errors
def cf_list_access_policies(client: httpx.Client, account_id: str, app_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/accounts/{account_id}/access/apps/{app_id}/policies")
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_create_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(f"/accounts/{account_id}/access/apps/{app_id}/policies", json=policy)
    return cf_check(response)["result"]


@_retry_transient_access_errors
def cf_update_access_policy(
    client: httpx.Client, account_id: str, app_id: str, policy_id: str, policy: dict[str, Any]
) -> dict[str, Any]:
    response = client.put(f"/accounts/{account_id}/access/apps/{app_id}/policies/{policy_id}", json=policy)
    return cf_check(response)["result"]


@_retry_transient_access_errors
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


def cf_update_account_token_policies(
    client: httpx.Client, account_id: str, token_id: str, name: str, policies: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replace an account token's policy list in place (the token value is unchanged)."""
    response = client.put(f"/accounts/{account_id}/tokens/{token_id}", json={"name": name, "policies": policies})
    return cf_check(response)["result"]


def cf_roll_account_token_value(client: httpx.Client, account_id: str, token_id: str) -> str:
    """Regenerate an account token's secret value (same token id, same policies)."""
    response = client.put(f"/accounts/{account_id}/tokens/{token_id}/value", json={})
    return cf_check(response)["result"]


def cf_get_bucket_usage(client: httpx.Client, account_id: str, bucket_name: str) -> dict[str, Any]:
    """Return one bucket's live usage (payloadSize / metadataSize / objectCount / uploadCount)."""
    response = client.get(f"/accounts/{account_id}/r2/buckets/{bucket_name}/usage")
    return cf_check(response)["result"]


# Row budget for the sweep's GraphQL query. The query groups by bucketName
# alone, so one row is one bucket and this budget is effectively a
# bucket-count ceiling (Cloudflare accepts limits up to 10000; past that,
# shard the query into bucketName_in chunks). A response that fills the
# budget may be truncated and raises rather than enforcing from partial data.
_R2_STORAGE_GRAPHQL_ROW_LIMIT: Final = 5000

# GraphQL analytics query used by the storage-quota sweep: one request covers
# every bucket in the account, regardless of bucket count, so the sweep never
# scales its REST-API usage with the number of users. Grouping by bucketName
# only (no datetime dimension) yields exactly one row per bucket: the max
# snapshot inside the lookback window. That is the window *peak*, not the
# latest value -- peak >= live, so it can only delay a restore, never justify
# a downgrade on its own; downgrades are re-confirmed against the real-time
# per-bucket REST endpoint (which also serves the display path).
_R2_STORAGE_GRAPHQL_QUERY = (
    """
query R2StorageByBucket($accountTag: string!, $since: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2StorageAdaptiveGroups(
        limit: %d
        filter: {datetime_geq: $since}
      ) {
        max {
          payloadSize
          metadataSize
        }
        dimensions {
          bucketName
        }
      }
    }
  }
}
"""
    % _R2_STORAGE_GRAPHQL_ROW_LIMIT
)

# How far back the sweep's GraphQL query looks for storage snapshots. Only
# needs to contain at least one snapshot per bucket: measured production
# cadence is one snapshot per 10-70 minutes (median 30, newest-snapshot age
# up to ~76 min), so 3 hours holds comfortable margin. A longer window costs
# peak staleness (delayed automatic restores after a cleanup), not rows.
_R2_STORAGE_LOOKBACK_HOURS = 3


def parse_r2_storage_graphql_response(data: dict[str, Any]) -> dict[str, int]:
    """Extract {bucket_name: peak_bytes_in_window} from the r2StorageAdaptiveGroups response.

    One row per bucket (bucketName-only grouping); ``payloadSize`` +
    ``metadataSize`` together are the bucket's stored bytes. A response that
    fills the query's row budget may be truncated -- buckets past the limit
    would silently count as zero usage -- so that case raises
    :class:`R2StorageResultTruncatedError` and fails the sweep loudly instead
    of enforcing from partial data.
    """
    usage_by_bucket: dict[str, int] = {}
    row_count = 0
    accounts = data.get("data", {}).get("viewer", {}).get("accounts", []) if isinstance(data, dict) else []
    for account in accounts:
        for group in account.get("r2StorageAdaptiveGroups", []) or []:
            row_count += 1
            dimensions = group.get("dimensions", {})
            bucket_name = dimensions.get("bucketName")
            if not bucket_name:
                continue
            max_values = group.get("max", {}) or {}
            payload = int(max_values.get("payloadSize") or 0)
            metadata = int(max_values.get("metadataSize") or 0)
            usage_by_bucket[bucket_name] = max(usage_by_bucket.get(bucket_name, 0), payload + metadata)
    if row_count >= _R2_STORAGE_GRAPHQL_ROW_LIMIT:
        raise R2StorageResultTruncatedError(row_count=row_count, row_limit=_R2_STORAGE_GRAPHQL_ROW_LIMIT)
    return usage_by_bucket


def cf_query_r2_storage_by_bucket(client: httpx.Client, account_id: str) -> dict[str, int]:
    """Query the GraphQL analytics dataset for every bucket's peak stored bytes in the lookback window.

    Requires the API token to carry ``Account Analytics: Read``. Raises
    :class:`CloudflareApiError` when the GraphQL layer reports errors and
    :class:`R2StorageResultTruncatedError` when the response fills the row
    budget (possible truncation).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=_R2_STORAGE_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = client.post(
        "/graphql",
        json={
            "query": _R2_STORAGE_GRAPHQL_QUERY,
            "variables": {"accountTag": account_id, "since": since},
        },
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    errors = data.get("errors")
    if errors:
        raise CloudflareApiError(status_code=response.status_code, errors=list(errors))
    return parse_r2_storage_graphql_response(data)


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


# Cloudflare Access include-rule types that constrain access to specific
# identities. Anything outside this set (``everyone``, ``ip``, ...) would let
# a policy make a service publicly reachable, which we do not allow -- Access
# service tokens are the one sanctioned non-identity path and they are managed
# by the dedicated service-token endpoint, never through AuthPolicy bodies.
_IDENTITY_INCLUDE_KEYS: Final = frozenset({"email", "email_domain", "login_method", "group"})


def validate_auth_policy_has_identity(policy: AuthPolicy) -> None:
    """Reject any auth policy that would leave a service publicly reachable.

    Every policy must carry at least one rule, and every rule's ``include``
    list must be non-empty with only identity-constraining entry types.
    Raises :class:`InvalidAuthPolicyError` otherwise.
    """
    if not policy.rules:
        raise InvalidAuthPolicyError("policy must contain at least one rule")
    for rule in policy.rules:
        include = rule.get("include")
        if not isinstance(include, list) or not include:
            raise InvalidAuthPolicyError("every rule must have a non-empty 'include' list")
        for entry in include:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise InvalidAuthPolicyError(f"malformed include entry: {entry!r}")
            (entry_type,) = entry.keys()
            if entry_type not in _IDENTITY_INCLUDE_KEYS:
                raise InvalidAuthPolicyError(
                    f"include type '{entry_type}' is not an identity constraint "
                    f"(allowed: {sorted(_IDENTITY_INCLUDE_KEYS)})"
                )


def owner_email_auth_policy(email: str) -> AuthPolicy:
    """The fallback Access policy: allow only the tunnel owner's verified email."""
    return AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": email}}]}])


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
    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None: ...
    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]: ...
    def get_bucket_usage_bytes(self, bucket_name: str) -> int: ...
    def query_r2_storage_by_bucket(self) -> dict[str, int]: ...


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

    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None:
        policies = build_r2_bucket_token_policies(self.account_id, bucket_name, self._r2_permission_group_id(access))
        cf_update_account_token_policies(self.client, self.account_id, token_id, token_name, policies)

    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]:
        return {"value": cf_roll_account_token_value(self.client, self.account_id, token_id)}

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        usage = cf_get_bucket_usage(self.client, self.account_id, bucket_name)
        return int(usage.get("payloadSize") or 0) + int(usage.get("metadataSize") or 0)

    def query_r2_storage_by_bucket(self) -> dict[str, int]:
        return cf_query_r2_storage_by_bucket(self.client, self.account_id)


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

    def create_tunnel(
        self,
        username: str,
        agent_id: str,
        default_auth_policy: AuthPolicy | None = None,
        # Applied as the tunnel's default policy only when no default is stored
        # yet (idempotent re-creates must not clobber a user-set default).
        fallback_auth_policy: AuthPolicy | None = None,
    ) -> TunnelInfo:
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
            elif fallback_auth_policy is not None and self.ops.kv_get(name) is None:
                self.ops.kv_put(name, fallback_auth_policy.model_dump_json())
            else:
                # A stored default already exists (or no fallback was given);
                # an idempotent re-create must not clobber it.
                pass
            return TunnelInfo(tunnel_name=name, tunnel_id=tid, token=token, services=services)

        result = self.ops.create_tunnel(name)
        tid = result["id"]
        token = self.ops.get_tunnel_token(tid)
        self.ops.put_tunnel_config(tid, wrap_ingress([]))

        effective_policy = default_auth_policy if default_auth_policy is not None else fallback_auth_policy
        if effective_policy is not None:
            self.ops.kv_put(name, effective_policy.model_dump_json())

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

    def get_tunnel_for_agent(self, username: str, agent_id: str) -> TunnelInfo | None:
        """Resolve the caller's tunnel for a single agent in O(1) Cloudflare calls.

        minds always knows the exact tunnel name it wants
        (``<username>--<agent-prefix>``), so this resolves the tunnel via
        Cloudflare's server-side name filter (:func:`cf_get_tunnel_by_name`)
        plus a single config fetch -- 2 Cloudflare calls regardless of how
        many tunnels the account owns. Contrast with :meth:`list_tunnels`,
        which enumerates every tunnel under the user prefix and fetches each
        one's config (O(n) calls). Returns ``None`` when the user has no
        tunnel for the agent yet.
        """
        name = make_tunnel_name(username, agent_id)
        tunnel = self.ops.get_tunnel_by_name(name)
        if tunnel is None:
            return None
        tid = tunnel["id"]
        services = self._list_services(tid, name, username)
        return TunnelInfo(tunnel_name=name, tunnel_id=tid, services=services)

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

    def add_service(
        self,
        tunnel_name: str,
        username: str,
        service_name: str,
        service_url: str,
        # The Access policy applied when the tunnel has no stored default --
        # typically allow-only-the-owner's-email. When both this and the KV
        # default are absent the add is refused: a service must never go up
        # without an Access Application.
        fallback_policy: AuthPolicy | None = None,
        # When provided, the authoritative policy for this service's Access
        # Application: it wins over the stored tunnel default, and on a
        # re-add it REPLACES a pre-existing app's policies. The combined
        # enable-sharing path passes this so the caller's requested ACL
        # always lands in the same call that brings the service up.
        service_policy: AuthPolicy | None = None,
    ) -> ServiceInfo:
        self.verify_ownership(tunnel_name, username)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id_prefix(tunnel_name, username)
        hostname = make_hostname(service_name, agent_id, username, self.domain)

        # Resolve the Access policy up front and create the Access Application
        # BEFORE any exposure exists (DNS/ingress). A failure here aborts the
        # add outright, so a failed Access call can never leave a service
        # publicly reachable.
        stored_default = self.ops.kv_get(tunnel_name)
        if service_policy is not None:
            policy: AuthPolicy | None = service_policy
        elif stored_default is not None:
            policy = AuthPolicy.model_validate_json(stored_default)
        else:
            policy = fallback_policy
        if policy is None:
            raise ServicePolicyMissingError(tunnel_name)
        created_access_app_id: str | None = None
        is_dns_created_here = False
        try:
            existing_access_app = self.ops.get_access_app_by_domain(hostname)
            if existing_access_app is None:
                access_app = self.ops.create_access_app(hostname, f"cf-fwd-{hostname}", allowed_idps=self.allowed_idps)
                created_access_app_id = access_app["id"]
                for cf_policy in policy_to_cf_rules(policy):
                    self.ops.create_access_policy(access_app["id"], cf_policy)
            elif service_policy is not None:
                # An explicit service policy replaces whatever the pre-existing
                # app carried, so a re-enable always ends at the requested ACL.
                for existing_policy in self.ops.list_access_policies(existing_access_app["id"]):
                    self.ops.delete_access_policy(existing_access_app["id"], existing_policy["id"])
                for cf_policy in policy_to_cf_rules(service_policy):
                    self.ops.create_access_policy(existing_access_app["id"], cf_policy)
            else:
                # A pre-existing app means the service was configured before (with a
                # possibly customized policy) -- leave it untouched on re-add.
                pass

            cname_target = f"{tid}.cfargotunnel.com"
            existing_dns = self.ops.list_dns_records(name=hostname)
            if not existing_dns:
                self.ops.create_cname(hostname, cname_target)
                is_dns_created_here = True
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
                r
                for r in non_catchall_rules(config.get("config", {}).get("ingress", []))
                if r.get("hostname") != hostname
            ]
            rules.append(
                {
                    "hostname": hostname,
                    "service": service_url,
                    "originRequest": {"noTLSVerify": True},
                }
            )
            self.ops.put_tunnel_config(tid, wrap_ingress(rules))
        except (CloudflareApiError, httpx.HTTPError):
            # Roll back only what this call created (never a pre-existing DNS
            # record or Access App) so a half-added service leaves nothing
            # behind -- in particular nothing publicly reachable.
            if created_access_app_id is not None:
                self._delete_access_app_for_hostname(hostname)
            if is_dns_created_here:
                self._delete_dns_by_name(hostname)
            raise

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


def derive_username_prefix(user_id: str) -> str:
    """The 16-hex prefix of a SuperTokens user id, used to namespace tunnels/leases/buckets.

    Also the ``account_entitlements.username_prefix`` lookup key, so every
    caller must derive it identically -- always go through this helper.
    """
    return user_id.replace("-", "")[:_USER_ID_PREFIX_LENGTH]


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
        # session getter does NOT auto-reject based on the token's
        # email-verification claim. We determine verification ourselves from a
        # live core lookup below (see the ``email_getter`` call), and raise a
        # clear "Email not verified" only when the email is genuinely
        # unverified (the SDK's default rejection surfaces as a generic
        # ``SuperTokensSessionError`` → "Invalid token", which is misleading).
        session = session_getter(
            access_token=token,
            anti_csrf_check=False,
            override_global_claim_validators=lambda *_args, **_kwargs: [],
        )
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")

    user_id = session.get_user_id()
    user_id_prefix = derive_username_prefix(user_id)

    # Resolve the verified email live from the core rather than trusting the
    # token's cached email-verification claim. That claim is baked into the
    # access token at login and cannot reflect a verification that happened
    # afterwards -- e.g. a user who was just added to the paid list (and thus
    # auto-verified) would keep getting rejected until their token refreshed.
    # ``email_getter`` returns an email only for a *verified* login method, so a
    # ``None`` result means "no verified email" and we reject. The core lookup
    # already ran here on every authenticated request, so gating on it instead
    # of on the token claim adds no extra round trip on the success path.
    email = email_getter(user_id)
    if email is None:
        raise HTTPException(status_code=401, detail="Email not verified")

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

    Raises ``psycopg2.Error`` on any database failure; gate-style callers
    (:func:`require_ally_eligible`) convert that into a fail-closed 403.
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


def require_ally_eligible(
    email: str | None,
    paid_checker: Callable[[str], bool] = is_email_paid,
) -> None:
    """Gate ally-plan selection on the caller's email appearing in the paid lists.

    Raises ``HTTPException(403)`` when the caller has no verified email, when
    their email is not in the ``paid_emails`` / ``paid_domains`` tables, or
    when the database lookup fails (fail closed). This is the only remaining
    consumer of the paid lists as a *gate* -- resource access itself is now
    governed by per-account entitlements. ``paid_checker`` is injected for
    tests; production callers use the cached, table-backed default.
    """
    if not email:
        raise HTTPException(
            status_code=403,
            detail="Account email unavailable; cannot check ally-plan eligibility",
        )
    try:
        is_paid = paid_checker(email)
    except psycopg2.Error as exc:
        logger.warning("Paid-status lookup failed for %s: %s", email, exc)
        raise HTTPException(
            status_code=403,
            detail="Could not verify ally-plan eligibility (database error); please try again",
        ) from exc
    if not is_paid:
        raise HTTPException(
            status_code=403,
            detail="The 'ally' plan requires partner access (a paid-listed email)",
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
# Plans + account entitlements
#
# Plans (git-owned, written from deploy.toml on every deploy) define the
# default entitlements a user receives when a plan is assigned. Each account
# gets its own ``account_entitlements`` row -- created lazily on first
# quota-relevant touch -- whose values are copied wholesale from the plan and
# are the operator-adjustable source of truth thereafter. Changing a plan's
# defaults never retroactively changes existing rows.
# ---------------------------------------------------------------------------


_PLAN_EXPLORER = "explorer"
_PLAN_ALLY = "ally"

# Ship-time cutoff for the lazy-backfill rule: accounts whose SuperTokens
# ``time_joined`` predates this instant get the paid-list-based initial plan
# (ally when paid-listed); accounts created after it always start as explorer
# and must select ally explicitly. 2026-07-21T00:00:00Z, in the epoch
# milliseconds SuperTokens uses for ``time_joined``.
_PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS = 1784592000000

_QUOTA_COLUMNS_SQL = ", ".join(QUOTA_ENTITLEMENT_NAMES)
_PLAN_COLUMNS_SQL = f"plan_name, {_QUOTA_COLUMNS_SQL}"
_ENTITLEMENT_COLUMNS_SQL = f"user_id, username_prefix, plan_name, {_QUOTA_COLUMNS_SQL}"


class AccountEntitlements(PlanEntitlements):
    """One account's entitlement row: identity fields plus the quota values."""

    user_id: str = Field(description="Full SuperTokens user id (row key)")
    username_prefix: str = Field(description="16-hex user-id prefix used to namespace tunnels/leases/buckets")
    plan_name: str = Field(description="The plan this row was last assigned from")

    def quota_values(self) -> PlanEntitlements:
        return PlanEntitlements(
            max_remote_workspaces=self.max_remote_workspaces,
            max_tunnels=self.max_tunnels,
            max_services_per_tunnel=self.max_services_per_tunnel,
            max_buckets=self.max_buckets,
            max_total_bucket_bytes=self.max_total_bucket_bytes,
            monthly_llm_spend_usd=self.monthly_llm_spend_usd,
            max_active_synced_workspaces=self.max_active_synced_workspaces,
        )


def _quota_values_from_row(row: tuple[Any, ...], offset: int) -> dict[str, Any]:
    """Map the trailing quota columns of a SELECT row into name->value pairs."""
    values: dict[str, Any] = {}
    for idx, name in enumerate(QUOTA_ENTITLEMENT_NAMES):
        raw = row[offset + idx]
        values[name] = float(raw) if name == "monthly_llm_spend_usd" else int(raw)
    return values


class EntitlementsStore(Protocol):
    """Abstraction over the plans + account_entitlements tables."""

    def get_plan(self, plan_name: str) -> dict[str, Any] | None: ...
    def list_plans(self) -> list[dict[str, Any]]: ...
    def get_entitlements(self, user_id: str) -> dict[str, Any] | None: ...
    def get_entitlements_by_prefix(self, username_prefix: str) -> dict[str, Any] | None: ...
    def insert_entitlements_if_absent(self, row: dict[str, Any]) -> None: ...
    def update_entitlements(self, user_id: str, values: dict[str, Any]) -> None: ...


class PostgresEntitlementsStore:
    """EntitlementsStore backed by the connector's existing Neon DB."""

    def _plan_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {"plan_name": row[0], **_quota_values_from_row(row, 1)}

    def _entitlements_row_to_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "user_id": row[0],
            "username_prefix": row[1],
            "plan_name": row[2],
            **_quota_values_from_row(row, 3),
        }

    def get_plan(self, plan_name: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PLAN_COLUMNS_SQL} FROM plans WHERE plan_name = %s", (plan_name,))
                row = cur.fetchone()
        finally:
            conn.close()
        return self._plan_row_to_dict(row) if row is not None else None

    def list_plans(self) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PLAN_COLUMNS_SQL} FROM plans ORDER BY plan_name")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [self._plan_row_to_dict(row) for row in rows]

    def get_entitlements(self, user_id: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_ENTITLEMENT_COLUMNS_SQL} FROM account_entitlements WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return self._entitlements_row_to_dict(row) if row is not None else None

    def get_entitlements_by_prefix(self, username_prefix: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_ENTITLEMENT_COLUMNS_SQL} FROM account_entitlements WHERE username_prefix = %s",
                    (username_prefix,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return self._entitlements_row_to_dict(row) if row is not None else None

    def insert_entitlements_if_absent(self, row: dict[str, Any]) -> None:
        column_names = ["user_id", "username_prefix", "plan_name", *QUOTA_ENTITLEMENT_NAMES]
        placeholders = ", ".join(["%s"] * len(column_names))
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO account_entitlements ({', '.join(column_names)}) "
                        f"VALUES ({placeholders}) ON CONFLICT (user_id) DO NOTHING",
                        tuple(row[name] for name in column_names),
                    )
        finally:
            conn.close()

    def update_entitlements(self, user_id: str, values: dict[str, Any]) -> None:
        allowed = {"plan_name", *QUOTA_ENTITLEMENT_NAMES}
        unknown = set(values) - allowed
        if unknown:
            raise UnknownEntitlementColumnError(sorted(unknown))
        assignments = ", ".join(f"{name} = %s" for name in values)
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE account_entitlements SET {assignments}, updated_at = NOW() WHERE user_id = %s",
                        (*values.values(), user_id),
                    )
        finally:
            conn.close()


@functools.cache
def get_entitlements_store() -> EntitlementsStore:
    return PostgresEntitlementsStore()


def _get_user_time_joined_ms(user_id: str, user_getter: Callable[[str], Any] = get_user) -> int:
    """Return the SuperTokens account-creation timestamp (epoch ms), 0 when unknown.

    An unknown timestamp (missing user, SDK error) conservatively counts as
    pre-existing -- the pre-cutoff rule only *adds* the paid-list check, and a
    genuinely-new account is never paid-listed by accident in practice.
    """
    try:
        user = user_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Failed to fetch SuperTokens user %s for time_joined: %s", user_id[:8], exc)
        return 0
    if user is None:
        return 0
    return int(user.time_joined)


def _initial_plan_name_for_user(
    user_id: str,
    email: str,
    # Resolved at call time (not bound as a default) so tests that patch the
    # module-level ``_get_user_time_joined_ms`` take effect.
    time_joined_getter: Callable[[str], int] | None = None,
    paid_checker: Callable[[str], bool] = is_email_paid,
) -> str:
    """Pick the plan for a lazily-created entitlements row.

    Accounts predating the feature-ship cutoff get ally when their email is
    paid-listed (the backfill rule); everyone else starts as explorer.
    """
    resolved_getter = time_joined_getter if time_joined_getter is not None else _get_user_time_joined_ms
    if resolved_getter(user_id) < _PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS and email and paid_checker(email):
        return _PLAN_ALLY
    return _PLAN_EXPLORER


def ensure_account_entitlements(
    user_id: str,
    username_prefix: str,
    email: str,
    store: "EntitlementsStore | None" = None,
) -> AccountEntitlements:
    """Return the account's entitlements row, lazily creating it from the initial plan.

    The lazy creation writes only the DB row; the LiteLLM user budget is
    pushed later, at the points that actually need it (`/keys/create` and the
    explicit plan/quota operations), so an unreachable LiteLLM cannot fail an
    unrelated request. Insert races resolve via ON CONFLICT DO NOTHING plus a
    re-read.
    """
    entitlements_store = store if store is not None else get_entitlements_store()
    existing = entitlements_store.get_entitlements(user_id)
    if existing is not None:
        return AccountEntitlements(**existing)
    plan_name = _initial_plan_name_for_user(user_id, email)
    plan = entitlements_store.get_plan(plan_name)
    if plan is None:
        raise PlanNotFoundError(plan_name)
    row = {
        "user_id": user_id,
        "username_prefix": username_prefix,
        "plan_name": plan_name,
        **{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES},
    }
    entitlements_store.insert_entitlements_if_absent(row)
    stored = entitlements_store.get_entitlements(user_id)
    if stored is None:
        raise HTTPException(status_code=500, detail="Failed to create the account entitlements row")
    return AccountEntitlements(**stored)


def resolve_entitlements_for_admin(request: Request, admin: AdminAuth) -> AccountEntitlements:
    """Resolve (lazily creating) the entitlements row for an admin-authenticated request."""
    token = request.headers.get("authorization", "")[7:]
    user_id = _get_user_id_from_access_token(token)
    return ensure_account_entitlements(user_id=user_id, username_prefix=admin.username, email=admin.email or "")


def raise_quota_exceeded(entitlement: str, limit: float, current: float, noun: str) -> NoReturn:
    raise QuotaExceededError(
        entitlement=entitlement,
        limit=limit,
        current=current,
        message=(
            f"Quota exceeded: this account allows {limit:g} {noun} and {current:g} are already in use. "
            "Free some up, or ask for a higher limit."
        ),
    )


# ---------------------------------------------------------------------------
# LiteLLM user budgets
#
# The monthly LLM spend quota is enforced by LiteLLM itself: every virtual key
# carries the account's SuperTokens user_id, and LiteLLM aggregates spend from
# all of a user's keys against the *user-level* ``max_budget``. The budget is
# a rolling monthly window (``budget_duration = "1mo"``, anchored when the
# budget is first created). Pushed at key-creation time and on every explicit
# plan/quota change -- never during lazy row creation, so an unreachable
# LiteLLM cannot fail an unrelated request.
# ---------------------------------------------------------------------------


_LITELLM_USER_BUDGET_DURATION = "1mo"


def upsert_litellm_user_budget(user_id: str, max_budget: float) -> None:
    """Create or update the LiteLLM internal user carrying the account's monthly budget.

    Raises (via ``_litellm_request``) on failure -- callers deliberately let
    that fail the whole operation so the DB row and LiteLLM never diverge.
    """
    body: dict[str, object] = {
        "user_id": user_id,
        "max_budget": max_budget,
        "budget_duration": _LITELLM_USER_BUDGET_DURATION,
    }
    try:
        _litellm_request("POST", "/user/new", json_body=body)
    except HTTPException as exc:
        # LiteLLM rejects /user/new for an existing user (the exact status/text
        # varies by version); fall through to /user/update, which raises on any
        # genuine failure.
        logger.debug("LiteLLM /user/new for %s rejected (%s); trying /user/update", user_id[:8], exc.status_code)
        _litellm_request("POST", "/user/update", json_body=body)


def get_litellm_user_spend(
    user_id: str,
    # Resolved at call time (not bound as a default) so installed fakes that
    # replace the module-level ``_litellm_request`` still take effect.
    request_fn: "Callable[..., httpx.Response] | None" = None,
) -> tuple[float, str | None]:
    """Return (spend this budget period, budget reset timestamp) for the account.

    A user that does not exist in LiteLLM yet (never minted a key) reports
    zero spend. Any LiteLLM error also reports zero -- this feeds the
    display-only usage endpoint, not enforcement. ``request_fn`` is injected
    for tests; production callers use the module-level ``_litellm_request``.
    """
    resolved_request = request_fn if request_fn is not None else _litellm_request
    try:
        response = resolved_request("GET", "/user/info", params={"user_id": user_id})
    except (HTTPException, httpx.HTTPError) as exc:
        # HTTPException covers HTTP >= 400 responses and missing proxy config;
        # httpx.HTTPError covers transport failures (proxy unreachable).
        logger.warning("LiteLLM /user/info for %s failed (%s); reporting zero spend", user_id[:8], exc)
        return 0.0, None
    data = response.json()
    info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return 0.0, None
    spend = info.get("spend")
    reset_at = info.get("budget_reset_at")
    return (float(spend) if spend is not None else 0.0, str(reset_at) if reset_at else None)


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
    if isinstance(exc, QuotaExceededError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "quota_exceeded",
                "entitlement": exc.entitlement,
                "limit": exc.limit,
                "current": exc.current,
                "message": exc.message,
            },
        ) from exc
    if isinstance(exc, CleanupGrantBudgetExhaustedError):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cleanup_grant_budget_exhausted",
                "limit": exc.limit,
                "current": exc.current,
                "window_hours": exc.window_hours,
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, R2StorageResultTruncatedError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if isinstance(exc, PlanNotFoundError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, InvalidAuthPolicyError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ServicePolicyMissingError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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


# What counts as one "remote workspace": a pool-host row leased to the user
# (running or stopped -- stopped workspaces still hold their lease and slice).
# Shared by the lease-time quota check and the /account usage display so the
# two can never drift.
_COUNT_LEASED_HOSTS_SQL: Final = "SELECT COUNT(*) FROM pool_hosts WHERE leased_to_user = %s AND status = 'leased'"


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
# Slice pool-host cleanup
#
# A pool host is a "slice": a lima VM on one of our bare-metal boxes. Releasing
# it (the inline release path) destroys the VM by SSHing the box and running
# limactl. The connector makes no provider-API calls of its own.
# ---------------------------------------------------------------------------


def _delete_pool_host_row(conn: Any, host_db_id: Any) -> None:
    """Delete a single pool_hosts row by id (committing immediately)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(host_db_id),))
    conn.commit()


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
    # here.
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
                "SELECT lima_instance_name FROM pool_hosts WHERE bare_metal_server_id = %s",
                (str(server_id),),
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


def count_user_tunnels(ops: CloudflareOps, username_prefix: str) -> int:
    """Count the user's tunnels.

    Shared by the tunnel quota check (``POST /tunnels``) and the ``/account``
    usage display so the two can never drift.
    """
    prefix = f"{username_prefix}{TUNNEL_NAME_SEP}"
    return len([t for t in ops.list_tunnels(include_prefix=prefix) if t["name"].startswith(prefix)])


def enforce_tunnel_quota_for_new_tunnel(
    ops: CloudflareOps, username: str, tunnel_name: str, entitlements: AccountEntitlements
) -> None:
    """Refuse creating ``tunnel_name`` when it does not exist yet and the account is at ``max_tunnels``.

    Idempotent re-creates of an existing tunnel are always allowed, so the
    count is only checked when the tunnel is absent. Shared by ``POST
    /tunnels`` and ``POST /sharing/enable`` so the two enforcement points
    cannot drift.
    """
    if ops.get_tunnel_by_name(tunnel_name) is not None:
        return
    current = count_user_tunnels(ops, username)
    if current >= entitlements.max_tunnels:
        raise_quota_exceeded("max_tunnels", entitlements.max_tunnels, current, "tunnels")


@web_app.post("/tunnels")
def create_tunnel(request: Request, body: CreateTunnelRequest) -> dict[str, object]:
    """Create a tunnel (idempotent) and return its info with token.

    Enforces the account's tunnel quota (idempotent re-creates of an existing
    tunnel are always allowed), validates any provided default auth policy,
    and -- when none is provided -- installs an allow-only-the-owner's-email
    default so services added later are never publicly reachable.
    """
    with handle_endpoint_errors():
        ctx = get_ctx()
        auth = authenticate_request(request, ctx.ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        if body.default_auth_policy is not None:
            validate_auth_policy_has_identity(body.default_auth_policy)
        tunnel_name = make_tunnel_name(admin.username, body.agent_id)
        enforce_tunnel_quota_for_new_tunnel(ctx.ops, admin.username, tunnel_name, entitlements)
        fallback = owner_email_auth_policy(admin.email) if admin.email else None
        return ctx.create_tunnel(
            admin.username,
            body.agent_id,
            default_auth_policy=body.default_auth_policy,
            fallback_auth_policy=fallback,
        ).model_dump()


@web_app.get("/tunnels")
def list_tunnels(request: Request) -> list[dict[str, object]]:
    """List all tunnels belonging to the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        return [t.model_dump() for t in get_ctx().list_tunnels(admin.username)]


@web_app.get("/tunnels/by-agent/{agent_id}")
def get_tunnel_for_agent(request: Request, agent_id: str) -> dict[str, object] | None:
    """Resolve the authenticated user's tunnel for ``agent_id`` (O(1) lookup).

    Uses Cloudflare's server-side name filter plus one config fetch (2
    Cloudflare calls) instead of the O(n) ``GET /tunnels`` path that
    enumerates every tunnel and fetches each one's config. The static
    ``by-agent`` prefix can never collide with a real ``{tunnel_name}``
    (those always contain the ``--`` separator), so there is no ambiguity
    with the other ``/tunnels/*`` routes.

    Returns HTTP 200 with ``null`` when the user has no tunnel for the agent
    yet (rather than 404). This is deliberate: a client hitting a connector
    that predates this endpoint gets FastAPI's generic 404-for-unknown-route,
    so reserving 404 exclusively for "endpoint absent" lets the client tell
    "this connector is too old, fall back to enumerating ``GET /tunnels``"
    apart from "the endpoint works and there is simply no tunnel" (200 null).
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        tunnel = get_ctx().get_tunnel_for_agent(admin.username, agent_id)
        return tunnel.model_dump() if tunnel is not None else None


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


def _service_quota_and_owner_email(request: Request, auth: AuthResult, tunnel_name: str) -> tuple[int, str | None]:
    """Resolve the services-per-tunnel limit and the owner's email for either auth kind.

    Admin auth resolves (lazily creating) the caller's entitlements row and
    uses their verified email. Agent auth only knows the tunnel-name prefix:
    it reads the row by prefix (created earlier, at admin-authed tunnel
    creation) and looks the owner's email up from SuperTokens; a missing row
    falls back to the explorer plan's limit with no derivable owner email.
    """
    if isinstance(auth, AdminAuth):
        entitlements = resolve_entitlements_for_admin(request, auth)
        return entitlements.max_services_per_tunnel, auth.email
    prefix = extract_username_from_tunnel_name(tunnel_name)
    store = get_entitlements_store()
    row = store.get_entitlements_by_prefix(prefix)
    if row is not None:
        entitlements = AccountEntitlements(**row)
        return entitlements.max_services_per_tunnel, _default_email_getter(entitlements.user_id)
    plan = store.get_plan(_PLAN_EXPLORER)
    if plan is None:
        raise PlanNotFoundError(_PLAN_EXPLORER)
    return int(plan["max_services_per_tunnel"]), None


def enforce_service_quota(existing_services: list[ServiceInfo], service_name: str, limit: int) -> None:
    """Refuse adding ``service_name`` when the tunnel is at ``limit`` services.

    Re-adding an existing service is always allowed. Shared by ``POST
    /tunnels/{tunnel_name}/services`` and ``POST /sharing/enable`` so the two
    enforcement points cannot drift.
    """
    if service_name in {s.service_name for s in existing_services}:
        return
    if len(existing_services) >= limit:
        raise_quota_exceeded("max_services_per_tunnel", limit, len(existing_services), "services on this tunnel")


@web_app.post("/tunnels/{tunnel_name}/services")
def add_service(request: Request, tunnel_name: str, body: AddServiceRequest) -> dict[str, object]:
    """Add a service to a tunnel. Works with both admin and agent auth.

    Enforces the services-per-tunnel quota (re-adding an existing service is
    always allowed) and guarantees the service comes up behind a Cloudflare
    Access Application -- falling back to an owner-email-only policy when the
    tunnel has no stored default, and refusing outright when no policy can be
    derived at all.
    """
    with handle_endpoint_errors():
        ctx = get_ctx()
        auth = authenticate_request(request, ctx.ops)
        username = require_tunnel_access(auth, tunnel_name)
        limit, owner_email = _service_quota_and_owner_email(request, auth, tunnel_name)
        enforce_service_quota(ctx.list_services(tunnel_name, username), body.service_name, limit)
        fallback = owner_email_auth_policy(owner_email) if owner_email else None
        return ctx.add_service(
            tunnel_name,
            username,
            body.service_name,
            body.service_url,
            fallback_policy=fallback,
        ).model_dump()


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
    """Set the default auth policy for a tunnel. Identity-less policies are rejected."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        validate_auth_policy_has_identity(body)
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
    """Set the auth policy for a specific service. Identity-less policies are rejected."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        validate_auth_policy_has_identity(body)
        get_ctx().set_service_auth(tunnel_name, admin.username, service_name, body)
        return {"status": "updated"}


@web_app.post("/sharing/enable")
def enable_sharing_endpoint(request: Request, body: EnableSharingRequest) -> dict[str, object]:
    """Enable (or update) sharing for one service in a single call.

    Collapses the client's previous create-tunnel + add-service +
    set-service-auth sequence -- three round trips, each paying CLI and
    network overhead -- into one request: ensure the tunnel exists
    (idempotent), add the service with the caller's Access policy applied
    directly to its Access Application (replacing a pre-existing app's
    policies on re-enable), and return the resulting tunnel (with token)
    plus the service info, so the caller needs no follow-up status reads.

    Enforces the same quotas as the individual endpoints: the tunnel count
    when a new tunnel would be created, and services-per-tunnel when a new
    service would be added.
    """
    with handle_endpoint_errors():
        ctx = get_ctx()
        auth = authenticate_request(request, ctx.ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        validate_auth_policy_has_identity(body.auth_policy)
        tunnel_name = make_tunnel_name(admin.username, body.agent_id)
        enforce_tunnel_quota_for_new_tunnel(ctx.ops, admin.username, tunnel_name, entitlements)
        fallback = owner_email_auth_policy(admin.email) if admin.email else None
        tunnel_info = ctx.create_tunnel(
            admin.username,
            body.agent_id,
            default_auth_policy=None,
            fallback_auth_policy=fallback,
        )
        # ``create_tunnel`` already returned the tunnel's current services
        # (empty for a fresh tunnel), so no extra Cloudflare fetch is needed.
        enforce_service_quota(tunnel_info.services, body.service_name, entitlements.max_services_per_tunnel)
        service = ctx.add_service(
            tunnel_name,
            admin.username,
            body.service_name,
            body.service_url,
            fallback_policy=fallback,
            service_policy=body.auth_policy,
        )
        return {"tunnel": tunnel_info.model_dump(), "service": service.model_dump()}


# ---------------------------------------------------------------------------
# Host pool endpoints
# ---------------------------------------------------------------------------


@web_app.post("/hosts/lease")
def lease_host(request: Request, body: LeaseHostRequest) -> dict[str, object]:
    """Lease an available host from the pool, injecting the caller's SSH public key.

    Enforces the account's remote-workspace quota strictly: a per-user
    advisory lock (held for the lease transaction) serializes concurrent
    leases so two simultaneous requests cannot both squeeze past the count
    check. Stopped workspaces still hold their lease (and their slice), so
    they count against the quota too.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize this user's leases for the duration of the
                    # transaction, then enforce the workspace quota. The
                    # advisory lock releases automatically at commit/rollback.
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (admin.username,))
                    cur.execute(_COUNT_LEASED_HOSTS_SQL, (admin.username,))
                    count_row = cur.fetchone()
                    leased_count = int(count_row[0]) if count_row is not None else 0
                    if leased_count >= entitlements.max_remote_workspaces:
                        raise_quota_exceeded(
                            "max_remote_workspaces",
                            entitlements.max_remote_workspaces,
                            leased_count,
                            "remote workspaces",
                        )
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
    """Release a leased host: destroy its slice lima VM, then drop the row.

    Runs the full cleanup chain inline and **synchronously**: flip the row to
    ``removing`` (the durable, retryable in-progress marker), destroy the slice's
    lima VM on its bare-metal box, then delete the row.

    Returns 200 only once *every* step has succeeded -- a "released" result
    truly means the VM is destroyed. If any teardown step fails, the row stays
    ``removing`` and the endpoint returns an error (5xx) so the client retries;
    we never report success on a failed teardown. A failure before ``removing``
    is committed (lookup, ownership, the status flip) surfaces as an error too.

    Idempotent at the HTTP layer: a release on a row that is already gone
    (deleted) or no longer leased returns 200 ``status: already_released``.
    Ownership is still enforced -- a row leased by another user returns 403.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                # ``str(host_db_id)`` because psycopg2 can't adapt the
                # Python ``UUID`` type that FastAPI parsed from the path
                # (it raises "can't adapt type 'UUID'").
                cur.execute(
                    "SELECT leased_to_user, status, "
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
            # Past the commit point: the row is durably ``removing``. A teardown
            # failure below leaves the row ``removing`` and surfaces a 5xx so the
            # client retries.
            _finish_releasing_pool_host(
                conn,
                host_db_id,
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
    lima_instance_name: str | None,
    lima_disk_name: str | None,
    bare_metal_server_id: Any,
) -> None:
    """Destroy a slice's lima VM (host already marked ``removing``), then delete the row.

    **Raises** on any failure rather than swallowing it -- the caller has already
    committed the row to ``removing`` (a durable, retryable in-progress marker), so
    a failure here propagates to the HTTP layer: the release reports failure, the
    row stays ``removing``, and the client retries. A release that cannot actually
    destroy the slice VM must never report success.
    """
    clean_up_slice_on_box(conn, host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name)
    _delete_pool_host_row(conn, host_db_id)


@web_app.post("/hosts/{host_db_id}/rename")
def rename_host(request: Request, host_db_id: UUID, body: RenameHostRequest) -> dict[str, object]:
    """Rename a leased host: update the mutable ``host_name`` column on its row.

    The lease's ``host_db_id`` is the durable identity; only the friendly
    ``host_name`` changes, so a rename never touches the VPS/VM or the lease
    state. Ownership is enforced (a row leased by another user returns 403);
    a missing or not-leased row returns 404. ``host_name`` is validated by the
    request model against mngr's SafeName regex.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT leased_to_user, status FROM pool_hosts WHERE id = %s",
                    (str(host_db_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="No such host")
                leased_to_user, status = row
                # Ownership check first, to avoid leaking a status signal.
                if leased_to_user != admin.username:
                    raise HTTPException(status_code=403, detail="You do not own this host lease")
                if status != "leased":
                    raise HTTPException(status_code=404, detail="Host is not currently leased")
                cur.execute(
                    "UPDATE pool_hosts SET host_name = %s WHERE id = %s",
                    (body.host_name, str(host_db_id)),
                )
                conn.commit()
        finally:
            conn.close()
        return RenameHostResponse(host_db_id=host_db_id, host_name=body.host_name).model_dump()


@web_app.get("/hosts")
def list_leased_hosts(request: Request) -> list[dict[str, object]]:
    """List all hosts currently leased by the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
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
        # If this email already has an account, verify it now so the just-granted
        # paid access is not blocked by an unverified email. Best-effort: never
        # let a verification hiccup fail the paid-list write.
        _mark_paid_email_verified_best_effort(email)
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
    """Create a new LiteLLM virtual key for the authenticated user.

    Refused with a quota error when the account's monthly LLM budget is zero
    (e.g. the explorer plan -- pick 'subscription' as the AI provider
    instead). Otherwise the account's user-level LiteLLM budget is upserted
    before the key is minted, so aggregate spend across every key is capped
    at the account's monthly quota by the time any key exists. Per-key
    budgets remain entirely caller-controlled.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        if entitlements.monthly_llm_spend_usd <= 0:
            raise QuotaExceededError(
                entitlement="monthly_llm_spend_usd",
                limit=entitlements.monthly_llm_spend_usd,
                current=0,
                message=(
                    "This account's plan has no LLM spend budget, so imbue-cloud inference keys cannot be "
                    "created. Select 'subscription' (or your own API key) as the AI provider instead."
                ),
            )
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)
        upsert_litellm_user_budget(user_id, entitlements.monthly_llm_spend_usd)

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
        require_admin(auth)
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
        require_admin(auth)
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
        require_admin(auth)
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
        require_admin(auth)
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


_R2_KEY_COLUMNS = "access_key_id, owner_user_id, bucket_name, access, alias, created_at, enforced_access"


def _r2_key_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "access_key_id": row[0],
        "owner_user_id": row[1],
        "bucket_name": row[2],
        "access": row[3],
        "alias": row[4],
        "created_at": str(row[5]) if row[5] is not None else "",
        "enforced_access": row[6],
    }


class KeyStore(Protocol):
    """Abstraction over the r2_keys table so endpoints are unit-testable."""

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None: ...
    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]: ...
    def list_all_keys(self) -> list[dict[str, Any]]: ...
    def get_key(self, access_key_id: str) -> dict[str, Any] | None: ...
    def delete_key(self, access_key_id: str) -> None: ...
    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]: ...
    def set_enforced_access(self, access_key_id: str, enforced_access: str | None) -> None: ...


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

    def list_all_keys(self) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_R2_KEY_COLUMNS} FROM r2_keys ORDER BY owner_user_id, bucket_name, created_at")
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

    def set_enforced_access(self, access_key_id: str, enforced_access: str | None) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE r2_keys SET enforced_access = %s WHERE access_key_id = %s",
                        (enforced_access, access_key_id),
                    )
        finally:
            conn.close()

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
# R2 cleanup grants (r2_cleanup_grants table)
#
# A cleanup grant temporarily restores an over-quota account's downgraded
# bucket keys to readwrite so client-side restic cleanup (forget + prune,
# which needs full write -- prune repacks) can run. The grant settles at an
# explicit recheck or, as a fallback, when the sweep finds it expired; a
# grant that settles without any usage decrease counts against a rolling
# failed-grant budget, so genuine cleanup is unlimited while write-under-
# cover-of-cleanup abuse is bounded.
# ---------------------------------------------------------------------------


# How long a cleanup grant stays active before the sweep settles it as the
# fallback (the client's recheck normally settles it much sooner).
_R2_CLEANUP_GRANT_EXPIRY_MINUTES: Final = 60
# How many settled-without-decrease grants an account may burn per window.
_R2_CLEANUP_GRANT_FAILED_BUDGET: Final = 5
_R2_CLEANUP_GRANT_WINDOW_HOURS: Final = 24

_R2_GRANT_COLUMNS = "grant_id, user_id, username_prefix, baseline_bytes, granted_at, expires_at, settled_at, settled_bytes, is_decreased"


def _r2_grant_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "grant_id": int(row[0]),
        "user_id": row[1],
        "username_prefix": row[2],
        "baseline_bytes": int(row[3]),
        "granted_at": str(row[4]),
        "expires_at": str(row[5]),
        "settled_at": str(row[6]) if row[6] is not None else None,
        "settled_bytes": int(row[7]) if row[7] is not None else None,
        "is_decreased": row[8],
    }


class GrantStore(Protocol):
    """Abstraction over the r2_cleanup_grants table so endpoints are unit-testable."""

    def create_grant(
        self, user_id: str, username_prefix: str, baseline_bytes: int, expiry_minutes: int
    ) -> dict[str, Any]: ...
    def get_active_grant(self, user_id: str) -> dict[str, Any] | None: ...
    def list_unsettled_grants(self, user_id: str) -> list[dict[str, Any]]: ...
    def list_expired_unsettled_grants(self) -> list[dict[str, Any]]: ...
    def settle_grant(self, grant_id: int, settled_bytes: int, is_decreased: bool) -> None: ...
    def count_failed_grants_in_window(self, user_id: str, window_hours: int) -> int: ...


class PostgresGrantStore:
    """GrantStore backed by the connector's existing Neon DB (all timestamps are DB NOW())."""

    def create_grant(
        self, user_id: str, username_prefix: str, baseline_bytes: int, expiry_minutes: int
    ) -> dict[str, Any]:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO r2_cleanup_grants (user_id, username_prefix, baseline_bytes, expires_at) "
                        f"VALUES (%s, %s, %s, NOW() + make_interval(mins => %s)) RETURNING {_R2_GRANT_COLUMNS}",
                        (user_id, username_prefix, baseline_bytes, expiry_minutes),
                    )
                    row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=500, detail="Failed to record the cleanup grant")
        return _r2_grant_row_to_dict(row)

    def get_active_grant(self, user_id: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NULL AND expires_at > NOW() "
                    "ORDER BY granted_at DESC LIMIT 1",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return _r2_grant_row_to_dict(row) if row is not None else None

    def list_unsettled_grants(self, user_id: str) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NULL ORDER BY granted_at",
                    (user_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_r2_grant_row_to_dict(row) for row in rows]

    def list_expired_unsettled_grants(self) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_R2_GRANT_COLUMNS} FROM r2_cleanup_grants "
                    "WHERE settled_at IS NULL AND expires_at <= NOW() ORDER BY granted_at",
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_r2_grant_row_to_dict(row) for row in rows]

    def settle_grant(self, grant_id: int, settled_bytes: int, is_decreased: bool) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE r2_cleanup_grants SET settled_at = NOW(), settled_bytes = %s, is_decreased = %s "
                        "WHERE grant_id = %s AND settled_at IS NULL",
                        (settled_bytes, is_decreased, grant_id),
                    )
        finally:
            conn.close()

    def count_failed_grants_in_window(self, user_id: str, window_hours: int) -> int:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM r2_cleanup_grants "
                    "WHERE user_id = %s AND settled_at IS NOT NULL AND is_decreased = FALSE "
                    "AND granted_at > NOW() - make_interval(hours => %s)",
                    (user_id, window_hours),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return int(row[0]) if row is not None else 0


@functools.cache
def get_grant_store() -> GrantStore:
    return PostgresGrantStore()


@contextlib.contextmanager
def _r2_enforcement_lock(owner_user_id: str) -> Iterator[None]:
    """Hold a per-owner advisory lock while flipping bucket-key token policies.

    Serializes the sweep, cleanup grants, and rechecks for one owner so
    overlapping runs cannot interleave Cloudflare policy writes with the
    ``enforced_access`` bookkeeping (same xact-lock pattern as the lease
    path's per-user serialization).
    """
    conn = _get_pool_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"r2-enforce:{owner_user_id}",))
            yield
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# R2 bucket endpoints
# ---------------------------------------------------------------------------


def _list_owned_buckets(ops: CloudflareOps, username: str) -> list[dict[str, Any]]:
    """List the caller's buckets: R2 name_contains filter, then re-verify the prefix in code."""
    prefix = bucket_owner_prefix(username)
    return [b for b in ops.list_buckets(name_contains=prefix) if str(b.get("name", "")).startswith(prefix)]


def _owned_bucket_exists(ops: CloudflareOps, username: str, full_name: str) -> bool:
    return any(b.get("name") == full_name for b in _list_owned_buckets(ops, username))


# Bound on simultaneous per-bucket usage REST calls. Reads were previously
# sequential, which made every live-usage measurement O(bucket_count) in
# Cloudflare round trips (~0.45s each -- ~19s for a 42-bucket account).
_BUCKET_USAGE_MAX_PARALLEL_READS: Final = 8


def _read_one_bucket_usage_bytes(ops: CloudflareOps, bucket_name: str) -> "int | CloudflareApiError | httpx.HTTPError":
    """Read one bucket's live usage bytes, returning (not raising) a failed read's exception."""
    try:
        return ops.get_bucket_usage_bytes(bucket_name)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        return exc


def _read_bucket_usage_bytes_concurrently(
    ops: CloudflareOps, bucket_names: list[str]
) -> "list[int | CloudflareApiError | httpx.HTTPError]":
    """Read each bucket's live usage bytes via concurrent REST calls.

    Results align positionally with ``bucket_names``. A failed read yields its
    exception instead of raising, so each caller keeps its own error
    semantics (display warns and counts zero; enforcement raises).
    """
    if not bucket_names:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_BUCKET_USAGE_MAX_PARALLEL_READS, len(bucket_names))
    ) as pool:
        futures = [pool.submit(_read_one_bucket_usage_bytes, ops, bucket_name) for bucket_name in bucket_names]
        return [future.result() for future in futures]


def _measure_live_owner_usage_bytes(ops: CloudflareOps, username_prefix: str) -> int:
    """Sum the owner's bucket bytes via the real-time REST usage endpoint.

    Raises :class:`CloudflareApiError` / ``httpx.HTTPError`` on any failed
    read -- callers decide whether that fails open (sweep, creation gate) or
    fails the request (grant baseline, recheck).
    """
    bucket_names = [str(bucket.get("name", "")) for bucket in _list_owned_buckets(ops, username_prefix)]
    total_bytes = 0
    for result in _read_bucket_usage_bytes_concurrently(ops, bucket_names):
        if isinstance(result, (CloudflareApiError, httpx.HTTPError)):
            raise result
        total_bytes += result
    return total_bytes


def _is_owner_enforced_over_quota(store: KeyStore, owner_user_id: str) -> bool:
    """True when any of the owner's keys is currently sweep-downgraded (enforced read-only)."""
    return any(row.get("enforced_access") == "read" for row in store.list_keys(owner_user_id, None))


def _check_storage_quota_for_new_bucket(ops: CloudflareOps, username: str, entitlements: AccountEntitlements) -> None:
    """Refuse bucket creation when the owner's live storage usage is already over quota.

    A failed usage read fails open (creation proceeds with a warning),
    consistent with the sweep's missing-data-never-downgrades rule.
    """
    try:
        live_bytes = _measure_live_owner_usage_bytes(ops, username)
    except (CloudflareApiError, httpx.HTTPError) as exc:
        logger.warning("Skipped the storage-quota check for bucket creation (usage read failed): %s", exc)
        return
    if live_bytes > entitlements.max_total_bucket_bytes:
        raise_quota_exceeded(
            "max_total_bucket_bytes", entitlements.max_total_bucket_bytes, live_bytes, "bytes of bucket storage"
        )


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
        enforced_access=row.get("enforced_access"),
    )


def _mint_and_record_key(
    ops: CloudflareOps,
    store: KeyStore,
    owner_user_id: str,
    bucket_name: str,
    access: str,
    alias: str | None,
    rollback_bucket: bool,
    # When the owner is currently enforced-over-quota, a readwrite key is
    # minted with a read-only token policy and recorded as enforced -- a
    # fresh mint must not hand out a writable key the sweep already denies.
    is_enforced_read: bool,
) -> R2KeyMaterial:
    """Mint a bucket-scoped Cloudflare token, record its metadata, and return the S3 material.

    On any failure, best-effort revokes a partially-created token and (when
    ``rollback_bucket``) deletes the just-created bucket so ``bucket create``
    stays atomic.
    """
    minted_access = "read" if is_enforced_read and access == "readwrite" else access
    created_token_id: str | None = None
    try:
        token_result = ops.create_bucket_token(bucket_name, minted_access, _r2_token_name(bucket_name, alias))
        access_key_id = str(token_result["id"])
        created_token_id = access_key_id
        secret_access_key = derive_s3_secret_access_key(str(token_result["value"]))
        store.add_key(access_key_id, owner_user_id, bucket_name, access, alias)
        if minted_access != access:
            store.set_enforced_access(access_key_id, "read")
        return R2KeyMaterial(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            s3_endpoint=r2_s3_endpoint(ops.account_id),
            bucket_name=bucket_name,
            access=minted_access,
        )
    except (CloudflareApiError, httpx.HTTPError, psycopg2.Error) as exc:
        if created_token_id is not None:
            _best_effort_revoke_token(ops, created_token_id)
        if rollback_bucket:
            _best_effort_delete_bucket(ops, bucket_name)
        raise HTTPException(status_code=502, detail=f"Failed to provision bucket key: {exc}") from exc


@web_app.post("/buckets")
def create_bucket_endpoint(request: Request, body: CreateBucketRequest) -> dict[str, object]:
    """Create an R2 bucket for the caller and mint its single key (returned inline)."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, body.name)
        owned = _list_owned_buckets(ops, admin.username)
        if any(b.get("name") == full_name for b in owned):
            raise R2BucketExistsError(full_name)
        if len(owned) >= entitlements.max_buckets:
            raise_quota_exceeded("max_buckets", entitlements.max_buckets, len(owned), "buckets")
        _check_storage_quota_for_new_bucket(ops, admin.username, entitlements)
        store = get_key_store()
        ops.create_bucket(full_name)
        material = _mint_and_record_key(
            ops,
            store,
            owner_user_id,
            full_name,
            body.access,
            _DEFAULT_R2_KEY_ALIAS,
            rollback_bucket=True,
            is_enforced_read=_is_owner_enforced_over_quota(store, owner_user_id),
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
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, name)
        verify_bucket_ownership(full_name, admin.username)
        ops.delete_bucket(full_name)
        revoked = get_key_store().delete_keys_for_bucket(owner_user_id, full_name)
        for row in revoked:
            _best_effort_revoke_token(ops, str(row["access_key_id"]))
        return {"status": "deleted"}


@web_app.post("/buckets/{name}/roll-key")
def roll_bucket_key_endpoint(request: Request, name: str) -> dict[str, object]:
    """Return fresh credentials for a bucket's single key by rolling its secret in place.

    Each bucket has exactly one key. The secret is derived from the
    Cloudflare token value and is shown only once, so re-provisioning
    (e.g. minds re-applying backups) rolls the existing token's value --
    same Access Key ID, fresh Secret Access Key, and, crucially, the
    token's *policies* are untouched, so a storage-quota downgrade
    survives a roll. When the bucket has no recorded key (revoked, or
    a legacy bucket), a fresh key is minted instead.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        ops = get_ctx().ops
        full_name = make_bucket_name(admin.username, name)
        if not _owned_bucket_exists(ops, admin.username, full_name):
            raise R2BucketNotFoundError(full_name)
        store = get_key_store()
        rows = store.list_keys(owner_user_id, full_name)
        if not rows:
            material = _mint_and_record_key(
                ops,
                store,
                owner_user_id,
                full_name,
                "readwrite",
                _DEFAULT_R2_KEY_ALIAS,
                rollback_bucket=False,
                is_enforced_read=_is_owner_enforced_over_quota(store, owner_user_id),
            )
            return material.model_dump()
        # The sweep enforces single-key-per-bucket; if extras still exist
        # (pre-sweep), roll the newest -- the sweep will revoke the rest.
        newest = rows[-1]
        result = ops.roll_bucket_token_value(str(newest["access_key_id"]))
        secret_access_key = derive_s3_secret_access_key(str(result["value"]))
        effective_access = str(newest.get("enforced_access") or newest["access"])
        return R2KeyMaterial(
            access_key_id=str(newest["access_key_id"]),
            secret_access_key=secret_access_key,
            s3_endpoint=r2_s3_endpoint(ops.account_id),
            bucket_name=full_name,
            access=effective_access,
        ).model_dump()


@web_app.get("/buckets/{name}/keys")
def list_bucket_keys_endpoint(request: Request, name: str) -> list[dict[str, object]]:
    """List the caller's keys scoped to one bucket."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        full_name = make_bucket_name(admin.username, name)
        rows = get_key_store().list_keys(owner_user_id, full_name)
        return [_key_info_from_row(row).model_dump() for row in rows]


@web_app.get("/bucket-keys")
def list_all_bucket_keys_endpoint(request: Request) -> list[dict[str, object]]:
    """List all of the caller's bucket keys across every bucket."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        rows = get_key_store().list_keys(owner_user_id, None)
        return [_key_info_from_row(row).model_dump() for row in rows]


@web_app.delete("/bucket-keys/{access_key_id}")
def delete_bucket_key_endpoint(request: Request, access_key_id: str) -> dict[str, str]:
    """Revoke one of the caller's bucket keys (by Access Key ID) and drop its DB row."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        require_admin(auth)
        owner_user_id = _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])
        store = get_key_store()
        row = store.get_key(access_key_id)
        if row is None or row["owner_user_id"] != owner_user_id:
            raise HTTPException(status_code=404, detail="Key not found")
        get_ctx().ops.delete_bucket_token(access_key_id)
        store.delete_key(access_key_id)
        return {"status": "deleted"}


# ---------------------------------------------------------------------------
# R2 storage-cleanup grants + recheck endpoints
# ---------------------------------------------------------------------------


@web_app.post("/account/storage-cleanup-grant")
def create_storage_cleanup_grant(request: Request) -> dict[str, object]:
    """Temporarily restore the caller's sweep-downgraded bucket keys for client-side cleanup.

    restic cleanup (forget + prune) needs full write access -- prune repacks
    data, so no permission level allows delete-but-not-put. The grant flips
    the downgraded keys back to readwrite; it settles at the caller's
    /account/storage-recheck (or at expiry via the sweep), and only grants
    that settle without ANY usage decrease burn the rolling failed-grant
    budget. Idempotent: an active grant is returned as-is (flipping any keys
    still downgraded), and an account with nothing downgraded gets a
    'not_needed' no-op.
    """
    with handle_endpoint_errors():
        ops = get_ctx().ops
        auth = authenticate_request(request, ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        key_store = get_key_store()
        grant_store = get_grant_store()
        counters = {"keys_downgraded": 0, "keys_restored": 0, "key_update_failures": 0}
        with _r2_enforcement_lock(entitlements.user_id):
            rows = key_store.list_keys(entitlements.user_id, None)
            active_grant = grant_store.get_active_grant(entitlements.user_id)
            is_any_key_downgraded = any(row.get("enforced_access") == "read" for row in rows)
            if active_grant is None and not is_any_key_downgraded:
                return CleanupGrantResponse(
                    status="not_needed", keys=[_key_info_from_row(row) for row in rows]
                ).model_dump()
            if active_grant is None:
                failed_count = grant_store.count_failed_grants_in_window(
                    entitlements.user_id, _R2_CLEANUP_GRANT_WINDOW_HOURS
                )
                if failed_count >= _R2_CLEANUP_GRANT_FAILED_BUDGET:
                    raise CleanupGrantBudgetExhaustedError(
                        limit=_R2_CLEANUP_GRANT_FAILED_BUDGET,
                        current=failed_count,
                        window_hours=_R2_CLEANUP_GRANT_WINDOW_HOURS,
                    )
                baseline_bytes = _measure_live_owner_usage_bytes(ops, admin.username)
                active_grant = grant_store.create_grant(
                    entitlements.user_id, admin.username, baseline_bytes, _R2_CLEANUP_GRANT_EXPIRY_MINUTES
                )
            # Restore every still-downgraded key (is_over_quota=False path).
            _enforce_owner_key_access(ops, key_store, rows, False, counters)
            refreshed_rows = key_store.list_keys(entitlements.user_id, None)
        return CleanupGrantResponse(
            status="granted",
            expires_at=str(active_grant["expires_at"]),
            baseline_bytes=int(active_grant["baseline_bytes"]),
            keys=[_key_info_from_row(row) for row in refreshed_rows],
        ).model_dump()


@web_app.post("/account/storage-recheck")
def recheck_storage_enforcement(request: Request) -> dict[str, object]:
    """Re-measure the caller's live storage usage and apply enforcement immediately.

    Works standalone (a user who freed space any other way gets their keys
    restored without waiting for the hourly sweep) and doubles as the
    settlement point for an outstanding cleanup grant: settled usage below
    the grant's baseline -- any decrease -- marks the grant successful.
    Reads the same real-time REST usage the sweep's downgrade confirmation
    uses, so this endpoint and the sweep can never disagree about the same
    measurement.
    """
    with handle_endpoint_errors():
        ops = get_ctx().ops
        auth = authenticate_request(request, ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        key_store = get_key_store()
        grant_store = get_grant_store()
        counters = {"keys_downgraded": 0, "keys_restored": 0, "key_update_failures": 0}
        with _r2_enforcement_lock(entitlements.user_id):
            live_bytes = _measure_live_owner_usage_bytes(ops, admin.username)
            is_over_quota = live_bytes > entitlements.max_total_bucket_bytes
            unsettled_grants = grant_store.list_unsettled_grants(entitlements.user_id)
            for grant in unsettled_grants:
                grant_store.settle_grant(int(grant["grant_id"]), live_bytes, live_bytes < int(grant["baseline_bytes"]))
            rows = key_store.list_keys(entitlements.user_id, None)
            _enforce_owner_key_access(ops, key_store, rows, is_over_quota, counters)
            refreshed_rows = key_store.list_keys(entitlements.user_id, None)
        return StorageRecheckResponse(
            usage_bytes=live_bytes,
            limit_bytes=entitlements.max_total_bucket_bytes,
            is_over_quota=is_over_quota,
            is_grant_settled=bool(unsettled_grants),
            keys=[_key_info_from_row(row) for row in refreshed_rows],
        ).model_dump()


# ---------------------------------------------------------------------------
# R2 storage-quota sweep
#
# Hourly cron: reads every bucket's peak stored bytes from the GraphQL
# analytics dataset (one query per sweep regardless of bucket count, one row
# per bucket), sums per owner, and flips bucket-key token policies in place --
# readwrite keys of an over-quota owner become read-only (same S3
# credentials, so reads keep working while writes fail), and are restored
# automatically once the owner is back under quota. The GraphQL number is a
# lookback-window *peak*, so it is only a screening filter: before any
# downgrade the owner is re-measured with the real-time REST usage endpoint
# (the same source the grant/recheck endpoints read), which makes the sweep
# and an out-of-band restore unable to disagree. The sweep also settles
# expired cleanup grants, skips owners with an active grant (so a mid-prune
# measurement never re-locks them), and permanently enforces the
# single-key-per-bucket invariant: any bucket with more than one recorded key
# has the extras revoked (newest wins), which doubles as the one-time cleanup
# of multi-key buckets minted before this model.
# ---------------------------------------------------------------------------


def _sweep_owner_email(user_id: str, email_getter: Callable[[str], str | None]) -> str | None:
    """Best-effort verified-email lookup for the sweep's lazy row creation."""
    try:
        return email_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Sweep could not resolve email for user %s: %s", user_id[:8], exc)
        return None


def _revoke_extra_bucket_keys(
    ops: CloudflareOps,
    key_store: KeyStore,
    counters: dict[str, int],
    # When set, only this owner's keys are considered (the email-scoped admin sweep).
    only_user_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Enforce the single-key-per-bucket invariant; returns the surviving keys grouped by owner.

    The newest key per (owner, bucket) survives; extras are revoked and their
    rows dropped, counted in ``counters["extra_keys_revoked"]``. The row is
    dropped only after a successful Cloudflare revoke: the ``r2_keys`` table
    is the sole record of keys, so dropping the row of a still-live token
    would orphan a credential no later sweep could revoke or downgrade. A
    failed revoke is logged, counted in ``counters["key_update_failures"]``,
    and retried on the next sweep.
    """
    keys_by_owner: dict[str, list[dict[str, Any]]] = {}
    keys_by_owner_bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in key_store.list_all_keys():
        if only_user_id is not None and str(row["owner_user_id"]) != only_user_id:
            continue
        keys_by_owner_bucket.setdefault((str(row["owner_user_id"]), str(row["bucket_name"])), []).append(row)
    for (owner_user_id, _bucket_name), rows in keys_by_owner_bucket.items():
        ordered = sorted(rows, key=lambda r: str(r["created_at"]))
        for extra in ordered[:-1]:
            access_key_id = str(extra["access_key_id"])
            try:
                ops.delete_bucket_token(access_key_id)
            except (CloudflareApiError, httpx.HTTPError) as exc:
                logger.error("Sweep failed to revoke extra key %s: %s", access_key_id, exc)
                counters["key_update_failures"] += 1
                continue
            key_store.delete_key(access_key_id)
            counters["extra_keys_revoked"] += 1
        keys_by_owner.setdefault(owner_user_id, []).append(ordered[-1])
    return keys_by_owner


def _resolve_owner_storage_limit_bytes(
    owner_user_id: str,
    owner_prefix: str,
    entitlements_store: EntitlementsStore,
    email_getter: Callable[[str], str | None],
) -> int | None:
    """Resolve the owner's storage limit, lazily creating their entitlements row when needed.

    Mirrors the request-path rule (paid pre-cutoff accounts land on ally).
    Returns ``None`` for an unresolvable owner (no row and no verified email)
    -- the sweep must skip them, never enforce against guessed limits.
    """
    existing = entitlements_store.get_entitlements(owner_user_id)
    if existing is not None:
        return int(existing["max_total_bucket_bytes"])
    email = _sweep_owner_email(owner_user_id, email_getter)
    if email is None:
        return None
    entitlements = ensure_account_entitlements(
        user_id=owner_user_id, username_prefix=owner_prefix, email=email, store=entitlements_store
    )
    return entitlements.max_total_bucket_bytes


def _enforce_owner_key_access(
    ops: CloudflareOps,
    key_store: KeyStore,
    rows: list[dict[str, Any]],
    is_over_quota: bool,
    counters: dict[str, int],
) -> None:
    """Downgrade (or restore) one owner's bucket-key token policies around the storage quota.

    A failed Cloudflare token update is logged and counted, skipping only
    that key.
    """
    for row in rows:
        access_key_id = str(row["access_key_id"])
        bucket_name = str(row["bucket_name"])
        token_name = _r2_token_name(bucket_name, row.get("alias"))
        try:
            if is_over_quota and row["access"] == "readwrite" and row.get("enforced_access") != "read":
                ops.update_bucket_token_access(access_key_id, bucket_name, "read", token_name)
                key_store.set_enforced_access(access_key_id, "read")
                counters["keys_downgraded"] += 1
            elif not is_over_quota and row.get("enforced_access") is not None:
                ops.update_bucket_token_access(access_key_id, bucket_name, str(row["access"]), token_name)
                key_store.set_enforced_access(access_key_id, None)
                counters["keys_restored"] += 1
            else:
                # The key already reflects the desired state (intentionally
                # read-only, already downgraded, or already restored).
                pass
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.error("Sweep failed to update token %s for bucket %s: %s", access_key_id, bucket_name, exc)
            counters["key_update_failures"] += 1


def _settle_expired_grants(
    ops: CloudflareOps,
    grant_store: GrantStore,
    counters: dict[str, int],
    only_user_id: str | None,
) -> None:
    """Settle cleanup grants whose expiry passed without an explicit recheck.

    Settlement measures live usage via the REST endpoint (the same source the
    grant's baseline came from); a failed read skips only that grant, which
    stays unsettled and is retried next pass.
    """
    for grant in grant_store.list_expired_unsettled_grants():
        if only_user_id is not None and str(grant["user_id"]) != only_user_id:
            continue
        try:
            live_bytes = _measure_live_owner_usage_bytes(ops, str(grant["username_prefix"]))
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.error("Sweep failed to settle grant %s (usage read failed): %s", grant["grant_id"], exc)
            counters["grant_settle_failures"] += 1
            continue
        grant_store.settle_grant(int(grant["grant_id"]), live_bytes, live_bytes < int(grant["baseline_bytes"]))
        counters["grants_settled"] += 1


def run_r2_quota_sweep(
    ops: CloudflareOps,
    key_store: KeyStore,
    entitlements_store: EntitlementsStore,
    grant_store: GrantStore,
    email_getter: Callable[[str], str | None] = _default_email_getter,
    enforcement_lock: Callable[[str], contextlib.AbstractContextManager[None]] = _r2_enforcement_lock,
    only_user_id: str | None = None,
) -> dict[str, int]:
    """Run one storage-quota sweep pass; returns counters for the cron log.

    Fails loudly (raises) when the account-wide usage query fails or fills
    its row budget -- a sweep that cannot see usage must not look like a
    clean pass. Per-user failures (email lookup, a Cloudflare token update)
    are logged and skip only that user/key, and an unknown limit skips the
    user entirely. Missing data never *downgrades* a key: a bucket absent
    from the analytics window counts as zero usage, and a downgrade is only
    applied after the real-time REST usage confirms the account is over its
    limit (the GraphQL peak alone can only restore or screen).
    """
    counters = {
        "extra_keys_revoked": 0,
        "users_over_quota": 0,
        "keys_downgraded": 0,
        "keys_restored": 0,
        "users_skipped": 0,
        "users_skipped_for_grant": 0,
        "key_update_failures": 0,
        "grants_settled": 0,
        "grant_settle_failures": 0,
        "downgrades_cancelled_by_live_usage": 0,
        "live_usage_read_failures": 0,
    }

    # Enforce the single-key-per-bucket invariant first: newest key per
    # (owner, bucket) survives, extras are revoked + dropped.
    keys_by_owner = _revoke_extra_bucket_keys(ops, key_store, counters, only_user_id)

    # Settle grants whose expiry passed without a recheck (the fallback path;
    # a live client normally settles via /account/storage-recheck).
    _settle_expired_grants(ops, grant_store, counters, only_user_id)

    # One GraphQL query covers every bucket's peak stored bytes; a failure
    # (or a possibly-truncated full page) aborts the sweep by raising rather
    # than being mistaken for zero usage.
    usage_by_bucket = ops.query_r2_storage_by_bucket()
    all_buckets = [str(b.get("name", "")) for b in ops.list_buckets()]

    for owner_user_id, rows in keys_by_owner.items():
        # An active grant means client-side cleanup may be mid-prune (which
        # transiently *increases* usage); leave the owner alone until the
        # grant settles.
        if grant_store.get_active_grant(owner_user_id) is not None:
            counters["users_skipped_for_grant"] += 1
            continue

        owner_prefix = str(rows[0]["bucket_name"]).split(_R2_BUCKET_NAME_SEP, 1)[0]
        bucket_prefix = f"{owner_prefix}{_R2_BUCKET_NAME_SEP}"
        owner_buckets = [name for name in all_buckets if name.startswith(bucket_prefix)]
        owner_peak_bytes = sum(usage_by_bucket.get(name, 0) for name in owner_buckets)

        limit_bytes = _resolve_owner_storage_limit_bytes(owner_user_id, owner_prefix, entitlements_store, email_getter)
        if limit_bytes is None:
            logger.error(
                "Sweep skipping user %s: no resolvable verified email for lazy plan assignment", owner_user_id[:8]
            )
            counters["users_skipped"] += 1
            continue

        # The peak over the lookback window screens candidates; peak under
        # the limit proves live usage is under (restores need no confirm).
        # Over-peak owners are re-measured with the real-time REST endpoint
        # so a user who just cleaned up is never re-downgraded on stale data.
        is_over_quota = owner_peak_bytes > limit_bytes
        if is_over_quota:
            try:
                live_bytes = _measure_live_owner_usage_bytes(ops, owner_prefix)
            except (CloudflareApiError, httpx.HTTPError) as exc:
                logger.error("Sweep skipping user %s: live usage read failed: %s", owner_user_id[:8], exc)
                counters["live_usage_read_failures"] += 1
                counters["users_skipped"] += 1
                continue
            if live_bytes <= limit_bytes:
                counters["downgrades_cancelled_by_live_usage"] += 1
            is_over_quota = live_bytes > limit_bytes

        if is_over_quota:
            counters["users_over_quota"] += 1
        with enforcement_lock(owner_user_id):
            # Re-check under the lock before downgrading: a cleanup grant may
            # have been created (restoring the keys under this same lock)
            # between the loop-top check and lock acquisition, and a
            # downgrade here would break the mid-cleanup guarantee. Restores
            # need no re-check -- restoring is exactly what a grant wants.
            if is_over_quota and grant_store.get_active_grant(owner_user_id) is not None:
                counters["users_skipped_for_grant"] += 1
                continue
            _enforce_owner_key_access(ops, key_store, rows, is_over_quota, counters)
    return counters


# ---------------------------------------------------------------------------
# Workspace sync endpoints (records + account key bundles)
#
# Per-account workspace records: plaintext metadata (name, color, provider,
# location, lifecycle state) plus an opaque, client-side-encrypted secrets
# blob the server can never read. Writes are compare-and-swap on a per-row
# revision counter. The account key bundle holds the argon2id inputs and the
# password-wrapped data-encryption key (also opaque). All endpoints require
# admin (SuperTokens) auth but are NOT paid-gated -- sync is a free feature.
# ---------------------------------------------------------------------------


# Hard caps on what one sync row may carry. These exist to bound a row's size
# (the server can never read the blobs, so it cannot validate their contents)
# -- not to police the payload's shape. Today's payload uses a small fraction
# of each, and the headroom is deliberate: the secrets blob is an opaque,
# client-versioned envelope, so adding another secret to it later must not
# require a connector deploy to raise a limit.
#
# Client-encrypted secrets blob, decoded bytes. Today: an SSH private key +
# known_hosts + a canonical restic env (a few KiB).
_MAX_ENCRYPTED_SECRETS_BYTES = 2560 * 1024
# Each binary key-bundle field: the password-wrapped DEK (a 32-byte key +
# nonce + tag) and the argon2id salt. Today: under 100 bytes each.
_MAX_KEY_BUNDLE_FIELD_BYTES = 40960
# Each plaintext metadata field (names, ids, device labels).
_MAX_SYNC_TEXT_FIELD_LENGTH = 5120


class WorkspaceRecordState(str, Enum):
    """Lifecycle state of a synced workspace record (lowercase wire/DB values)."""

    ACTIVE = "active"
    DESTROYED = "destroyed"


class WorkspaceRecordModel(BaseModel):
    """Wire form of one synced workspace record (also the PUT body)."""

    host_id: str = Field(min_length=1, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Host the workspace is on")
    agent_id: str = Field(min_length=1, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Logical workspace id")
    display_name: str = Field(max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Workspace display name")
    color: str | None = Field(default=None, max_length=64, description="Workspace accent color (#rrggbb)")
    provider_kind: str = Field(
        max_length=_MAX_SYNC_TEXT_FIELD_LENGTH,
        description="mngr provider backend kind; empty when not yet known (create-path seed records)",
    )
    hosting_device_id: str | None = Field(
        default=None,
        max_length=_MAX_SYNC_TEXT_FIELD_LENGTH,
        description="Install that hosts a local workspace (None for cloud rows)",
    )
    device_label: str = Field(
        default="", max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Human-readable device name"
    )
    state: WorkspaceRecordState = Field(description="Lifecycle state; 'destroyed' is a tombstone")
    restored_from_host_id: str | None = Field(
        default=None, max_length=_MAX_SYNC_TEXT_FIELD_LENGTH, description="Lineage link for restored workspaces"
    )
    encrypted_secrets: str | None = Field(
        default=None, description="Base64 of the client-encrypted secrets blob (opaque to the server)"
    )
    revision: int = Field(ge=1, description="Per-row monotonic revision; PUT is CAS on this")
    created_at: str = Field(default="", description="Server timestamp (response only)")
    updated_at: str = Field(default="", description="Server timestamp (response only)")


class AccountKeyBundleModel(BaseModel):
    """Wire form of the per-account password-wrapped data key (also the PUT body)."""

    kdf_salt: str = Field(min_length=1, description="Base64 argon2id salt")
    kdf_time_cost: int = Field(gt=0, description="argon2id iteration count")
    kdf_memory_kib: int = Field(gt=0, description="argon2id memory (KiB)")
    kdf_parallelism: int = Field(gt=0, description="argon2id lane count")
    wrapped_dek: str = Field(min_length=1, description="Base64 password-wrapped DEK (opaque to the server)")
    key_epoch: int = Field(ge=1, description="Bumped only on compromise recovery")
    updated_at: str = Field(default="", description="Server timestamp (response only)")


class SyncRevisionConflictError(Exception):
    """CAS failure: the stored revision does not precede the pushed one."""

    def __init__(self, stored_record: dict[str, Any]) -> None:
        super().__init__("workspace record revision conflict")
        self.stored_record = stored_record


class SyncActiveAgentConflictError(Exception):
    """A second ACTIVE record for the same (user_id, agent_id) was rejected."""


class SyncStoreConsistencyError(RuntimeError):
    """The store violated one of its own invariants (e.g. a write returned no row)."""


_WORKSPACE_RECORD_COLUMNS = (
    "host_id, agent_id, display_name, color, provider_kind, hosting_device_id, device_label, "
    "state, restored_from_host_id, encrypted_secrets, revision, created_at, updated_at"
)

# Must match the index name in migrations/013_workspace_sync.sql; used to tell
# an active-agent conflict apart from a primary-key insert race.
_ONE_ACTIVE_PER_AGENT_INDEX_NAME = "workspace_records_one_active_per_agent_idx"


def _workspace_record_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    encrypted_secrets = row[9]
    return {
        "host_id": row[0],
        "agent_id": row[1],
        "display_name": row[2],
        "color": row[3],
        "provider_kind": row[4],
        "hosting_device_id": row[5],
        "device_label": row[6],
        "state": row[7],
        "restored_from_host_id": row[8],
        "encrypted_secrets": (
            base64.b64encode(bytes(encrypted_secrets)).decode("ascii") if encrypted_secrets is not None else None
        ),
        "revision": row[10],
        "created_at": str(row[11]) if row[11] is not None else "",
        "updated_at": str(row[12]) if row[12] is not None else "",
    }


class SyncStore(Protocol):
    """Abstraction over the workspace_records + account_key_bundles tables."""

    def list_records(self, user_id: str) -> list[dict[str, Any]]: ...
    def put_record(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
    def delete_record(self, user_id: str, host_id: str) -> None: ...
    def scrub_secrets(self, user_id: str) -> int: ...
    def get_bundle(self, user_id: str) -> dict[str, Any] | None: ...
    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None: ...
    def delete_bundle(self, user_id: str) -> None: ...


class PostgresSyncStore:
    """SyncStore backed by the connector's existing Neon DB (same DB as pool_hosts)."""

    def list_records(self, user_id: str) -> list[dict[str, Any]]:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_WORKSPACE_RECORD_COLUMNS} FROM workspace_records "
                    "WHERE user_id = %s ORDER BY created_at",
                    (user_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_workspace_record_row_to_dict(row) for row in rows]

    def put_record(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Insert or CAS-update one record; returns the stored row after the write.

        An update requires ``record["revision"] == stored revision + 1``;
        otherwise :class:`SyncRevisionConflictError` carries the stored row so
        the client can merge and retry. The partial unique index on
        ``(user_id, agent_id) WHERE state = 'active'`` surfaces as
        :class:`SyncActiveAgentConflictError`. Two concurrent *first* pushes
        of the same host_id both pass the FOR UPDATE probe and the loser's
        INSERT hits the primary key instead; by then the winner's row is
        committed, so one retry reports that race through the regular CAS
        path (409 + stored row) rather than as an agent conflict.
        """
        try:
            return self._put_record_once(user_id, record)
        except psycopg2.errors.UniqueViolation:
            return self._put_record_once(user_id, record)

    def _put_record_once(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_WORKSPACE_RECORD_COLUMNS} FROM workspace_records "
                        "WHERE user_id = %s AND host_id = %s FOR UPDATE",
                        (user_id, record["host_id"]),
                    )
                    existing = cur.fetchone()
                    encrypted = record["encrypted_secrets"]
                    encrypted_bytes = psycopg2.Binary(encrypted) if encrypted is not None else None
                    try:
                        if existing is None:
                            cur.execute(
                                "INSERT INTO workspace_records (user_id, host_id, agent_id, display_name, color, "
                                "provider_kind, hosting_device_id, device_label, state, restored_from_host_id, "
                                "encrypted_secrets, revision) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                                f"RETURNING {_WORKSPACE_RECORD_COLUMNS}",
                                (
                                    user_id,
                                    record["host_id"],
                                    record["agent_id"],
                                    record["display_name"],
                                    record["color"],
                                    record["provider_kind"],
                                    record["hosting_device_id"],
                                    record["device_label"],
                                    record["state"],
                                    record["restored_from_host_id"],
                                    encrypted_bytes,
                                    record["revision"],
                                ),
                            )
                        else:
                            stored = _workspace_record_row_to_dict(existing)
                            if record["revision"] != stored["revision"] + 1:
                                raise SyncRevisionConflictError(stored)
                            cur.execute(
                                "UPDATE workspace_records SET agent_id = %s, display_name = %s, color = %s, "
                                "provider_kind = %s, hosting_device_id = %s, device_label = %s, state = %s, "
                                "restored_from_host_id = %s, encrypted_secrets = %s, "
                                "revision = %s, updated_at = NOW() "
                                "WHERE user_id = %s AND host_id = %s "
                                f"RETURNING {_WORKSPACE_RECORD_COLUMNS}",
                                (
                                    record["agent_id"],
                                    record["display_name"],
                                    record["color"],
                                    record["provider_kind"],
                                    record["hosting_device_id"],
                                    record["device_label"],
                                    record["state"],
                                    record["restored_from_host_id"],
                                    encrypted_bytes,
                                    record["revision"],
                                    user_id,
                                    record["host_id"],
                                ),
                            )
                        written = cur.fetchone()
                    except psycopg2.errors.UniqueViolation as exc:
                        if exc.diag.constraint_name == _ONE_ACTIVE_PER_AGENT_INDEX_NAME:
                            raise SyncActiveAgentConflictError(
                                f"another ACTIVE record already exists for agent {record['agent_id']}"
                            ) from exc
                        # Any other unique violation (the primary key) is a
                        # concurrent-insert race; the caller retries once.
                        raise
        finally:
            conn.close()
        if written is None:
            # INSERT/UPDATE ... RETURNING on a locked, existing row always
            # yields a row; reaching here means the store broke its own
            # invariant, which must surface as a server error -- not as a 409
            # whose "stored" row would be the pushed record (whose secrets are
            # raw bytes at this point, not wire-shaped base64).
            raise SyncStoreConsistencyError(f"workspace record write for host {record['host_id']} returned no row")
        return _workspace_record_row_to_dict(written)

    def delete_record(self, user_id: str, host_id: str) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM workspace_records WHERE user_id = %s AND host_id = %s",
                        (user_id, host_id),
                    )
        finally:
            conn.close()

    def scrub_secrets(self, user_id: str) -> int:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE workspace_records SET encrypted_secrets = NULL, updated_at = NOW() "
                        "WHERE user_id = %s AND encrypted_secrets IS NOT NULL",
                        (user_id,),
                    )
                    scrubbed = cur.rowcount
        finally:
            conn.close()
        return scrubbed

    def get_bundle(self, user_id: str) -> dict[str, Any] | None:
        conn = _get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, wrapped_dek, key_epoch, "
                    "updated_at FROM account_key_bundles WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "kdf_salt": base64.b64encode(bytes(row[0])).decode("ascii"),
            "kdf_time_cost": row[1],
            "kdf_memory_kib": row[2],
            "kdf_parallelism": row[3],
            "wrapped_dek": base64.b64encode(bytes(row[4])).decode("ascii"),
            "key_epoch": row[5],
            "updated_at": str(row[6]) if row[6] is not None else "",
        }

    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO account_key_bundles (user_id, kdf_salt, kdf_time_cost, kdf_memory_kib, "
                        "kdf_parallelism, wrapped_dek, key_epoch) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET kdf_salt = EXCLUDED.kdf_salt, "
                        "kdf_time_cost = EXCLUDED.kdf_time_cost, kdf_memory_kib = EXCLUDED.kdf_memory_kib, "
                        "kdf_parallelism = EXCLUDED.kdf_parallelism, wrapped_dek = EXCLUDED.wrapped_dek, "
                        "key_epoch = EXCLUDED.key_epoch, updated_at = NOW()",
                        (
                            user_id,
                            psycopg2.Binary(bundle["kdf_salt"]),
                            bundle["kdf_time_cost"],
                            bundle["kdf_memory_kib"],
                            bundle["kdf_parallelism"],
                            psycopg2.Binary(bundle["wrapped_dek"]),
                            bundle["key_epoch"],
                        ),
                    )
        finally:
            conn.close()

    def delete_bundle(self, user_id: str) -> None:
        conn = _get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM account_key_bundles WHERE user_id = %s", (user_id,))
        finally:
            conn.close()


@functools.cache
def get_sync_store() -> SyncStore:
    return PostgresSyncStore()


def _decode_size_capped_base64(field_name: str, encoded: str, max_bytes: int) -> bytes:
    """Decode a base64 request field, 400ing on malformed input or an oversized payload."""
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid base64") from exc
    if len(decoded) > max_bytes:
        raise HTTPException(status_code=400, detail=f"{field_name} exceeds the {max_bytes}-byte limit")
    return decoded


def _sync_caller(request: Request) -> tuple[AdminAuth, str]:
    """Authenticate a sync endpoint call; returns (admin auth, full user_id)."""
    auth = authenticate_request(request, get_ctx().ops)
    admin = require_admin(auth)
    return admin, _get_user_id_from_access_token(request.headers.get("authorization", "")[7:])


def _sync_caller_user_id(request: Request) -> str:
    """Authenticate a sync endpoint call and return the caller's full user_id."""
    return _sync_caller(request)[1]


@web_app.get("/sync/records")
def list_workspace_records_endpoint(request: Request) -> dict[str, object]:
    """List all of the caller's workspace records (metadata + opaque secrets)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        records = get_sync_store().list_records(user_id)
        return {"records": [WorkspaceRecordModel(**record).model_dump() for record in records]}


@web_app.put("/sync/records/{host_id}")
def put_workspace_record_endpoint(request: Request, host_id: str, body: WorkspaceRecordModel) -> dict[str, object]:
    """Insert or CAS-update one workspace record; 409 (with the stored row) on conflict.

    Enforces the active-synced-workspaces quota: a push that would create a
    *new* ACTIVE record (a fresh row, or an existing non-active row flipping
    to active) is refused at the cap. Updates to already-active rows and
    tombstoning are always allowed.
    """
    with handle_endpoint_errors():
        admin, user_id = _sync_caller(request)
        if body.host_id != host_id:
            raise HTTPException(status_code=400, detail="host_id in the path and body must match")
        if body.state == WorkspaceRecordState.ACTIVE:
            existing_records = get_sync_store().list_records(user_id)
            existing_row = next((r for r in existing_records if r["host_id"] == host_id), None)
            is_new_active = existing_row is None or existing_row["state"] != WorkspaceRecordState.ACTIVE.value
            if is_new_active:
                entitlements = ensure_account_entitlements(
                    user_id=user_id, username_prefix=admin.username, email=admin.email or ""
                )
                active_count = sum(1 for r in existing_records if r["state"] == WorkspaceRecordState.ACTIVE.value)
                if active_count >= entitlements.max_active_synced_workspaces:
                    raise_quota_exceeded(
                        "max_active_synced_workspaces",
                        entitlements.max_active_synced_workspaces,
                        active_count,
                        "active synced workspaces",
                    )
        record = body.model_dump(mode="json")
        record["encrypted_secrets"] = (
            _decode_size_capped_base64("encrypted_secrets", body.encrypted_secrets, _MAX_ENCRYPTED_SECRETS_BYTES)
            if body.encrypted_secrets is not None
            else None
        )
        try:
            stored = get_sync_store().put_record(user_id, record)
        except SyncRevisionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "revision conflict",
                    "stored": WorkspaceRecordModel(**exc.stored_record).model_dump(),
                },
            ) from exc
        except SyncActiveAgentConflictError as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
        return WorkspaceRecordModel(**stored).model_dump()


@web_app.delete("/sync/records/{host_id}")
def delete_workspace_record_endpoint(request: Request, host_id: str) -> dict[str, str]:
    """Remove one workspace record outright (disassociation; idempotent)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        get_sync_store().delete_record(user_id, host_id)
        return {"status": "deleted"}


@web_app.post("/sync/scrub-secrets")
def scrub_sync_secrets_endpoint(request: Request) -> dict[str, object]:
    """Strip encrypted_secrets from all the caller's records (the clear-password flow)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        return {"scrubbed": get_sync_store().scrub_secrets(user_id)}


@web_app.get("/sync/bundle")
def get_key_bundle_endpoint(request: Request) -> dict[str, object]:
    """Fetch the caller's password-wrapped key bundle (404 when none is stored)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        bundle = get_sync_store().get_bundle(user_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="No key bundle stored for this account")
        return AccountKeyBundleModel(**bundle).model_dump()


@web_app.put("/sync/bundle")
def put_key_bundle_endpoint(request: Request, body: AccountKeyBundleModel) -> dict[str, str]:
    """Store (replace) the caller's password-wrapped key bundle."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        bundle = body.model_dump()
        bundle["kdf_salt"] = _decode_size_capped_base64("kdf_salt", body.kdf_salt, _MAX_KEY_BUNDLE_FIELD_BYTES)
        bundle["wrapped_dek"] = _decode_size_capped_base64(
            "wrapped_dek", body.wrapped_dek, _MAX_KEY_BUNDLE_FIELD_BYTES
        )
        get_sync_store().put_bundle(user_id, bundle)
        return {"status": "ok"}


@web_app.delete("/sync/bundle")
def delete_key_bundle_endpoint(request: Request) -> dict[str, str]:
    """Delete the caller's key bundle (idempotent; part of the clear-password flow)."""
    with handle_endpoint_errors():
        user_id = _sync_caller_user_id(request)
        get_sync_store().delete_bundle(user_id)
        return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Account endpoints (plan + entitlements + live usage)
# ---------------------------------------------------------------------------


def _count_leased_hosts(username_prefix: str) -> int:
    """Count the user's current pool-host leases (the remote-workspace usage number)."""
    conn = _get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_COUNT_LEASED_HOSTS_SQL, (username_prefix,))
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


def _count_active_sync_records(user_id: str) -> int:
    records = get_sync_store().list_records(user_id)
    return sum(1 for r in records if r["state"] == WorkspaceRecordState.ACTIVE.value)


def _summarize_owner_bucket_usage(ops: CloudflareOps, username_prefix: str) -> tuple[int, int]:
    """Return the owner's (bucket_count, total_bytes) from live REST usage reads.

    Display-only semantics: a failed read for one bucket logs a warning and
    counts that bucket as zero rather than failing the whole request.
    """
    bucket_names = [str(bucket.get("name", "")) for bucket in _list_owned_buckets(ops, username_prefix)]
    total_bucket_bytes = 0
    for bucket_name, result in zip(
        bucket_names, _read_bucket_usage_bytes_concurrently(ops, bucket_names), strict=True
    ):
        if isinstance(result, (CloudflareApiError, httpx.HTTPError)):
            logger.warning("Failed to read usage for bucket %s: %s", bucket_name, result)
        else:
            total_bucket_bytes += result
    return len(bucket_names), total_bucket_bytes


def compute_account_usage(ops: CloudflareOps, username_prefix: str, user_id: str) -> AccountUsage:
    """Compute the account's live usage numbers, querying the upstream sources concurrently.

    The three network-backed sources (Cloudflare tunnel count, per-bucket
    REST usage, LiteLLM spend) are independent and run concurrently; the two
    DB-backed counts stay on the request thread because the stores' psycopg2
    connections are not shared-safe across threads. Bucket byte counts come
    from the real-time per-bucket REST usage endpoint (bounded by the
    account's bucket quota, itself read concurrently).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        tunnel_count_future = pool.submit(count_user_tunnels, ops, username_prefix)
        bucket_summary_future = pool.submit(_summarize_owner_bucket_usage, ops, username_prefix)
        llm_spend_future = pool.submit(get_litellm_user_spend, user_id)
        leased_host_count = _count_leased_hosts(username_prefix)
        active_sync_count = _count_active_sync_records(user_id)
        bucket_count, total_bucket_bytes = bucket_summary_future.result()
        spend, reset_at = llm_spend_future.result()
        tunnel_count = tunnel_count_future.result()
    return AccountUsage(
        remote_workspaces=leased_host_count,
        tunnels=tunnel_count,
        buckets=bucket_count,
        total_bucket_bytes=total_bucket_bytes,
        llm_spend_usd_this_period=spend,
        llm_budget_resets_at=reset_at,
        active_synced_workspaces=active_sync_count,
    )


@web_app.get("/account")
def get_account(request: Request) -> dict[str, object]:
    """Return the caller's plan, entitlement values, and live usage.

    Lazily creates the entitlements row on first touch (like every other
    quota-relevant endpoint), so this is also the cheapest way for a client
    to materialize an account's plan.
    """
    with handle_endpoint_errors():
        ops = get_ctx().ops
        auth = authenticate_request(request, ops)
        admin = require_admin(auth)
        token = request.headers.get("authorization", "")[7:]
        user_id = _get_user_id_from_access_token(token)
        entitlements = ensure_account_entitlements(
            user_id=user_id, username_prefix=admin.username, email=admin.email or ""
        )
        usage = compute_account_usage(ops, admin.username, user_id)
        return AccountInfoResponse(
            user_id=user_id,
            email=admin.email or "",
            plan_name=entitlements.plan_name,
            entitlements=entitlements.quota_values(),
            usage=usage,
            available_plans=[str(p["plan_name"]) for p in get_entitlements_store().list_plans()],
        ).model_dump()


def apply_plan_to_account(user_id: str, plan_name: str, store: "EntitlementsStore | None" = None) -> PlanEntitlements:
    """Reset an account's entitlements wholesale to a plan's defaults.

    Pushes the plan's monthly LLM budget to LiteLLM *first*: a failed push
    fails the whole operation, so the DB row and LiteLLM never diverge.
    """
    entitlements_store = store if store is not None else get_entitlements_store()
    plan = entitlements_store.get_plan(plan_name)
    if plan is None:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan_name!r}")
    upsert_litellm_user_budget(user_id, float(plan["monthly_llm_spend_usd"]))
    entitlements_store.update_entitlements(
        user_id, {"plan_name": plan_name, **{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES}}
    )
    return PlanEntitlements(**{name: plan[name] for name in QUOTA_ENTITLEMENT_NAMES})


@web_app.post("/account/plan")
def set_account_plan(request: Request, body: SetPlanRequest) -> dict[str, object]:
    """Switch the caller's plan, resetting their entitlements to the plan's defaults.

    Re-selecting the current plan is a no-op (so idempotent client retries
    never wipe operator-granted bumps). Switching to 'ally' requires a
    paid-listed email.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, get_ctx().ops)
        admin = require_admin(auth)
        entitlements = resolve_entitlements_for_admin(request, admin)
        if body.plan == entitlements.plan_name:
            return {"plan_name": entitlements.plan_name, "entitlements": entitlements.quota_values().model_dump()}
        if body.plan == _PLAN_ALLY:
            require_ally_eligible(admin.email)
        new_values = apply_plan_to_account(entitlements.user_id, body.plan)
        return {"plan_name": body.plan, "entitlements": new_values.model_dump()}


# ---------------------------------------------------------------------------
# Account admin endpoints (email-addressed, admin-key authenticated)
#
# Same fixed-key auth as the paid-list CRUD (``MINDS_PAID_ADMIN_KEY``); the
# operator addresses users by email and the connector resolves the SuperTokens
# user. ``show`` lazily creates the entitlements row (so a subsequent
# ``set-quota`` always has a row to update); ``set-plan`` always resets to the
# plan's defaults (the operator's way to wipe manual bumps) and deliberately
# skips the ally eligibility check -- the operator knows best.
# ---------------------------------------------------------------------------


def _resolve_user_id_by_email(email: str) -> str:
    """Resolve a SuperTokens user id from an email; 404 when no account exists."""
    users = list_users_by_account_info(
        tenant_id=_AUTH_TENANT_ID,
        account_info=AccountInfoInput(email=email.strip().lower()),
    )
    if not users:
        raise HTTPException(status_code=404, detail=f"No account found for email {email!r}")
    return str(users[0].id)


def _admin_ensure_entitlements(email: str) -> AccountEntitlements:
    user_id = _resolve_user_id_by_email(email)
    username_prefix = derive_username_prefix(user_id)
    return ensure_account_entitlements(user_id=user_id, username_prefix=username_prefix, email=email)


@web_app.get("/admin/accounts/{email}")
def admin_get_account(request: Request, email: str) -> dict[str, object]:
    """Operator view of one account: plan, entitlements, and live usage."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        entitlements = _admin_ensure_entitlements(email)
        usage = compute_account_usage(get_ctx().ops, entitlements.username_prefix, entitlements.user_id)
        return AccountInfoResponse(
            user_id=entitlements.user_id,
            email=email.strip().lower(),
            plan_name=entitlements.plan_name,
            entitlements=entitlements.quota_values(),
            usage=usage,
            available_plans=[str(p["plan_name"]) for p in get_entitlements_store().list_plans()],
        ).model_dump()


@web_app.post("/admin/accounts/{email}/plan")
def admin_set_account_plan(request: Request, email: str, body: AdminSetPlanRequest) -> dict[str, object]:
    """Assign a plan to an account, resetting its entitlements to the plan's defaults."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        entitlements = _admin_ensure_entitlements(email)
        new_values = apply_plan_to_account(entitlements.user_id, body.plan)
        return {"plan_name": body.plan, "entitlements": new_values.model_dump()}


@web_app.post("/admin/accounts/{email}/quota")
def admin_set_account_quota(request: Request, email: str, body: AdminSetQuotaRequest) -> dict[str, object]:
    """Set a single entitlement value on an account (an operator bump)."""
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        if body.entitlement not in QUOTA_ENTITLEMENT_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown entitlement {body.entitlement!r}; must be one of {list(QUOTA_ENTITLEMENT_NAMES)}",
            )
        if body.entitlement in _INTEGER_ENTITLEMENT_NAMES and body.value != int(body.value):
            raise HTTPException(
                status_code=400, detail=f"Entitlement {body.entitlement!r} requires a whole number, got {body.value}"
            )
        if body.value < 0:
            raise HTTPException(status_code=400, detail="Entitlement values must be non-negative")
        entitlements = _admin_ensure_entitlements(email)
        value: float | int = body.value if body.entitlement == "monthly_llm_spend_usd" else int(body.value)
        if body.entitlement == "monthly_llm_spend_usd":
            upsert_litellm_user_budget(entitlements.user_id, float(value))
        get_entitlements_store().update_entitlements(entitlements.user_id, {body.entitlement: value})
        return {"status": "updated", "entitlement": body.entitlement, "value": value}


@web_app.post("/admin/sweep/r2")
def admin_run_r2_sweep(request: Request, email: str | None = None) -> dict[str, object]:
    """Run one R2 storage-quota sweep pass on demand (operator tool + deployment tests).

    Authenticated by the fixed operator admin key (``MINDS_PAID_ADMIN_KEY``),
    NOT the SuperTokens auth path. An optional ``email`` query parameter
    scopes the pass to one account (resolved via SuperTokens); without it the
    pass covers every account, exactly like the hourly cron.
    """
    with handle_endpoint_errors():
        require_paid_admin_key(request)
        only_user_id = _resolve_user_id_by_email(email) if email else None
        counters = run_r2_quota_sweep(
            get_ctx().ops,
            get_key_store(),
            get_entitlements_store(),
            get_grant_store(),
            only_user_id=only_user_id,
        )
        return {"status": "completed", "counters": counters}


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


def _mark_email_verified(recipe_user_id: RecipeUserId, email: str) -> None:
    """Force-verify an email without the user clicking a link.

    Mints a verification token and immediately consumes it. A no-op when the
    email is already verified (the SDK then returns an already-verified result
    that carries no token to consume).
    """
    token_result = create_email_verification_token(
        tenant_id=_AUTH_TENANT_ID,
        recipe_user_id=recipe_user_id,
        email=email,
    )
    if isinstance(token_result, CreateEmailVerificationTokenOkResult):
        verify_email_using_token(tenant_id=_AUTH_TENANT_ID, token=token_result.token)


def _mark_paid_email_verified_best_effort(email: str) -> None:
    """Mark any existing account for ``email`` verified, swallowing failures.

    Called when an email is added to the paid list so a user who already signed
    up (but never verified) is not left locked out of the paid access they were
    just granted. Purely best-effort: SuperTokens being unconfigured or
    unreachable must never fail the paid-list write, and an email with no
    account yet is simply a no-op (that user is auto-verified at signup
    instead).
    """
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        return
    try:
        users = list_users_by_account_info(
            tenant_id=_AUTH_TENANT_ID,
            account_info=AccountInfoInput(email=email),
        )
        for user in users:
            for login_method in user.login_methods:
                if login_method.email == email and not login_method.verified:
                    _mark_email_verified(recipe_user_id=login_method.recipe_user_id, email=email)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Failed to auto-verify paid email %s: %s", email, exc)


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

            # Paid users skip the email-verification round trip: mark the new
            # account verified up front (before minting the session, so its very
            # first token already carries the verified claim) and don't send a
            # verification email. A paid-list lookup failure falls back to the
            # normal verify-by-email flow rather than failing the signup.
            # ``KeyError`` covers an unset ``DATABASE_URL`` (pool DB not
            # configured); ``psycopg2.Error`` covers a connect/query failure.
            try:
                is_paid = is_email_paid(email)
            except (psycopg2.Error, KeyError) as exc:
                logger.warning("Paid-list lookup failed during signup for %s; treating as not paid: %s", email, exc)
                is_paid = False
            if is_paid:
                _mark_email_verified(recipe_user_id=recipe_user_id, email=email)

            tokens = _build_session_tokens(user.id)
            if not is_paid:
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
            needs_email_verification=not is_paid,
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
    "fastapi[standard]", "httpx", "supertokens-python", "psycopg2-binary", "paramiko", "tenacity"
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
    """The Modal secrets attached to every connector function (web app + cron)."""
    return [
        modal.Secret.from_name(f"cloudflare-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"supertokens-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"neon-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"pool-ssh-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_name(f"litellm-connector-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
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
# Without this, Modal delivers ONE request per container at a time, so a
# single slow request (a lease's SSH provisioning, a cold sync pull) makes
# every other caller queue behind it or wait out a fresh container's cold
# boot -- even with a warm pool. The app is safe to run concurrently: routes
# are sync ``def`` (FastAPI runs them on its threadpool), every route opens
# its own psycopg2 connection and closes it in ``finally``, the lease
# selection uses ``FOR UPDATE SKIP LOCKED``, the shared Cloudflare
# ``httpx.Client`` is thread-safe, and the only module-level mutable state
# (the paid-status cache) is lock-guarded. ``max_inputs`` is kept modest
# because each concurrent request holds one direct Neon connection and one
# threadpool thread for its duration.
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    _init_supertokens()
    return web_app


@app.function(
    name="cleanup_removing_pool_hosts",
    secrets=_connector_secrets(),
    # Hourly slice-box reconcile audit. Scoped to this env's stamped slices; it
    # only alerts (never auto-deletes), so it is safe on a box shared by multiple
    # dev envs.
    schedule=modal.Cron("0 * * * *"),
    timeout=900,
)
def cleanup_removing_pool_hosts() -> dict[str, int]:
    conn = _get_pool_db_connection()
    try:
        # Audit this env's slices on every box against the DB (alert-only: it never
        # auto-deletes, to avoid racing an in-flight bake). Scoped to MINDS_ENV_NAME so
        # it is safe on a box shared by multiple dev envs. A reconcile failure (DB,
        # SSH, or a missing POOL_SSH_PRIVATE_KEY while boxes exist) is a real failure:
        # let it propagate and fail the cron run rather than silently swallowing it.
        divergence_count = reconcile_slice_boxes(conn, _current_minds_env_name())
    finally:
        conn.close()
    logger.info("Slice reconcile done: slice_divergences=%d", divergence_count)
    return {"slice_divergences": divergence_count}


# One-time-per-container SuperTokens init for the sweep cron: the sweep's lazy
# entitlements creation resolves owner emails via the SuperTokens SDK, and
# ``supertokens_init`` must not run twice in a warm container.
@functools.cache
def _init_supertokens_once() -> None:
    _init_supertokens()


@app.function(
    name="r2_quota_sweep",
    secrets=_connector_secrets(),
    # Hourly storage-quota sweep, offset from the slice reconcile so the two
    # crons don't contend for a cold container at the top of the hour.
    schedule=modal.Cron("30 * * * *"),
    timeout=900,
)
def r2_quota_sweep() -> dict[str, int]:
    _init_supertokens_once()
    counters = run_r2_quota_sweep(get_ctx().ops, get_key_store(), get_entitlements_store(), get_grant_store())
    logger.info("R2 quota sweep done: %s", counters)
    return counters
