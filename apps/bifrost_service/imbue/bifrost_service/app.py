"""Bifrost LLM gateway service, deployed as a Modal app.

Runs the `Bifrost <https://github.com/maximhq/bifrost>`_ LLM gateway backed by
a Neon.tech PostgreSQL database. Exposes two Modal Functions:

* An **inference** Function (``bifrost_inference``) that runs bifrost's
  Go server directly via ``@modal.web_server(port=8080)``. Agents hit this
  endpoint with an ``Authorization: Bearer sk-bf-*`` virtual key to make LLM
  calls against Anthropic through bifrost's budget/rate-limit enforcement.
* A **management** Function (``bifrost_management``) that exposes a
  SuperTokens-authenticated FastAPI app for creating, listing, reading,
  updating, and deleting virtual keys scoped to the signed-in user. It runs
  its own local bifrost subprocess and proxies admin calls to
  ``http://localhost:8080/api/governance/virtual-keys`` using the
  ``BIFROST_ADMIN_TOKEN`` bearer token.

Both Functions share the same Neon DB for consistent state without needing
cross-function networking.

This file is entirely self-contained -- it has NO imports from the monorepo.
Only stdlib and 3rd-party packages (installed in the Modal image) are used.
This keeps deployment simple: ``modal deploy app.py`` ships just this file.
"""

import functools
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from typing import Any
from typing import NoReturn

import httpx
import modal
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from supertokens_python import InputAppInfo
from supertokens_python import SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe import session as st_session_recipe
from supertokens_python.recipe.emailverification import EmailVerificationClaim
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import get_session_without_request_response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BIFROST_PORT = 8080
_BIFROST_HOST = "127.0.0.1"
_BIFROST_APP_DIR = "/app/bifrost"
_BIFROST_BINARY = "bifrost-http"
_BIFROST_NPM_VERSION = "1.6.2"

_BIFROST_ADMIN_BASE = f"http://{_BIFROST_HOST}:{_BIFROST_PORT}"
_BIFROST_STARTUP_TIMEOUT_SECONDS = 60.0
_BIFROST_STARTUP_POLL_INTERVAL_SECONDS = 0.25

# Max virtual keys returned per list-keys call. The handler further filters
# this list by the caller's user prefix, so the effective per-user cap is also
# 500; revisit if any user ever realistically owns more keys than this.
_VIRTUAL_KEY_LIST_LIMIT = 500

# Key-name separator. Mirrors ``TUNNEL_NAME_SEP`` in the
# remote_service_connector: picked so parsing a name into
# ``(user_prefix, user_choice)`` is unambiguous. Users' names cannot contain
# this separator (enforced at create time).
_KEY_NAME_SEP = "--"

# Length of the SuperTokens user ID prefix used for namespacing. Matches the
# remote_service_connector so the same user identity maps to the same prefix
# across services.
_USER_ID_PREFIX_LENGTH = 16

# Default budget applied when the caller does not specify one.
_DEFAULT_BUDGET_DOLLARS = 100.0
_DEFAULT_BUDGET_RESET_DURATION = "1d"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BifrostServiceError(RuntimeError):
    """Base class for errors raised by this service."""


class BifrostAdminApiError(BifrostServiceError):
    """Raised when the local bifrost admin API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Bifrost admin API error ({status_code}): {detail}")


class BifrostNotReadyError(BifrostServiceError):
    """Raised when bifrost does not come up within the startup timeout."""


class VirtualKeyOwnershipError(PermissionError):
    """Raised when a user attempts to manage a key owned by someone else."""

    def __init__(self, key_name: str, user_prefix: str) -> None:
        self.key_name = key_name
        self.user_prefix = user_prefix
        super().__init__(f"User '{user_prefix}' does not own virtual key '{key_name}'")


class VirtualKeyNotFoundError(KeyError):
    """Raised when a virtual key cannot be found in bifrost."""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id
        super().__init__(f"Virtual key not found: {key_id}")


class InvalidKeyNameError(ValueError):
    """Raised when a caller-supplied key name contains forbidden characters."""

    def __init__(self, name: str) -> None:
        self.key_name = name
        super().__init__(f"Key name '{name}' must not contain '{_KEY_NAME_SEP}'")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    name: str = Field(description="Caller-chosen short name for the key (scoped to this user)")
    budget_dollars: float | None = Field(
        default=None,
        description=f"Max spend before the budget resets. Defaults to ${_DEFAULT_BUDGET_DOLLARS:g}.",
    )
    budget_reset_duration: str | None = Field(
        default=None,
        description=(
            "How often the budget resets, e.g. '1d', '1h', '1M', '1w'. "
            f"Defaults to '{_DEFAULT_BUDGET_RESET_DURATION}'."
        ),
    )


class UpdateBudgetRequest(BaseModel):
    budget_dollars: float = Field(description="Max spend before the budget resets.")
    budget_reset_duration: str | None = Field(
        default=None,
        description=f"Reset duration string (e.g. '1d'). Defaults to '{_DEFAULT_BUDGET_RESET_DURATION}'.",
    )


class BudgetInfo(BaseModel):
    max_limit: float = Field(description="Configured max spend before the budget resets.")
    reset_duration: str = Field(description="Reset duration string (e.g. '1d').")
    current_usage: float = Field(description="Spend since the last reset.")
    last_reset: str | None = Field(default=None, description="ISO timestamp of the last reset, if known.")


class VirtualKeyInfo(BaseModel):
    """Public description of a virtual key. Never includes the key value itself.

    The ``sk-bf-*`` key value is only returned from the create endpoint -- once
    bifrost stores it (encrypted), the plaintext is not retrievable.
    """

    key_id: str = Field(description="Opaque bifrost virtual-key ID")
    name: str = Field(description="Fully-qualified key name (includes the user prefix)")
    short_name: str = Field(description="The caller-provided portion of the name (without the user prefix)")
    is_active: bool = Field(description="Whether the key is currently usable")
    budget: BudgetInfo | None = Field(default=None, description="Budget state, if a budget is configured")


class CreateKeyResponse(VirtualKeyInfo):
    """Create-key response. Includes the one-time ``sk-bf-*`` key value."""

    value: str = Field(description="The sk-bf-* key value. Only returned on creation.")


# ---------------------------------------------------------------------------
# Key-name helpers
# ---------------------------------------------------------------------------


def make_key_name(user_prefix: str, short_name: str) -> str:
    """Combine a user prefix and a caller-supplied short name."""
    if _KEY_NAME_SEP in short_name:
        raise InvalidKeyNameError(short_name)
    return f"{user_prefix}{_KEY_NAME_SEP}{short_name}"


def extract_short_name(full_name: str, user_prefix: str) -> str:
    """Return the caller-facing portion of a fully-qualified key name.

    Raises ``VirtualKeyOwnershipError`` when ``full_name`` does not begin with
    the expected ``{user_prefix}{_KEY_NAME_SEP}`` prefix.
    """
    expected = f"{user_prefix}{_KEY_NAME_SEP}"
    if not full_name.startswith(expected):
        raise VirtualKeyOwnershipError(full_name, user_prefix)
    return full_name[len(expected) :]


def is_owned_by(full_name: str, user_prefix: str) -> bool:
    return full_name.startswith(f"{user_prefix}{_KEY_NAME_SEP}")


# ---------------------------------------------------------------------------
# Bifrost admin client
# ---------------------------------------------------------------------------


class BifrostAdminClient:
    """HTTP client for talking to the local bifrost admin API."""

    def __init__(self, base_url: str, admin_token: str) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30.0,
        )

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code == 404:
            # Only translate 404 into VirtualKeyNotFoundError when the request
            # was targeting a specific key (``/virtual-keys/{id}...``). A 404
            # on the collection endpoint itself (``/virtual-keys``) indicates a
            # bifrost routing/config problem, not a missing key, and should
            # surface as BifrostAdminApiError so the real cause is preserved.
            path = response.request.url.path.rstrip("/")
            collection_marker = "/virtual-keys/"
            marker_index = path.find(collection_marker)
            if marker_index != -1:
                key_id = path[marker_index + len(collection_marker) :].split("/", maxsplit=1)[0]
                if key_id:
                    raise VirtualKeyNotFoundError(key_id)
        if response.status_code >= 400:
            raise BifrostAdminApiError(response.status_code, response.text)
        # Successful responses may have an empty body (e.g. 204 No Content on
        # DELETE). Callers that expect a dict re-validate the return value, so
        # returning None here surfaces unexpected empty bodies as a clear
        # type-mismatch error instead of a confusing JSONDecodeError.
        if not response.content:
            return None
        return response.json()

    def create_virtual_key(
        self,
        name: str,
        budget_dollars: float,
        budget_reset_duration: str,
    ) -> dict[str, Any]:
        """Create a bifrost virtual key that can call Anthropic, with the given budget."""
        body: dict[str, Any] = {
            "name": name,
            "is_active": True,
            "provider_configs": [
                {
                    "provider": "anthropic",
                    "weight": 1.0,
                    "allowed_models": ["*"],
                    "key_ids": ["*"],
                }
            ],
            "budgets": [
                {
                    "max_limit": budget_dollars,
                    "reset_duration": budget_reset_duration,
                }
            ],
        }
        response = self.client.post("/api/governance/virtual-keys", json=body)
        result = self._check(response)
        if not isinstance(result, dict):
            raise BifrostAdminApiError(500, f"Unexpected bifrost create response shape: {type(result).__name__}")
        return result

    def list_virtual_keys(self, search: str | None = None) -> list[dict[str, Any]]:
        """List virtual keys, optionally filtered by name search."""
        params: dict[str, str] = {"limit": str(_VIRTUAL_KEY_LIST_LIMIT)}
        if search is not None:
            params["search"] = search
        response = self.client.get("/api/governance/virtual-keys", params=params)
        result = self._check(response)
        if isinstance(result, dict):
            raw_keys = result.get("virtual_keys") or result.get("items") or result.get("results") or []
        else:
            raw_keys = result
        if not isinstance(raw_keys, list):
            raise BifrostAdminApiError(500, f"Unexpected bifrost list response shape: {type(raw_keys).__name__}")
        return raw_keys

    def get_virtual_key(self, key_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/governance/virtual-keys/{key_id}")
        result = self._check(response)
        if not isinstance(result, dict):
            raise BifrostAdminApiError(500, f"Unexpected bifrost get response shape: {type(result).__name__}")
        return result

    def update_virtual_key_budget(
        self,
        key_id: str,
        budget_dollars: float,
        budget_reset_duration: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "budgets": [
                {
                    "max_limit": budget_dollars,
                    "reset_duration": budget_reset_duration,
                }
            ],
        }
        response = self.client.put(f"/api/governance/virtual-keys/{key_id}", json=body)
        result = self._check(response)
        if not isinstance(result, dict):
            raise BifrostAdminApiError(500, f"Unexpected bifrost update response shape: {type(result).__name__}")
        return result

    def delete_virtual_key(self, key_id: str) -> None:
        response = self.client.delete(f"/api/governance/virtual-keys/{key_id}")
        self._check(response)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_budget_info(raw_key: dict[str, Any]) -> BudgetInfo | None:
    """Pull the first budget entry out of a bifrost virtual-key record."""
    budgets = raw_key.get("budgets") or []
    if not budgets:
        return None
    first = budgets[0]
    return BudgetInfo(
        max_limit=float(first.get("max_limit", 0.0)),
        reset_duration=str(first.get("reset_duration", "")),
        current_usage=float(first.get("current_usage", 0.0)),
        last_reset=first.get("last_reset"),
    )


def _to_virtual_key_info(raw_key: dict[str, Any], user_prefix: str) -> VirtualKeyInfo:
    name = str(raw_key.get("name", ""))
    return VirtualKeyInfo(
        key_id=str(raw_key.get("id", "")),
        name=name,
        short_name=extract_short_name(name, user_prefix),
        is_active=bool(raw_key.get("is_active", True)),
        budget=_extract_budget_info(raw_key),
    )


def _to_create_key_response(raw_key: dict[str, Any], user_prefix: str) -> CreateKeyResponse:
    info = _to_virtual_key_info(raw_key, user_prefix)
    value = raw_key.get("value")
    if not isinstance(value, str) or not value:
        raise BifrostAdminApiError(500, "Bifrost create-key response missing 'value'")
    return CreateKeyResponse(
        key_id=info.key_id,
        name=info.name,
        short_name=info.short_name,
        is_active=info.is_active,
        budget=info.budget,
        value=value,
    )


# ---------------------------------------------------------------------------
# Bifrost config + subprocess
# ---------------------------------------------------------------------------


def _build_bifrost_config() -> dict[str, Any]:
    """Build the bifrost ``config.json`` contents from the current environment.

    Both the config store and the logs store are pointed at PostgreSQL on
    Neon. They use *different* databases (env-specified) to reduce load on any
    single database. Secrets (DB credentials, encryption key, admin token,
    provider API keys) are always referenced via bifrost's ``env.VAR`` syntax
    so the actual values never land in the serialized config on disk.
    """
    return {
        "version": 2,
        "client": {
            "drop_excess_requests": False,
            "allowed_origins": ["*"],
        },
        "encryption_key": {"value": "env.BIFROST_ENCRYPTION_KEY"},
        "auth_config": {
            "mode": "bearer",
            "bearer_token": {"value": "env.BIFROST_ADMIN_TOKEN"},
        },
        "providers": {
            "anthropic": {
                "keys": [
                    {
                        "value": "env.ANTHROPIC_API_KEY",
                        "models": ["*"],
                        "weight": 1.0,
                    }
                ],
            }
        },
        "config_store": {
            "enabled": True,
            "type": "postgres",
            "config": {
                "host": {"value": "env.NEON_CONFIG_HOST"},
                "port": {"value": "env.NEON_CONFIG_PORT"},
                "user": {"value": "env.NEON_CONFIG_USER"},
                "password": {"value": "env.NEON_CONFIG_PASSWORD"},
                "db_name": {"value": "env.NEON_CONFIG_DB"},
                "ssl_mode": {"value": "require"},
                "max_idle_conns": 2,
                "max_open_conns": 10,
            },
        },
        "logs_store": {
            "enabled": True,
            "type": "postgres",
            "config": {
                "host": {"value": "env.NEON_LOGS_HOST"},
                "port": {"value": "env.NEON_LOGS_PORT"},
                "user": {"value": "env.NEON_LOGS_USER"},
                "password": {"value": "env.NEON_LOGS_PASSWORD"},
                "db_name": {"value": "env.NEON_LOGS_DB"},
                "ssl_mode": {"value": "require"},
                "max_idle_conns": 2,
                "max_open_conns": 10,
            },
        },
    }


def write_bifrost_config(app_dir: str) -> str:
    """Write ``config.json`` under ``app_dir`` and return its path.

    Creates the directory if needed. Overwrites any existing config.
    """
    os.makedirs(app_dir, exist_ok=True)
    config_path = os.path.join(app_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(_build_bifrost_config(), handle, indent=2)
    return config_path


def _resolve_bifrost_binary() -> str:
    """Return the path to the bifrost binary installed via npm in the image."""
    binary = shutil.which(_BIFROST_BINARY)
    if binary is None:
        binary = shutil.which("bifrost")
    if binary is None:
        raise BifrostNotReadyError(
            f"Could not find bifrost binary on PATH (looked for '{_BIFROST_BINARY}' and 'bifrost')"
        )
    return binary


def start_bifrost_subprocess(app_dir: str) -> "subprocess.Popen[bytes]":
    """Start bifrost listening on localhost:_BIFROST_PORT and return the handle.

    The process inherits the parent's environment so bifrost's ``env.VAR``
    config references resolve against our Modal Secret-injected variables.
    """
    binary = _resolve_bifrost_binary()
    cmd = [
        binary,
        "-app-dir",
        app_dir,
        "-port",
        str(_BIFROST_PORT),
        "-host",
        _BIFROST_HOST,
        "-log-level",
        "info",
    ]
    logger.info("Starting bifrost subprocess: %s", " ".join(cmd))
    # Bifrost is a Go server process that should live for the container's
    # lifetime. We rely on Modal container teardown to reap it -- there is
    # nowhere cleaner to wire up cleanup for a @modal.web_server Function.
    return subprocess.Popen(cmd)


def _is_bifrost_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_bifrost_ready(
    process: "subprocess.Popen[bytes]",
    timeout_seconds: float = _BIFROST_STARTUP_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _BIFROST_STARTUP_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll until bifrost is accepting connections on the configured port.

    Raises ``BifrostNotReadyError`` if the process exits early, or if the
    port is not open within ``timeout_seconds``.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BifrostNotReadyError(f"bifrost exited early with return code {process.returncode}")
        if _is_bifrost_listening(_BIFROST_HOST, _BIFROST_PORT):
            logger.info("Bifrost is ready on %s:%d", _BIFROST_HOST, _BIFROST_PORT)
            return
        time.sleep(poll_interval_seconds)
    raise BifrostNotReadyError(f"bifrost did not open port {_BIFROST_PORT} within {timeout_seconds:g}s")


# ---------------------------------------------------------------------------
# Auth (SuperTokens)
# ---------------------------------------------------------------------------


def _require_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    return header[7:]


def _authenticate_supertokens(token: str) -> str:
    """Validate a SuperTokens JWT. Returns the 16-char user ID prefix.

    Follows the same derivation rule as the remote_service_connector so that a
    given SuperTokens user maps to the same namespacing prefix across both
    services.
    """
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        raise HTTPException(status_code=503, detail="SuperTokens not configured on the server")
    try:
        session = get_session_without_request_response(access_token=token, anti_csrf_check=False)
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")
    payload = session.get_access_token_payload()
    is_verified = EmailVerificationClaim.get_value_from_payload(payload)
    if not is_verified:
        raise HTTPException(status_code=401, detail="Email not verified")
    user_id = session.get_user_id()
    return user_id.replace("-", "")[:_USER_ID_PREFIX_LENGTH]


def require_user_prefix(request: Request) -> str:
    """Return the 16-char user ID prefix for the authenticated caller."""
    token = _require_bearer_token(request)
    return _authenticate_supertokens(token)


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


@functools.cache
def get_admin_client() -> BifrostAdminClient:
    """Return a module-cached admin client. Pointed at the local bifrost."""
    admin_token = os.environ.get("BIFROST_ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(status_code=503, detail="BIFROST_ADMIN_TOKEN not configured on the server")
    return BifrostAdminClient(base_url=_BIFROST_ADMIN_BASE, admin_token=admin_token)


def _raise_as_http(exc: Exception) -> NoReturn:
    """Convert domain exceptions into HTTP responses."""
    if isinstance(exc, VirtualKeyOwnershipError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, VirtualKeyNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, InvalidKeyNameError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, BifrostAdminApiError):
        logger.warning("Bifrost admin API error: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.exception("Unexpected error in endpoint handler")
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@contextmanager
def handle_endpoint_errors() -> Iterator[None]:
    """Wrap endpoint logic: re-raise HTTPException, convert domain errors to HTTP."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        _raise_as_http(exc)


def _require_owned(client: BifrostAdminClient, key_id: str, user_prefix: str) -> dict[str, Any]:
    """Fetch a virtual key by ID and verify the caller owns it. Returns the raw record."""
    raw = client.get_virtual_key(key_id)
    name = str(raw.get("name", ""))
    if not is_owned_by(name, user_prefix):
        raise VirtualKeyOwnershipError(name, user_prefix)
    return raw


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

web_app = FastAPI()

# FastAPI dependency aliases. Using ``Annotated[..., Depends(f)]`` rather than
# a ``f = Depends(g)`` default arg because ruff B008 flags calling ``Depends``
# in a default, and the module-level Annotated form is the FastAPI-recommended
# modern style. Keeps the handler signatures readable and override-friendly
# via ``web_app.dependency_overrides`` in tests.
UserPrefixDep = Annotated[str, Depends(require_user_prefix)]
AdminClientDep = Annotated[BifrostAdminClient, Depends(get_admin_client)]


@web_app.post("/keys", response_model=CreateKeyResponse)
def create_key(
    body: CreateKeyRequest,
    user_prefix: UserPrefixDep,
    client: AdminClientDep,
) -> CreateKeyResponse:
    """Create a virtual key scoped to the authenticated user.

    The key's name is ``{user_prefix}--{body.name}``. The caller can only use
    the short name portion; the prefix is appended server-side and enforced on
    every subsequent operation.
    """
    with handle_endpoint_errors():
        full_name = make_key_name(user_prefix, body.name)
        budget_dollars = body.budget_dollars if body.budget_dollars is not None else _DEFAULT_BUDGET_DOLLARS
        reset_duration = body.budget_reset_duration or _DEFAULT_BUDGET_RESET_DURATION
        raw = client.create_virtual_key(
            name=full_name,
            budget_dollars=budget_dollars,
            budget_reset_duration=reset_duration,
        )
        return _to_create_key_response(raw, user_prefix)


@web_app.get("/keys", response_model=list[VirtualKeyInfo])
def list_keys(user_prefix: UserPrefixDep, client: AdminClientDep) -> list[VirtualKeyInfo]:
    """List all virtual keys owned by the authenticated user."""
    with handle_endpoint_errors():
        prefix_filter = f"{user_prefix}{_KEY_NAME_SEP}"
        raw_keys = client.list_virtual_keys(search=prefix_filter)
        # Bifrost's ``search`` is a substring match; filter again by exact prefix
        # so a user called ``ab12`` cannot see keys from user ``ab123``.
        return [
            _to_virtual_key_info(k, user_prefix) for k in raw_keys if is_owned_by(str(k.get("name", "")), user_prefix)
        ]


@web_app.get("/keys/{key_id}/budget", response_model=BudgetInfo)
def get_key_budget(key_id: str, user_prefix: UserPrefixDep, client: AdminClientDep) -> BudgetInfo:
    """Return current usage and remaining budget for a key owned by the caller."""
    with handle_endpoint_errors():
        raw = _require_owned(client, key_id, user_prefix)
        budget = _extract_budget_info(raw)
        if budget is None:
            raise HTTPException(status_code=404, detail=f"Virtual key '{key_id}' has no budget configured")
        return budget


@web_app.put("/keys/{key_id}/budget", response_model=BudgetInfo)
def update_key_budget(
    key_id: str,
    body: UpdateBudgetRequest,
    user_prefix: UserPrefixDep,
    client: AdminClientDep,
) -> BudgetInfo:
    """Change the budget on a key owned by the caller."""
    with handle_endpoint_errors():
        _require_owned(client, key_id, user_prefix)
        reset_duration = body.budget_reset_duration or _DEFAULT_BUDGET_RESET_DURATION
        raw = client.update_virtual_key_budget(
            key_id=key_id,
            budget_dollars=body.budget_dollars,
            budget_reset_duration=reset_duration,
        )
        updated_budget = _extract_budget_info(raw)
        if updated_budget is None:
            raise HTTPException(
                status_code=500,
                detail="Bifrost did not return a budget after update",
            )
        return updated_budget


@web_app.delete("/keys/{key_id}")
def delete_key(key_id: str, user_prefix: UserPrefixDep, client: AdminClientDep) -> dict[str, str]:
    """Delete a key owned by the caller."""
    with handle_endpoint_errors():
        _require_owned(client, key_id, user_prefix)
        client.delete_virtual_key(key_id)
        return {"status": "deleted"}


# ---------------------------------------------------------------------------
# SuperTokens initialization
# ---------------------------------------------------------------------------


def _init_supertokens() -> None:
    """Initialize SuperTokens SDK with just the session recipe.

    The management app only validates existing JWTs issued by the
    remote_service_connector's auth flow, so we do not need the signup,
    signin, emailpassword, thirdparty, or emailverification recipes here.
    """
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        return
    api_key = os.environ.get("SUPERTOKENS_API_KEY")
    website_domain = os.environ.get("AUTH_WEBSITE_DOMAIN", _DEFAULT_MANAGEMENT_DOMAIN)
    supertokens_init(
        supertokens_config=SupertokensConfig(connection_uri=connection_uri, api_key=api_key),
        app_info=InputAppInfo(
            app_name="Minds",
            api_domain=website_domain,
            website_domain=website_domain,
            api_base_path="/auth",
            website_base_path="/auth",
        ),
        framework="fastapi",
        recipe_list=[st_session_recipe.init()],
        mode="asgi",
    )
    logger.info("SuperTokens SDK initialized (session recipe only)")


# ---------------------------------------------------------------------------
# Modal deployment
# ---------------------------------------------------------------------------


_DEPLOY_ENV = os.environ.get("MNGR_DEPLOY_ENV", "production")

# Modal URLs follow ``{workspace}--{app-name}-{function-name}.modal.run``, with
# underscores in identifiers normalized to hyphens. For this deployment that's
# ``joshalbrecht--bifrost-<env>-bifrost-management.modal.run``. This fallback
# is only used when ``AUTH_WEBSITE_DOMAIN`` is not set in the secret; in
# practice we set it explicitly from ``.minds/<env>/supertokens.sh``. Mirrors
# the pattern in remote_service_connector so a given deployment has a sensible
# default domain without depending on the monorepo (app.py is self-contained).
_MODAL_WORKSPACE = "joshalbrecht"
_DEFAULT_MANAGEMENT_DOMAIN = f"https://{_MODAL_WORKSPACE}--bifrost-{_DEPLOY_ENV}-bifrost-management.modal.run"

image = (
    modal.Image.debian_slim()
    .apt_install("curl", "ca-certificates", "gnupg")
    .run_commands(
        # Install Node.js 20 (needed for the @maximhq/bifrost npm package).
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
        # Install the pinned bifrost binary. The npm wrapper downloads the
        # platform-specific pre-built binary at install time.
        f"npm install -g @maximhq/bifrost@{_BIFROST_NPM_VERSION}",
    )
    .pip_install("fastapi[standard]", "httpx", "supertokens-python")
)

app = modal.App(name=f"bifrost-{_DEPLOY_ENV}", image=image)


@app.function(
    secrets=[
        modal.Secret.from_name(f"bifrost-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ],
    timeout=600,
    scaledown_window=300,
)
@modal.web_server(port=_BIFROST_PORT, startup_timeout=120)
def bifrost_inference() -> None:
    """Modal Function that runs bifrost directly for inference traffic.

    Modal routes external traffic to ``localhost:_BIFROST_PORT``, so agents
    hit bifrost's OpenAI-compatible ``/v1/*`` routes (and its ``/api/*`` admin
    routes) directly. Admin routes are protected by bifrost's own bearer-token
    auth (``BIFROST_ADMIN_TOKEN``); inference routes are protected by
    per-agent virtual keys.
    """
    config_path = write_bifrost_config(_BIFROST_APP_DIR)
    logger.info("Wrote bifrost config to %s", config_path)
    process = start_bifrost_subprocess(_BIFROST_APP_DIR)
    wait_for_bifrost_ready(process)


@app.function(
    secrets=[
        modal.Secret.from_name(f"bifrost-{_DEPLOY_ENV}"),
        modal.Secret.from_name(f"supertokens-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ],
    timeout=600,
    scaledown_window=300,
)
@modal.asgi_app()
def bifrost_management() -> FastAPI:
    """Modal Function that runs the FastAPI management app.

    Starts its own bifrost subprocess (listening on localhost) so the FastAPI
    handlers can proxy admin calls to bifrost without going over Modal's
    external network. All management containers share the same Neon DB as
    the inference Function, so virtual keys created here are immediately
    usable by inference containers.
    """
    config_path = write_bifrost_config(_BIFROST_APP_DIR)
    logger.info("Wrote bifrost config to %s", config_path)
    process = start_bifrost_subprocess(_BIFROST_APP_DIR)
    wait_for_bifrost_ready(process)
    _init_supertokens()
    return web_app
