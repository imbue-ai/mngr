"""HTTP client for the remote_service_connector.

One client wraps all the connector concerns (auth, hosts, keys, shares, ...)
so the CLI commands and provider can share a single httpx instance per
account.

Authentication semantics:
- Methods explicitly named ``*_auth_*`` (signin/signup/refresh, the browser
  login's device-token exchange) take no bearer token and are intended for
  unauthenticated callers.
- All other methods take an ``access_token`` (a SecretStr).
- The session store handles persistence; this client never reads or writes
  session files itself.
"""

import os
import time
from functools import cache
from importlib import metadata
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.errors import CLIENT_TOO_OLD_FALLBACK_MESSAGE
from imbue.mngr_imbue_cloud.errors import ImbueCloudAccountError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAccountSuspendedError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketExistsError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketLimitError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotFoundError
from imbue.mngr_imbue_cloud.errors import ImbueCloudCleanupGrantBudgetError
from imbue.mngr_imbue_cloud.errors import ImbueCloudClientTooOldError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudEmailNotVerifiedError
from imbue.mngr_imbue_cloud.errors import ImbueCloudKeyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudPaidListError
from imbue.mngr_imbue_cloud.errors import ImbueCloudQuotaExceededError
from imbue.mngr_imbue_cloud.errors import ImbueCloudRecordFormatTooNewError
from imbue.mngr_imbue_cloud.errors import ImbueCloudShareError
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncConflictError
from imbue.mngr_imbue_cloud.errors import ImbueCloudSyncError
from imbue.mngr_imbue_cloud.errors import WorkspacesEndpointUnavailableError
from imbue.mngr_imbue_cloud.wire import parse_wire_entries
from imbue.mngr_imbue_cloud.wire import validate_wire
from imbue.mngr_imbue_cloud.wire_types import AccountInfo
from imbue.mngr_imbue_cloud.wire_types import AdminAccountInfo
from imbue.mngr_imbue_cloud.wire_types import AuthRawResponse
from imbue.mngr_imbue_cloud.wire_types import LeaseResult
from imbue.mngr_imbue_cloud.wire_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.wire_types import LiteLLMKeyInfo
from imbue.mngr_imbue_cloud.wire_types import LiteLLMKeyMaterial
from imbue.mngr_imbue_cloud.wire_types import PaidListEntry
from imbue.mngr_imbue_cloud.wire_types import R2BucketCreateResult
from imbue.mngr_imbue_cloud.wire_types import R2BucketInfo
from imbue.mngr_imbue_cloud.wire_types import R2KeyInfo
from imbue.mngr_imbue_cloud.wire_types import R2KeyMaterial
from imbue.mngr_imbue_cloud.wire_types import RelayAdminInfo
from imbue.mngr_imbue_cloud.wire_types import ShareInfo
from imbue.mngr_imbue_cloud.wire_types import ShareRelayEndpoint
from imbue.mngr_imbue_cloud.wire_types import ShareRelayLogin
from imbue.mngr_imbue_cloud.wire_types import ShareRelayMap
from imbue.mngr_imbue_cloud.wire_types import StorageCleanupGrant
from imbue.mngr_imbue_cloud.wire_types import StorageRecheckResult
from imbue.mngr_imbue_cloud.wire_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.wire_types import SyncWorkspaceRecord
from imbue.mngr_imbue_cloud.wire_types import WorkspaceInfo
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

DEFAULT_TIMEOUT_SECONDS = 30.0
KEY_OP_TIMEOUT_SECONDS = 90.0
# Operator release: the connector deletes the stop artifacts and tears the
# slice VM down synchronously before answering.
ADMIN_RELEASE_TIMEOUT_SECONDS = 300.0
# One sweep pass releases up to its per-pass budget of leases synchronously.
ADMIN_SWEEP_TIMEOUT_SECONDS = 900.0

# What a user should do when their connector predates the hosted accounts
# surface (browser sign-in). Shared by the login command's up-front probe and
# the device-token exchange's 404 safety net.
CONNECTOR_TOO_OLD_REMEDY = (
    "If this is your own dev/CI env, update it by running `minds-admin env deploy` (Imbue-internal); "
    "otherwise sign in headlessly with `mngr imbue_cloud auth signin --account <email>`."
)

# Transient-transport retry policy for connector calls. The connector is a
# Modal app that scales to zero, so a call hitting a cold/scaling instance can
# fail at the transport layer (DNS "Name or service not known" -> ConnectError,
# "Connection reset by peer" -> ReadError/ConnectError, ConnectTimeout) before
# any HTTP response; a short bounded retry rides those blips out. HTTP *status*
# errors (4xx/5xx) are NOT transport errors and are never retried here -- they
# flow through ``_check``/``_check_bucket`` unchanged.
_TRANSPORT_RETRY_ATTEMPTS = 3
_TRANSPORT_RETRY_BASE_SLEEP_SECONDS = 0.5

# Transport errors raised before the request was put on the wire: the server
# never saw it, so retrying is safe even for a non-idempotent call. Used to gate
# retries on the create/lease POSTs, where a blanket retry on a post-send error
# (e.g. a read error after the server already acted) could double-allocate.
_CONNECT_PHASE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)

# Canonical client-identification header. Every connector call carries it (the
# Python client mirrors the same string into User-Agent), so the connector's
# access log can attribute traffic to shipped client versions -- the input for
# support-window decisions and, later, deprecation-by-date enforcement.
CLIENT_ID_HEADER = "X-Imbue-Client"

# Set by the minds desktop launcher; when present the identifier carries the
# product version ahead of the plugin package version.
_MINDS_RELEASE_ID_ENV_VAR = "MINDS_RELEASE_ID"


@cache
def get_client_identifier() -> str:
    """The ``X-Imbue-Client`` value: ``minds/<release> imbue-cloud-plugin/<version>`` (product half optional)."""
    try:
        plugin_version = metadata.version("imbue-mngr-imbue-cloud")
    except metadata.PackageNotFoundError:
        plugin_version = "unknown"
    plugin_part = f"imbue-cloud-plugin/{plugin_version}"
    minds_release = os.environ.get(_MINDS_RELEASE_ID_ENV_VAR, "")
    return f"minds/{minds_release} {plugin_part}" if minds_release else plugin_part


def _client_id_headers() -> dict[str, str]:
    identifier = get_client_identifier()
    return {CLIENT_ID_HEADER: identifier, "User-Agent": identifier}


class ImbueCloudConnectorClient(MutableModel):
    """Thin synchronous HTTP wrapper over the connector endpoints."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: AnyUrl = Field(description="Base URL of the remote_service_connector")
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, description="Default per-request timeout")
    transport: httpx.BaseTransport | None = Field(
        default=None,
        description=(
            "Optional httpx transport override. Tests inject an httpx.MockTransport here so "
            "requests never leave the process; production leaves it None (module-level httpx calls)."
        ),
    )

    # ------------------------------------------------------------------
    # URL + header helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return str(self.base_url).rstrip("/") + path

    def _bearer(self, access_token: SecretStr) -> dict[str, str]:
        return {**_client_id_headers(), "Authorization": f"Bearer {access_token.get_secret_value()}"}

    def _raise_if_client_too_old(self, response: httpx.Response) -> None:
        """Raise the typed client-too-old error on the connector's structured HTTP 426 refusal."""
        if response.status_code != 426:
            return
        detail = _detail_dict_from_response(response)
        if detail is not None and detail.get("code") == "client_too_old":
            min_version = detail.get("min_version")
            sunset_date = detail.get("sunset_date")
            raise ImbueCloudClientTooOldError(
                str(detail.get("message", CLIENT_TOO_OLD_FALLBACK_MESSAGE)),
                min_version=min_version if isinstance(min_version, str) else None,
                sunset_date=sunset_date if isinstance(sunset_date, str) else None,
            )
        raise ImbueCloudClientTooOldError(CLIENT_TOO_OLD_FALLBACK_MESSAGE, min_version=None, sunset_date=None)

    def _raise_if_quota_exceeded(self, response: httpx.Response) -> None:
        """Raise the typed quota error when a 403 carries the connector's structured detail."""
        if response.status_code != 403:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and detail.get("code") == "quota_exceeded":
            raise ImbueCloudQuotaExceededError(
                str(detail.get("message", "Quota exceeded")),
                entitlement=str(detail.get("entitlement", "")),
                limit=float(detail.get("limit", 0)),
                current=float(detail.get("current", 0)),
            )

    def _raise_if_email_not_verified(self, response: httpx.Response) -> None:
        """Raise the typed verification error when a 403 carries the connector's structured detail."""
        if response.status_code != 403:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and detail.get("code") == "email_not_verified":
            email = detail.get("email")
            raise ImbueCloudEmailNotVerifiedError(
                str(detail.get("message", "This action requires a verified email address")),
                email=email if isinstance(email, str) else None,
            )

    def _raise_if_account_suspended(self, response: httpx.Response) -> None:
        """Raise the typed suspension error when a 403 carries the connector's structured detail."""
        if response.status_code != 403:
            return
        detail = _detail_dict_from_response(response)
        if detail is not None and detail.get("code") == "account_suspended":
            raise ImbueCloudAccountSuspendedError(
                str(detail.get("message", "This account is suspended. Contact support@imbue.com."))
            )

    def _raise_if_grant_budget_exhausted(self, response: httpx.Response) -> None:
        """Raise the typed grant-budget error when a 403 carries the connector's structured detail."""
        if response.status_code != 403:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and detail.get("code") == "cleanup_grant_budget_exhausted":
            raise ImbueCloudCleanupGrantBudgetError(
                str(detail.get("message", "Cleanup-grant budget exhausted")),
                limit=int(detail.get("limit", 0)),
                current=int(detail.get("current", 0)),
                window_hours=int(detail.get("window_hours", 0)),
            )

    def _check(self, response: httpx.Response, exc_cls: type[Exception]) -> dict[str, Any]:
        """Raise ``exc_cls`` on non-2xx, otherwise return parsed JSON.

        Special-cases the structured quota rejection ->
        ImbueCloudQuotaExceededError (and the grant-budget rejection ->
        ImbueCloudCleanupGrantBudgetError), then 401/403 ->
        ImbueCloudAuthError so callers can treat them uniformly across all
        endpoints.
        """
        self._raise_if_client_too_old(response)
        self._raise_if_quota_exceeded(response)
        self._raise_if_grant_budget_exhausted(response)
        self._raise_if_email_not_verified(response)
        self._raise_if_account_suspended(response)
        if response.status_code in (401, 403):
            raise ImbueCloudAuthError(f"Unauthenticated ({response.status_code}): {response.text[:300]}")
        if response.status_code in (200, 201, 202, 204):
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise exc_cls(f"Connector returned non-JSON response: {response.text[:200]}") from exc
        raise exc_cls(f"Connector error {response.status_code}: {response.text[:300]}")

    def _http_call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch one HTTP call to the matching module-level ``httpx`` function.

        Calls ``httpx.get``/``post``/``put``/``delete`` by name at call time (not a
        cached reference) so tests that monkeypatch those functions still
        intercept the request. When an explicit ``transport`` is injected
        (the preferred test seam), the call goes through it instead.

        Redirects are always followed: the connector is a Modal web function,
        and when a synchronous request runs long Modal answers ``303 See
        Other`` pointing at an attempt-token URL the client must GET to fetch
        the eventual result (``curl -L`` semantics). Without following it, a
        slow-but-successful operation reads as a failure.
        """
        if self.transport is not None:
            with httpx.Client(transport=self.transport) as injected_client:
                return injected_client.request(method, url, follow_redirects=True, **kwargs)
        if method == "GET":
            return httpx.get(url, follow_redirects=True, **kwargs)
        if method == "POST":
            return httpx.post(url, follow_redirects=True, **kwargs)
        if method == "PUT":
            return httpx.put(url, follow_redirects=True, **kwargs)
        if method == "DELETE":
            return httpx.delete(url, follow_redirects=True, **kwargs)
        raise SwitchError(f"Unsupported HTTP method: {method}")

    def _send(
        self,
        method: str,
        url: str,
        *,
        exc_cls: type[Exception],
        idempotent: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make one connector request, retrying transient transport failures.

        Returns the raw ``httpx.Response`` (the caller still runs it through
        ``_check``/``_check_bucket`` for HTTP-status handling). On a transport
        error -- the connector was unreachable, reset, or timed out before a
        response -- this retries with bounded exponential backoff and, if it
        still fails, raises ``exc_cls`` with a concise message (never the raw
        httpx traceback). ``idempotent`` (default ``True``) controls retry
        breadth: idempotent calls (every GET/PUT/DELETE and the upsert-style
        POSTs) retry on any ``httpx.TransportError``; non-idempotent POSTs
        (lease, key/bucket creation) pass ``idempotent=False`` so only
        connect-phase errors -- where the request never reached the server --
        are retried, avoiding a double-allocation on a post-send blip.
        """
        for attempt in range(_TRANSPORT_RETRY_ATTEMPTS):
            try:
                return self._http_call(method, url, **kwargs)
            except httpx.TransportError as exc:
                is_last_attempt = attempt + 1 >= _TRANSPORT_RETRY_ATTEMPTS
                may_retry = idempotent or isinstance(exc, _CONNECT_PHASE_TRANSPORT_ERRORS)
                if may_retry and not is_last_attempt:
                    logger.warning(
                        "imbue_cloud connector {} {} transport error (attempt {}/{}); retrying: {}",
                        method,
                        url,
                        attempt + 1,
                        _TRANSPORT_RETRY_ATTEMPTS,
                        exc,
                    )
                    time.sleep(_TRANSPORT_RETRY_BASE_SLEEP_SECONDS * (2**attempt))
                    continue
                raise exc_cls(
                    f"could not reach the imbue_cloud connector at {url} after {attempt + 1} attempt(s): {exc}"
                ) from exc
        raise SwitchError("unreachable: _send exhausted its retry loop without returning or raising")

    # ------------------------------------------------------------------
    # Auth (no bearer token required)
    # ------------------------------------------------------------------

    def auth_signup(self, email: str, password: str) -> AuthRawResponse:
        response = httpx.post(
            self._url("/auth/signup"),
            headers=_client_id_headers(),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        return validate_wire(AuthRawResponse, self._check(response, ImbueCloudAuthError))

    def auth_signin(self, email: str, password: str) -> AuthRawResponse:
        response = httpx.post(
            self._url("/auth/signin"),
            headers=_client_id_headers(),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        return validate_wire(AuthRawResponse, self._check(response, ImbueCloudAuthError))

    def supports_browser_login(self) -> bool:
        """Whether the connector serves the hosted accounts surface (the browser login flow).

        Probes the unauthenticated accounts-config endpoint; a 404 means the
        connector predates the hosted accounts pages (a stale dev/CI env).
        """
        response = self._send(
            "GET",
            self._url("/accounts/api/config"),
            exc_cls=ImbueCloudAuthError,
            headers=_client_id_headers(),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            return False
        self._check(response, ImbueCloudAuthError)
        return True

    def auth_device_token(self, code: str, code_verifier: str, redirect_uri: str) -> AuthRawResponse:
        """Exchange a browser-login one-time code (+ PKCE verifier) for a fresh device session.

        The code is single-use, so only connect-phase transport errors are
        retried (a post-send retry would present an already-consumed code).
        """
        response = self._send(
            "POST",
            self._url("/auth/device/token"),
            exc_cls=ImbueCloudAuthError,
            idempotent=False,
            headers=_client_id_headers(),
            json={"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri},
            timeout=self.timeout_seconds,
        )
        if response.status_code == 400:
            detail = _detail_from_response(response)
            raise ImbueCloudAuthError(f"Device code exchange refused: {detail}")
        if response.status_code == 404:
            raise ImbueCloudAuthError(
                "The connector does not serve the browser-login code exchange (it is too old). "
                + CONNECTOR_TOO_OLD_REMEDY
            )
        return validate_wire(AuthRawResponse, self._check(response, ImbueCloudAuthError))

    def auth_refresh_session(self, refresh_token: SecretStr) -> dict[str, Any]:
        """Returns ``{status, access_token, refresh_token}``.

        Not idempotent: SuperTokens rotates the refresh token, and re-sending
        an already-consumed one trips its token-theft detection, so only
        connect-phase transport errors (request never reached the server) are
        retried.
        """
        response = self._send(
            "POST",
            self._url("/auth/session/refresh"),
            exc_cls=ImbueCloudAuthError,
            idempotent=False,
            headers=_client_id_headers(),
            json={"refresh_token": refresh_token.get_secret_value()},
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAuthError)

    def auth_revoke_session(self, access_token: SecretStr) -> None:
        """Revoke EVERY session for the caller's user (all devices + browser).

        Kept for the explicit sign-out-everywhere action; regular device
        sign-out uses :meth:`auth_revoke_current_session`.
        """
        response = self._send(
            "POST",
            self._url("/auth/session/revoke"),
            exc_cls=ImbueCloudAuthError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        # Treat 401 as "already revoked" (idempotent).
        if response.status_code in (200, 204, 401):
            return
        raise ImbueCloudAuthError(f"Revoke failed ({response.status_code}): {response.text[:200]}")

    def auth_revoke_current_session(self, access_token: SecretStr) -> None:
        """Revoke only the presented session (this device's sign-out).

        Falls back to the revoke-all endpoint against a connector too old to
        serve the device-scoped route, so sign-out never silently leaves the
        token live. A 401 counts as already revoked (idempotent).
        """
        response = self._send(
            "POST",
            self._url("/auth/session/revoke-current"),
            exc_cls=ImbueCloudAuthError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        if response.status_code in (200, 204, 401):
            return
        if response.status_code in (404, 405):
            logger.debug("Connector lacks /auth/session/revoke-current; falling back to revoke-all")
            self.auth_revoke_session(access_token)
            return
        raise ImbueCloudAuthError(f"Revoke failed ({response.status_code}): {response.text[:200]}")

    def auth_send_verification_email(self, access_token: SecretStr, email: str) -> bool:
        """(Re)send the caller's verification email; returns False when suppressed by the server cooldown.

        Authenticated by the caller's own access token (unverified sessions are
        deliberately accepted server-side -- resending is exactly what an
        unverified user needs to do). Not idempotent (a retried send could
        deliver twice), so only connect-phase transport errors are retried.
        """
        response = self._send(
            "POST",
            self._url("/auth/email/send-verification"),
            exc_cls=ImbueCloudAuthError,
            idempotent=False,
            headers=self._bearer(access_token),
            json={"email": email},
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudAuthError)
        sent = body.get("sent")
        if not isinstance(sent, bool):
            # A missing/non-bool ``sent`` is a broken contract; raising (rather
            # than defaulting to False) keeps callers from claiming the send
            # was "suppressed by the cooldown" when nothing of the sort is known.
            raise ImbueCloudAuthError(
                f"Malformed send-verification response: expected a 'sent' bool, got keys {sorted(body)}"
            )
        return sent

    def auth_is_email_verified(self, access_token: SecretStr, email: str) -> bool:
        """Return whether the caller's ``email`` is verified, authenticated by their access token."""
        response = self._send(
            "POST",
            self._url("/auth/email/is-verified"),
            exc_cls=ImbueCloudAuthError,
            headers=self._bearer(access_token),
            json={"email": email},
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudAuthError)
        verified = body.get("verified")
        if not isinstance(verified, bool):
            # A missing/non-bool ``verified`` is a broken contract; raising
            # (rather than defaulting to False) keeps the verification poll
            # from spinning forever on a "not verified" that was never known.
            raise ImbueCloudAuthError(
                f"Malformed is-verified response: expected a 'verified' bool, got keys {sorted(body)}"
            )
        return verified

    def auth_forgot_password(self, email: str) -> None:
        response = httpx.post(
            self._url("/auth/password/forgot"),
            headers=_client_id_headers(),
            json={"email": email},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        self._check(response, ImbueCloudAuthError)

    def auth_reset_password(self, token: str, new_password: str) -> None:
        response = httpx.post(
            self._url("/auth/password/reset"),
            headers=_client_id_headers(),
            json={"token": token, "new_password": new_password},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        self._check(response, ImbueCloudAuthError)

    def auth_get_user(self, user_id: str) -> dict[str, Any]:
        response = httpx.get(
            self._url(f"/auth/users/{user_id}"),
            headers=_client_id_headers(),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        return self._check(response, ImbueCloudAuthError)

    # ------------------------------------------------------------------
    # Hosts (lease pool)
    # ------------------------------------------------------------------

    def lease_host(
        self,
        access_token: SecretStr,
        attributes: LeaseAttributes,
        ssh_public_key: str,
        host_name: str,
        # Hard datacenter requirement: only a host in this region is eligible.
        region: str | None = None,
    ) -> LeaseResult:
        body: dict[str, object] = {
            "attributes": attributes.to_request_dict(),
            "ssh_public_key": ssh_public_key,
            "host_name": host_name,
        }
        # Only send region when set so the connector treats an absent field as
        # unconstrained.
        if region is not None:
            body["region"] = region
        response = httpx.post(
            self._url("/hosts/lease"),
            headers=self._bearer(access_token),
            json=body,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        if response.status_code == 503:
            try:
                detail = response.json().get("detail", "No matching pool host available.")
            except ValueError:
                detail = "No matching pool host available."
            raise ImbueCloudLeaseUnavailableError(detail)
        body_json = self._check(response, ImbueCloudConnectorError)
        return validate_wire(LeaseResult, body_json)

    def release_host(self, access_token: SecretStr, host_db_id: str) -> None:
        """Release a leased host. Raises ``ImbueCloudConnectorError`` on any failure.

        Returns normally only on a 2xx (including the idempotent
        ``already_released``). A transport error (couldn't reach the connector)
        or a non-2xx response -- e.g. the synchronous release returning 5xx
        because the OVH cancel failed -- raises. A failed release must never
        look like success, or the caller silently drops a host whose VPS is
        still running. Callers that want best-effort semantics (e.g. the
        create-rollback path) catch this explicitly.
        """
        try:
            response = httpx.post(
                self._url(f"/hosts/{host_db_id}/release"),
                headers=self._bearer(access_token),
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudConnectorError(
                f"release request for host {host_db_id} could not reach the connector: {exc}"
            ) from exc
        if response.status_code in (200, 204):
            return
        raise ImbueCloudConnectorError(
            f"release of host {host_db_id} returned {response.status_code}: {response.text[:200]}"
        )

    def rename_host(self, access_token: SecretStr, host_db_id: str, host_name: str) -> None:
        """Rename a leased host (update its mutable ``host_name``). Raises ``ImbueCloudConnectorError`` on any failure.

        The lease's ``host_db_id`` is the durable identity; only the friendly
        name changes. Reachable whether or not the leased container is running,
        since the connector owns the name.
        """
        try:
            response = httpx.post(
                self._url(f"/hosts/{host_db_id}/rename"),
                headers=self._bearer(access_token),
                json={"host_name": host_name},
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudConnectorError(
                f"rename request for host {host_db_id} could not reach the connector: {exc}"
            ) from exc
        self._check(response, ImbueCloudConnectorError)

    def enable_host_sharing(self, access_token: SecretStr, host_db_id: str) -> dict[str, Any]:
        """Bring sharing up for a leased host server-side (POST /hosts/{id}/enable-sharing).

        The connector creates/rotates the share record and injects the share
        materials (including the web chrome origin) into the container with
        the pool key -- the primitive behind "enable web access". Idempotent;
        returns the connector's ``{host_id, workspace_domain, region}`` body.
        """
        try:
            response = httpx.post(
                self._url(f"/hosts/{host_db_id}/enable-sharing"),
                headers=self._bearer(access_token),
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudConnectorError(
                f"enable-sharing request for host {host_db_id} could not reach the connector: {exc}"
            ) from exc
        return self._check(response, ImbueCloudConnectorError)

    def list_hosts(self, access_token: SecretStr) -> list[LeasedHostInfo]:
        response = httpx.get(
            self._url("/hosts"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        body = self._check(response, ImbueCloudConnectorError)
        items = body.get("hosts") if isinstance(body, dict) else body
        return parse_wire_entries(LeasedHostInfo, items, "GET /hosts", ImbueCloudConnectorError)

    # ------------------------------------------------------------------
    # Workspaces (full-lifecycle listing + stop/start)
    # ------------------------------------------------------------------

    def _check_workspaces_supported(self, response: httpx.Response) -> None:
        # An old connector has no /workspaces routes; FastAPI answers its
        # fixed 404 "Not Found" (or 405 "Method Not Allowed" for a method
        # mismatch). Surface that as its own type so callers can fall back to
        # the deprecated leased-only /hosts listing. A modern connector's own
        # 404 carries a specific detail (e.g. "No such workspace" when the
        # row was released concurrently) and must fall through to the normal
        # error mapping instead of masquerading as a missing endpoint.
        if response.status_code not in (404, 405):
            return
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail is None or detail in ("Not Found", "Method Not Allowed"):
            raise WorkspacesEndpointUnavailableError(
                "This connector does not serve /workspaces yet; redeploy it (Imbue-internal: `minds-admin env deploy`) "
                "or fall back to the leased-only listing."
            )

    def list_workspaces(self, access_token: SecretStr) -> list[WorkspaceInfo]:
        """List the account's workspaces in every lifecycle state.

        Raises ``WorkspacesEndpointUnavailableError`` against a connector that
        predates the endpoint (callers fall back to ``list_hosts``).
        """
        response = httpx.get(
            self._url("/workspaces"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        self._check_workspaces_supported(response)
        body = self._check(response, ImbueCloudConnectorError)
        return parse_wire_entries(WorkspaceInfo, body, "GET /workspaces", ImbueCloudConnectorError)

    def get_workspace(self, access_token: SecretStr, host_db_id: str) -> WorkspaceInfo:
        """One workspace's lifecycle view (the poll target during stop/start).

        Routed through ``_send`` so a transient transport blip during the
        minutes-long start poll is retried instead of aborting the wait.
        """
        response = self._send(
            "GET",
            self._url(f"/workspaces/{host_db_id}"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudConnectorError)
        return validate_wire(WorkspaceInfo, body)

    def stop_workspace(self, access_token: SecretStr, host_db_id: str) -> WorkspaceStatus:
        """Begin stopping a workspace (VM halt + upload, slot freed after retention); returns its status.

        Asynchronous and idempotent server-side (a retried POST joins the
        in-flight stop): the returned status is ``stopping`` when the request
        initiated (or joined) a stop, or the workspace's current status when
        it was already past ``running``.
        """
        response = self._send(
            "POST",
            self._url(f"/workspaces/{host_db_id}/stop"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check_workspaces_supported(response)
        body = self._check(response, ImbueCloudConnectorError)
        return WorkspaceStatus(str(body.get("status", "")))

    def start_workspace(self, access_token: SecretStr, host_db_id: str) -> WorkspaceStatus:
        """Begin starting a stopped workspace; returns its status (poll ``get_workspace``).

        Idempotent server-side (a retried POST joins the in-flight start).
        """
        response = self._send(
            "POST",
            self._url(f"/workspaces/{host_db_id}/start"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check_workspaces_supported(response)
        self._raise_if_quota_exceeded(response)
        body = self._check(response, ImbueCloudConnectorError)
        return WorkspaceStatus(str(body.get("status", "")))

    def admin_release_workspace(self, admin_key: SecretStr, host_db_id: str) -> str:
        """Operator release of one workspace regardless of owner (admin-key authenticated).

        The owner's exact release chain -- artifacts deleted, slice VM
        destroyed, record retired, row dropped -- returning ``released`` or
        ``already_released``. Idempotent, so transport retries are safe.
        """
        response = self._send(
            "POST",
            self._url(f"/admin/workspaces/{host_db_id}/release"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(admin_key),
            timeout=ADMIN_RELEASE_TIMEOUT_SECONDS,
        )
        body = self._check(response, ImbueCloudConnectorError)
        return str(body.get("status", ""))

    def admin_abandon_workspace(self, admin_key: SecretStr, host_db_id: str, reason: str) -> None:
        """Operator escape hatch: mark a workspace crashed (admin-key authenticated, idempotent)."""
        response = self._send(
            "POST",
            self._url(f"/admin/workspaces/{host_db_id}/abandon"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(admin_key),
            json={"reason": reason},
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudConnectorError)

    # ------------------------------------------------------------------
    # Keys (LiteLLM)
    # ------------------------------------------------------------------

    def create_litellm_key(
        self,
        access_token: SecretStr,
        key_alias: str | None,
        max_budget: float | None,
        budget_duration: str | None,
        metadata: dict[str, str] | None,
    ) -> LiteLLMKeyMaterial:
        body: dict[str, Any] = {}
        if key_alias is not None:
            body["key_alias"] = key_alias
        if max_budget is not None:
            body["max_budget"] = max_budget
        if budget_duration is not None:
            body["budget_duration"] = budget_duration
        if metadata is not None:
            body["metadata"] = metadata
        try:
            response = httpx.post(
                self._url("/keys/create"),
                headers=self._bearer(access_token),
                json=body,
                timeout=KEY_OP_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudKeyError(f"Key creation HTTP request failed: {exc}") from exc
        body_json = self._check(response, ImbueCloudKeyError)
        return validate_wire(LiteLLMKeyMaterial, body_json)

    def list_litellm_keys(self, access_token: SecretStr) -> list[LiteLLMKeyInfo]:
        try:
            response = httpx.get(
                self._url("/keys"),
                headers=self._bearer(access_token),
                timeout=KEY_OP_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudKeyError(f"Key list HTTP request failed: {exc}") from exc
        body = self._check(response, ImbueCloudKeyError)
        return parse_wire_entries(LiteLLMKeyInfo, body, "GET /keys", ImbueCloudKeyError)

    def get_litellm_key_info(self, access_token: SecretStr, key_id: str) -> LiteLLMKeyInfo:
        response = httpx.get(
            self._url(f"/keys/{key_id}"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        body = self._check(response, ImbueCloudKeyError)
        return validate_wire(LiteLLMKeyInfo, body)

    def update_litellm_key_budget(
        self,
        access_token: SecretStr,
        key_id: str,
        max_budget: float | None,
        budget_duration: str | None,
    ) -> None:
        body: dict[str, Any] = {"max_budget": max_budget}
        if budget_duration is not None:
            body["budget_duration"] = budget_duration
        response = httpx.put(
            self._url(f"/keys/{key_id}/budget"),
            headers=self._bearer(access_token),
            json=body,
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._check(response, ImbueCloudKeyError)

    def delete_litellm_key(self, access_token: SecretStr, key_id: str) -> None:
        response = httpx.delete(
            self._url(f"/keys/{key_id}"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._check(response, ImbueCloudKeyError)

    # ------------------------------------------------------------------
    # Shares (self-hosted relays)
    # ------------------------------------------------------------------

    def create_share(
        self,
        access_token: SecretStr,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareInfo:
        """Enable sharing for one workspace; the returned relay token is only ever returned here.

        ``entry_label`` is the workspace's shell-service origin label -- the
        routable origin the hosted web chrome enters and health-probes the
        workspace at (the bare share domain is unrouted on the relay).
        Omitting it keeps any label a previous bring-up recorded.
        ``preferred_region`` steers a first-time share of a local workspace
        (a host the connector has no datacenter record of) to a specific
        relay region; the connector ignores it for pool hosts and always
        keeps an existing share's region.
        """
        body_json: dict[str, str] = {"host_id": host_id}
        if workspace_id:
            # Workspace-keyed sharing: the connector mints (and persists) the
            # share label so the domain follows the workspace, not the machine.
            body_json["workspace_id"] = workspace_id
        if entry_label:
            body_json["entry_label"] = entry_label
        if preferred_region:
            body_json["preferred_region"] = preferred_region
        response = self._send(
            "POST",
            self._url("/shares"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(access_token),
            json=body_json,
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudShareError)
        return _parse_share_info(body, state="active")

    def delete_share(self, access_token: SecretStr, host_id: str) -> None:
        response = self._send(
            "DELETE",
            self._url(f"/shares/{host_id}"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudShareError)

    def get_share_status(self, access_token: SecretStr, host_id: str) -> ShareInfo | None:
        """The share's status document, or None when this workspace has never been shared."""
        response = self._send(
            "GET",
            self._url(f"/shares/{host_id}/status"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            return None
        body = self._check(response, ImbueCloudShareError)
        return _parse_share_info(body, state=str(body.get("state", "")))

    def list_shares(self, access_token: SecretStr) -> list[ShareInfo]:
        response = self._send(
            "GET",
            self._url("/shares"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudShareError)
        return [_parse_share_info(entry, state=str(entry.get("state", ""))) for entry in body.get("shares", [])]

    def list_share_relays(self, access_token: SecretStr) -> ShareRelayMap:
        """The relay fleet: every active relay's tunnel-control endpoint per region."""
        response = self._send(
            "GET",
            self._url("/shares/relays"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudShareError)
        relays = body.get("relays")
        # Strict: a malformed body must not degrade to an empty (or partial)
        # relay map -- the desktop's region picker would silently skip its
        # latency measurement and the misbehaving connector would go unnoticed.
        if not isinstance(relays, dict) or not all(isinstance(endpoints, list) for endpoints in relays.values()):
            raise ImbueCloudShareError(f"Connector returned a malformed relays response: {str(body)[:200]}")
        return ShareRelayMap(
            relay_endpoints_by_region={
                str(region): tuple(str(endpoint) for endpoint in endpoints) for region, endpoints in relays.items()
            },
        )

    def _check_bucket(self, response: httpx.Response) -> Any:
        """Validate a bucket-route response, mapping status codes to typed errors."""
        self._raise_if_client_too_old(response)
        self._raise_if_quota_exceeded(response)
        if response.status_code in (200, 201, 204):
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise ImbueCloudBucketError(f"Connector returned non-JSON response: {response.text[:200]}") from exc
        if response.status_code in (401, 403):
            raise ImbueCloudAuthError(f"Unauthenticated ({response.status_code}): {response.text[:300]}")
        detail = _detail_from_response(response)
        if response.status_code == 404:
            raise ImbueCloudBucketNotFoundError(detail)
        if response.status_code == 409:
            lowered = detail.lower()
            if "not empty" in lowered:
                raise ImbueCloudBucketNotEmptyError(detail)
            if "maximum" in lowered:
                raise ImbueCloudBucketLimitError(detail)
            if "already exists" in lowered:
                raise ImbueCloudBucketExistsError(detail)
            raise ImbueCloudBucketError(detail)
        raise ImbueCloudBucketError(f"Connector error {response.status_code}: {detail}")

    def create_bucket(self, access_token: SecretStr, name: str, access: str) -> R2BucketCreateResult:
        response = httpx.post(
            self._url("/buckets"),
            headers=self._bearer(access_token),
            json={"name": name, "access": access},
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        return validate_wire(R2BucketCreateResult, self._check_bucket(response))

    def list_buckets(self, access_token: SecretStr) -> list[R2BucketInfo]:
        response = httpx.get(
            self._url("/buckets"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        body = self._check_bucket(response)
        return parse_wire_entries(R2BucketInfo, body, "GET /buckets", ImbueCloudBucketError)

    def get_bucket_info(self, access_token: SecretStr, name: str) -> R2BucketInfo:
        response = httpx.get(
            self._url(f"/buckets/{name}"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        return validate_wire(R2BucketInfo, self._check_bucket(response))

    def destroy_bucket(self, access_token: SecretStr, name: str) -> None:
        response = httpx.delete(
            self._url(f"/buckets/{name}"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._check_bucket(response)

    def roll_bucket_key(self, access_token: SecretStr, name: str) -> R2KeyMaterial:
        """Return fresh credentials for a bucket's single key (same Access Key ID, new secret)."""
        response = httpx.post(
            self._url(f"/buckets/{name}/roll-key"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        return validate_wire(R2KeyMaterial, self._check_bucket(response))

    def list_bucket_keys(self, access_token: SecretStr, name: str | None) -> list[R2KeyInfo]:
        """List keys for one bucket (``name`` set) or across all the caller's buckets (``name`` None)."""
        path = "/bucket-keys" if name is None else f"/buckets/{name}/keys"
        response = httpx.get(
            self._url(path),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        body = self._check_bucket(response)
        return parse_wire_entries(R2KeyInfo, body, f"GET {path}", ImbueCloudBucketError)

    # ------------------------------------------------------------------
    # Account (plan + entitlements + usage)
    # ------------------------------------------------------------------

    def get_account(self, access_token: SecretStr) -> AccountInfo:
        """Fetch the account's plan, entitlement values, and live usage."""
        response = self._send(
            "GET",
            self._url("/account"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(access_token),
            # Live usage fans out to Cloudflare + LiteLLM server-side; allow
            # the same generous budget as the other multi-upstream calls.
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return validate_wire(AccountInfo, self._check(response, ImbueCloudAccountError))

    def set_account_plan(self, access_token: SecretStr, plan: str) -> dict[str, Any]:
        """Switch the account's plan; returns ``{plan_name, entitlements}``.

        Idempotent server-side (re-selecting the current plan is a no-op), so
        transport-level retries are safe.
        """
        response = self._send(
            "POST",
            self._url("/account/plan"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(access_token),
            json={"plan": plan},
            timeout=self.timeout_seconds,
        )
        # A 403 here is a refusal with a stated reason (e.g. the ally plan
        # requires a paid-listed email), not an authentication failure, so
        # surface the server's plain-string detail directly instead of letting
        # ``_check`` wrap it as "Unauthenticated". Structured quota 403s still
        # get the typed quota error first.
        if response.status_code == 403:
            self._raise_if_quota_exceeded(response)
            self._raise_if_email_not_verified(response)
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            if isinstance(detail, str) and detail:
                raise ImbueCloudAccountError(detail)
        return self._check(response, ImbueCloudAccountError)

    def create_storage_cleanup_grant(self, access_token: SecretStr) -> StorageCleanupGrant:
        """Request a temporary readwrite restore of storage-downgraded keys for client-side cleanup.

        Idempotent server-side (an active grant is returned as-is; an account
        with nothing downgraded gets a 'not_needed' no-op), so transport-level
        retries are safe. Raises :class:`ImbueCloudCleanupGrantBudgetError`
        when the account's failed-grant budget is exhausted.
        """
        response = self._send(
            "POST",
            self._url("/account/storage-cleanup-grant"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(access_token),
            # The grant measures live usage server-side (one Cloudflare call
            # per bucket), like the other multi-upstream calls.
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return validate_wire(StorageCleanupGrant, self._check(response, ImbueCloudAccountError))

    def recheck_storage(self, access_token: SecretStr) -> StorageRecheckResult:
        """Re-measure live storage usage and apply enforcement immediately (settling any grant).

        Idempotent server-side (re-measuring converges to the same state), so
        transport-level retries are safe.
        """
        response = self._send(
            "POST",
            self._url("/account/storage-recheck"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return validate_wire(StorageRecheckResult, self._check(response, ImbueCloudAccountError))

    # ------------------------------------------------------------------
    # Relay fleet admin (MINDS_ADMIN_KEY authenticated)
    # ------------------------------------------------------------------

    def admin_list_relays(self, admin_api_key: SecretStr) -> list[RelayAdminInfo]:
        response = self._send(
            "GET",
            self._url("/admin/relays"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(admin_api_key),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudShareError)
        relays = body.get("relays", []) if isinstance(body, dict) else body
        return parse_wire_entries(RelayAdminInfo, relays, "GET /admin/relays", ImbueCloudShareError)

    def admin_register_relay(
        self,
        admin_api_key: SecretStr,
        # None registers a fresh relay (the connector mints the id); a value
        # re-registers / revives that relay in place.
        relay_id: str | None,
        region: str,
        tunnel_endpoint: str,
        ip_address: str,
        instance_name: str,
    ) -> RelayAdminInfo:
        """Register (or update) one relay row; an idempotent upsert, safe to retry."""
        body_json: dict[str, str] = {
            "region": region,
            "tunnel_endpoint": tunnel_endpoint,
            "ip_address": ip_address,
            "instance_name": instance_name,
        }
        if relay_id:
            body_json["relay_id"] = relay_id
        response = self._send(
            "POST",
            self._url("/admin/relays"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(admin_api_key),
            json=body_json,
            timeout=self.timeout_seconds,
        )
        return validate_wire(RelayAdminInfo, self._check(response, ImbueCloudShareError))

    def admin_retire_relay(self, admin_api_key: SecretStr, relay_id: str) -> dict[str, Any]:
        """Retire one relay (it leaves assignment, DNS, and frps auth); idempotent."""
        response = self._send(
            "DELETE",
            self._url(f"/admin/relays/{relay_id}"),
            exc_cls=ImbueCloudShareError,
            headers=self._bearer(admin_api_key),
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudShareError)

    # ------------------------------------------------------------------
    # Account admin (email-addressed, MINDS_ADMIN_KEY authenticated)
    # ------------------------------------------------------------------

    @staticmethod
    def _admin_account_path(email: str) -> str:
        """The /admin/accounts path segment for an email, percent-encoded.

        Email local parts may legally contain URL-reserved characters like
        ``?`` or ``#``; encoding (keeping ``@`` literal) stops them from
        splitting the URL. FastAPI decodes the path param server-side.
        """
        return f"/admin/accounts/{quote(email, safe='@')}"

    def admin_get_account(self, admin_api_key: SecretStr, email: str) -> AdminAccountInfo:
        response = self._send(
            "GET",
            self._url(self._admin_account_path(email)),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return validate_wire(AdminAccountInfo, self._check(response, ImbueCloudAccountError))

    def admin_set_account_plan(self, admin_api_key: SecretStr, email: str, plan: str) -> dict[str, Any]:
        # Always resets to the plan's defaults, so a retried request lands in
        # the same state (safe to retry on transport errors).
        response = self._send(
            "POST",
            self._url(f"{self._admin_account_path(email)}/plan"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            json={"plan": plan},
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_set_account_quota(
        self, admin_api_key: SecretStr, email: str, entitlement: str, value: float
    ) -> dict[str, Any]:
        # A plain overwrite of one entitlement value (safe to retry on
        # transport errors).
        response = self._send(
            "POST",
            self._url(f"{self._admin_account_path(email)}/quota"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            json={"entitlement": entitlement, "value": value},
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_revoke_sessions(self, admin_api_key: SecretStr, email: str) -> dict[str, Any]:
        """Revoke every SuperTokens session of the addressed account (safe to retry)."""
        response = self._send(
            "POST",
            self._url(f"{self._admin_account_path(email)}/revoke-sessions"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_suspend_account(
        self, admin_api_key: SecretStr, email: str, reason: str, block_storage: bool
    ) -> dict[str, Any]:
        """Suspend the account (idempotent fan-out; re-running converges / escalates)."""
        response = self._send(
            "POST",
            self._url(f"{self._admin_account_path(email)}/suspend"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            json={"reason": reason, "block_storage": block_storage},
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_unsuspend_account(self, admin_api_key: SecretStr, email: str) -> dict[str, Any]:
        """Lift the account's suspension (idempotent restore fan-out)."""
        response = self._send(
            "POST",
            self._url(f"{self._admin_account_path(email)}/unsuspend"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_stop_workspace(self, admin_api_key: SecretStr, host_db_id: str) -> dict[str, Any]:
        """Operator force-stop of one workspace (idempotent, like the owner stop)."""
        response = self._send(
            "POST",
            self._url(f"/admin/workspaces/{host_db_id}/stop"),
            exc_cls=ImbueCloudConnectorError,
            headers=self._bearer(admin_api_key),
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudConnectorError)

    def admin_run_r2_sweep(self, admin_api_key: SecretStr, email: str | None) -> dict[str, Any]:
        """Run one R2 storage-quota sweep pass on demand; ``email`` scopes it to one account.

        The sweep converges to the same state however often it runs, so
        transport-level retries are safe.
        """
        params = {"email": email} if email else None
        response = self._send(
            "POST",
            self._url("/admin/sweep/r2"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            params=params,
            # A full pass fans out to Cloudflare per over-quota account.
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        return self._check(response, ImbueCloudAccountError)

    def admin_run_lease_record_sweep(
        self, admin_api_key: SecretStr, dry_run: bool, grace_seconds: float | None
    ) -> dict[str, Any]:
        """Run one lease-vs-record sweep pass on demand (operator tool + deployment tests).

        ``dry_run`` reports the verdicts and reap candidates without releasing
        anything; ``grace_seconds`` overrides the tombstone grace window.
        """
        params: dict[str, str] = {}
        if dry_run:
            params["dry_run"] = "1"
        if grace_seconds is not None:
            params["grace_seconds"] = str(grace_seconds)
        response = self._send(
            "POST",
            self._url("/admin/sweep/lease-records"),
            exc_cls=ImbueCloudAccountError,
            headers=self._bearer(admin_api_key),
            params=params or None,
            timeout=ADMIN_SWEEP_TIMEOUT_SECONDS,
        )
        return self._check(response, ImbueCloudAccountError)

    # ------------------------------------------------------------------
    # Workspace sync (records + account key bundle)
    # ------------------------------------------------------------------

    def list_sync_records(self, access_token: SecretStr) -> list[SyncWorkspaceRecord]:
        response = self._send(
            "GET",
            self._url("/sync/records"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudSyncError)
        records = body.get("records", []) if isinstance(body, dict) else body
        return parse_wire_entries(SyncWorkspaceRecord, records, "GET /sync/records", ImbueCloudSyncError)

    def put_sync_record(self, access_token: SecretStr, record: SyncWorkspaceRecord) -> SyncWorkspaceRecord:
        """Push one record (CAS on revision); returns the stored row after the write.

        A 409 raises one of two typed errors: the structured
        ``record_format_too_new`` refusal raises
        :class:`ImbueCloudRecordFormatTooNewError` (terminal -- the stored row
        was written at a newer record format, so retrying cannot succeed until
        the client updates); any other 409 raises
        :class:`ImbueCloudSyncConflictError`, carrying the server's current
        row for a revision conflict so the caller can merge and retry.
        """
        response = self._send(
            "PUT",
            self._url(f"/sync/records/by-workspace/{record.agent_id}"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            json=record.model_dump(mode="json"),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            # CLEANUP: drop this fallback once every supported connector serves
            # the workspace-keyed sync routes (a 404 here can only be a server
            # from before they existed -- the route itself never 404s).
            response = self._send(
                "PUT",
                self._url(f"/sync/records/{record.host_id}"),
                exc_cls=ImbueCloudSyncError,
                headers=self._bearer(access_token),
                json=record.model_dump(mode="json"),
                timeout=self.timeout_seconds,
            )
        if response.status_code == 409:
            detail_message = _detail_from_response(response)
            detail = _detail_dict_from_response(response)
            if detail is not None and detail.get("code") == "record_format_too_new":
                # Prefer the structured human message over detail_message,
                # which for a dict detail is the repr of the whole payload
                # (embedded stored row included).
                format_message = detail.get("message")
                raise ImbueCloudRecordFormatTooNewError(
                    format_message if isinstance(format_message, str) else detail_message
                )
            stored = detail.get("stored") if detail is not None else None
            raise ImbueCloudSyncConflictError(detail_message, stored if isinstance(stored, dict) else None)
        body = self._check(response, ImbueCloudSyncError)
        return validate_wire(SyncWorkspaceRecord, body)

    def delete_sync_record(self, access_token: SecretStr, host_id: str) -> None:
        """Remove one workspace record by its current host id (disassociation; idempotent).

        Refused with 409 ``lease_active`` while the workspace still holds a
        pool lease; destroying the workspace is what releases the lease.
        """
        response = self._send(
            "DELETE",
            self._url(f"/sync/records/{host_id}"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudSyncError)

    def delete_sync_record_by_workspace(self, access_token: SecretStr, workspace_id: str) -> None:
        """Remove one workspace record by its workspace id (disassociation; idempotent).

        Refused with 409 ``lease_active`` while the workspace still holds a
        pool lease; destroying the workspace is what releases the lease.
        Against a connector from before the workspace-keyed routes, resolves
        the workspace's current host id through the record listing and deletes
        by host instead.
        """
        response = self._send(
            "DELETE",
            self._url(f"/sync/records/by-workspace/{workspace_id}"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            # CLEANUP: drop this fallback once every supported connector serves
            # the workspace-keyed sync routes. The listing round-trip is the
            # only way to recover the host coordinate the old route needs.
            for record in self.list_sync_records(access_token):
                if record.agent_id == workspace_id:
                    self.delete_sync_record(access_token, record.host_id)
                    return
            return
        self._check(response, ImbueCloudSyncError)

    def scrub_sync_secrets(self, access_token: SecretStr) -> int:
        """Strip encrypted_secrets from all the account's records; returns how many were scrubbed."""
        response = self._send(
            "POST",
            self._url("/sync/scrub-secrets"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudSyncError)
        return int(body.get("scrubbed", 0))

    def get_key_bundle(self, access_token: SecretStr) -> SyncKeyBundle | None:
        """Fetch the account's password-wrapped key bundle, or None when none is stored."""
        response = self._send(
            "GET",
            self._url("/sync/bundle"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            return None
        body = self._check(response, ImbueCloudSyncError)
        return validate_wire(SyncKeyBundle, body)

    def put_key_bundle(self, access_token: SecretStr, bundle: SyncKeyBundle) -> None:
        response = self._send(
            "PUT",
            self._url("/sync/bundle"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            json=bundle.model_dump(mode="json"),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudSyncError)

    def delete_key_bundle(self, access_token: SecretStr) -> None:
        response = self._send(
            "DELETE",
            self._url("/sync/bundle"),
            exc_cls=ImbueCloudSyncError,
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudSyncError)

    # ------------------------------------------------------------------
    # Paid lists (admin-key authenticated)
    # ------------------------------------------------------------------
    #
    # These take the fixed admin API key (NOT a SuperTokens
    # session token); the connector authenticates them against
    # ``MINDS_ADMIN_KEY`` and rejects user tokens.

    def _list_paid_entries(
        self, admin_api_key: SecretStr, path: str, value_key: str, paid_only: bool
    ) -> list[PaidListEntry]:
        response = httpx.get(
            self._url(path),
            headers=self._bearer(admin_api_key),
            params={"paid_only": "true" if paid_only else "false"},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        body = self._check(response, ImbueCloudPaidListError)
        if not isinstance(body, list):
            raise ImbueCloudPaidListError(f"GET {path}: expected a JSON list, got {type(body).__name__}")
        entries: list[PaidListEntry] = []
        for entry in body:
            if not isinstance(entry, dict):
                logger.warning("Skipped a non-object paid-list entry from GET {}: {!r}", path, entry)
                continue
            entries.append(_parse_paid_list_entry(entry, value_key))
        return entries

    def _post_paid_entry(self, admin_api_key: SecretStr, path: str, value: str) -> dict[str, Any]:
        response = httpx.post(
            self._url(path),
            headers=self._bearer(admin_api_key),
            json={"value": value},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        return self._check(response, ImbueCloudPaidListError)

    def list_paid_domains(self, admin_api_key: SecretStr, paid_only: bool) -> list[PaidListEntry]:
        return self._list_paid_entries(admin_api_key, "/paid/domains", "domain", paid_only)

    def add_paid_domain(self, admin_api_key: SecretStr, domain: str) -> dict[str, Any]:
        return self._post_paid_entry(admin_api_key, "/paid/domains/add", domain)

    def remove_paid_domain(self, admin_api_key: SecretStr, domain: str) -> dict[str, Any]:
        return self._post_paid_entry(admin_api_key, "/paid/domains/remove", domain)

    def list_paid_emails(self, admin_api_key: SecretStr, paid_only: bool) -> list[PaidListEntry]:
        return self._list_paid_entries(admin_api_key, "/paid/emails", "email", paid_only)

    def add_paid_email(self, admin_api_key: SecretStr, email: str) -> dict[str, Any]:
        return self._post_paid_entry(admin_api_key, "/paid/emails/add", email)

    def remove_paid_email(self, admin_api_key: SecretStr, email: str) -> dict[str, Any]:
        return self._post_paid_entry(admin_api_key, "/paid/emails/remove", email)


def create_litellm_key_rotating_on_exists(
    client: ImbueCloudConnectorClient,
    access_token: SecretStr,
    key_alias: str,
    max_budget: float | None,
    budget_duration: str | None,
    metadata: dict[str, str] | None,
) -> LiteLLMKeyMaterial:
    """Create a LiteLLM key, rotating (delete + re-create) when ``key_alias`` is taken.

    LiteLLM enforces globally-unique aliases and never re-reveals a minted
    secret, so a caller with a deterministic alias (e.g. one key per
    workspace) would otherwise dead-end forever once the alias exists -- for
    example after a create whose response was lost in flight. The whole
    list -> delete -> re-create fallback runs inside this one process, so a
    caller shelling out to the CLI pays a single process boot for the entire
    rotation. Rotation invalidates the key previously issued for the alias.
    """
    try:
        return client.create_litellm_key(
            access_token=access_token,
            key_alias=key_alias,
            max_budget=max_budget,
            budget_duration=budget_duration,
            metadata=metadata,
        )
    except ImbueCloudKeyError as exc:
        # Substring-matched against LiteLLM's unique-alias rejection; any
        # other create failure propagates untouched.
        if f"alias '{key_alias}' already exists" not in str(exc):
            raise
        logger.debug("Key alias {} already exists; rotating it", key_alias)
    existing_keys = client.list_litellm_keys(access_token)
    matching_tokens = [key.token for key in existing_keys if key.key_alias == key_alias and key.token]
    if not matching_tokens:
        raise ImbueCloudKeyError(
            f"Key alias '{key_alias}' is already taken, but no key with that alias is listable under this account"
        )
    for existing_token in matching_tokens:
        client.delete_litellm_key(access_token, existing_token)
    return client.create_litellm_key(
        access_token=access_token,
        key_alias=key_alias,
        max_budget=max_budget,
        budget_duration=budget_duration,
        metadata=metadata,
    )


def _detail_from_response(response: httpx.Response) -> str:
    """Extract the connector's ``detail`` error message, falling back to the raw body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return response.text[:300]


def _detail_dict_from_response(response: httpx.Response) -> dict[str, Any] | None:
    """Extract the connector's structured ``detail`` dict, or None when the body has no such shape."""
    try:
        payload = response.json()
    except ValueError as exc:
        logger.debug("Response body of {} is not JSON: {}", response.status_code, exc)
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, dict) else None


def _parse_paid_list_entry(raw: dict[str, Any], value_key: str) -> PaidListEntry:
    """Coerce a connector paid-list row into a ``PaidListEntry``.

    ``value_key`` is ``"domain"`` or ``"email"`` -- the connector names the
    value column differently for each table; this maps it onto the generic
    ``value`` field.
    """
    return PaidListEntry(
        value=str(raw.get(value_key, "")),
        is_paid=bool(raw.get("is_paid", False)),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def _parse_share_info(body: dict[str, Any], state: str) -> ShareInfo:
    """Build a ShareInfo from a connector share payload (create/status/list shapes)."""
    relay_token = body.get("relay_token")
    raw_endpoints = body.get("relay_endpoints")
    relay_endpoints = tuple(
        ShareRelayEndpoint(relay_id=str(entry.get("relay_id", "")), endpoint=str(entry.get("endpoint", "")))
        for entry in (raw_endpoints if isinstance(raw_endpoints, list) else [])
        if isinstance(entry, dict)
    )
    # Per-relay tunnel login stamps; only the status document carries them.
    raw_relay_logins = body.get("relays")
    relay_logins = tuple(
        ShareRelayLogin(relay_id=str(entry.get("relay_id", "")), last_login_at=entry.get("last_login_at"))
        for entry in (raw_relay_logins if isinstance(raw_relay_logins, list) else [])
        if isinstance(entry, dict)
    )
    return ShareInfo(
        host_id=str(body.get("host_id", "")),
        workspace_domain=str(body.get("workspace_domain", "")),
        region=str(body.get("region", "")),
        state=state or "active",
        relay_endpoints=relay_endpoints,
        relays=relay_logins,
        relay_token=SecretStr(relay_token) if relay_token else None,
        last_tunnel_login_at=body.get("last_tunnel_login_at"),
        cert_not_after=body.get("cert_not_after"),
    )
