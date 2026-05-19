"""Create / delete SuperTokens apps via the multi-tenant management API.

SuperTokens core exposes ``POST /recipe/multitenancy/app`` to register a
new app (which is "tenant" in their terminology); each tenant has its own
user pool, password reset tokens, OAuth state, etc.

We use the dev-tier SuperTokens core's URL + admin API key (from Vault)
to create / delete a per-dev-env tenant. The result is the connection URI
the per-dev-env ``remote_service_connector`` will read.
"""

import re
from typing import Final

import httpx
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0
_CDI_VERSION: Final[str] = "5.1"


class SuperTokensProviderError(MindError):
    """Raised when the SuperTokens management API rejects a request."""


class SuperTokensAppRecord(FrozenModel):
    """Result of :func:`create_supertokens_app`."""

    app_id: str = Field(description="The SuperTokens app id (matches the dev env name).")
    connection_uri: str = Field(description="App-scoped SuperTokens connection URI.")
    api_key: SecretStr = Field(description="Admin API key for the new app (shared with the core's key).")


def _supertokens_request(
    method: str,
    path: str,
    *,
    base_url: str,
    api_key: SecretStr,
    json_body: dict | None = None,
) -> dict:
    headers = {
        "api-key": api_key.get_secret_value(),
        "cdi-version": _CDI_VERSION,
        "Accept": "application/json",
    }
    url = base_url.rstrip("/") + path
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise SuperTokensProviderError(f"SuperTokens API request failed ({method} {url}): {exc}") from exc
    if response.status_code >= 400:
        raise SuperTokensProviderError(
            f"SuperTokens API returned {response.status_code} for {method} {url}: {response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise SuperTokensProviderError(f"SuperTokens API returned non-JSON for {method} {url}: {exc}") from exc


def create_supertokens_app(
    name: DevEnvName,
    *,
    core_base_url: str,
    api_key: SecretStr,
) -> SuperTokensAppRecord:
    """Create a per-dev-env SuperTokens app rooted at the dev-tier core.

    Uses the SuperTokens multi-tenancy API: a new app is created with
    ``app_id=<dev-env-name>`` and inherits the core's API key. The
    returned ``connection_uri`` includes the ``appid-<name>`` suffix that
    the per-dev-env connector deployment will configure into its SuperTokens
    SDK init.

    Idempotent: a 400 response containing ``already exists`` is treated as
    success.
    """
    body = {"appId": str(name)}
    try:
        _supertokens_request(
            "PUT",
            "/recipe/multitenancy/app",
            base_url=core_base_url,
            api_key=api_key,
            json_body=body,
        )
    except SuperTokensProviderError as exc:
        if "already exists" not in str(exc).lower():
            raise
    connection_uri = f"{core_base_url.rstrip('/')}/appid-{name}"
    return SuperTokensAppRecord(
        app_id=str(name),
        connection_uri=connection_uri,
        api_key=api_key,
    )


def delete_supertokens_app(
    name: DevEnvName,
    *,
    core_base_url: str,
    api_key: SecretStr,
) -> None:
    """Delete the per-dev-env SuperTokens app.

    Idempotent: a 404 / "does not exist" response is treated as success.
    """
    try:
        _supertokens_request(
            "POST",
            "/recipe/multitenancy/app/remove",
            base_url=core_base_url,
            api_key=api_key,
            json_body={"appId": str(name)},
        )
    except SuperTokensProviderError as exc:
        message = str(exc).lower()
        if "does not exist" in message or "404" in message or "not found" in message:
            return
        raise


def wipe_supertokens_app_data(
    app_id: str,
    *,
    core_base_url: str,
    api_key: SecretStr,
) -> None:
    """Wipe every user / session / per-tenant record in an existing SuperTokens app.

    Used by ``minds env destroy --yes-i-mean-staging`` to clear the
    staging app's accumulated data without removing the app itself --
    the operator's Vault entry for staging holds the app's connection
    URI and admin API key, and we want both to stay valid across
    destroy / redeploy cycles.

    Implementation: delete the SuperTokens app and immediately recreate
    it with the same ``app_id``. SuperTokens treats this as a fresh app
    (zero users, zero sessions, no per-tenant config) but the
    multi-tenant connection URI ``<core>/appid-<app_id>`` is unchanged
    -- so the operator's Vault entry and the per-tier Modal Secret it
    pushes to the connector both continue to work without updating.

    Idempotent: if the app was already deleted out of band, the recreate
    succeeds and the wipe is effectively a no-op.
    """
    name = DevEnvName(app_id)
    delete_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)
    create_supertokens_app(name, core_base_url=core_base_url, api_key=api_key)


def app_id_from_connection_uri(connection_uri: str) -> str:
    """Extract the SuperTokens app id from a multi-tenant connection URI.

    SuperTokens encodes the app id as a path segment: ``<core_url>/appid-<app_id>``.
    The destroy-side wipe needs the bare ``<app_id>`` to call the
    multi-tenancy admin endpoints. Raises :class:`SuperTokensProviderError`
    when the URI doesn't carry an ``/appid-<id>`` segment so a
    misconfigured Vault entry surfaces immediately instead of trying to
    wipe the wrong app.
    """
    match = re.search(r"/appid-([^/?#]+)", connection_uri)
    if match is None:
        raise SuperTokensProviderError(
            f"SUPERTOKENS_CONNECTION_URI {connection_uri!r} has no `/appid-<app_id>` segment; "
            "cannot determine which SuperTokens app to wipe."
        )
    return match.group(1)
