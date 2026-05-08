"""HTTP client for the remote_service_connector.

One client wraps all four connector concerns (auth, hosts, keys, tunnels) so
the CLI commands and provider can share a single httpx instance per account.

Authentication semantics:
- Methods explicitly named ``*_auth_*`` (signin/signup/oauth/refresh) take no
  bearer token and are intended for unauthenticated callers.
- All other methods take an ``access_token`` (a SecretStr).
- The session store handles persistence; this client never reads or writes
  session files itself.
"""

from typing import Any

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_imbue_cloud.data_types import AuthPolicy
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeaseResult
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.data_types import LiteLLMKeyInfo
from imbue.mngr_imbue_cloud.data_types import LiteLLMKeyMaterial
from imbue.mngr_imbue_cloud.data_types import ServiceInfo
from imbue.mngr_imbue_cloud.data_types import TunnelInfo
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudKeyError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudTunnelError

DEFAULT_TIMEOUT_SECONDS = 30.0
KEY_OP_TIMEOUT_SECONDS = 90.0


class AuthRawResponse(FrozenModel):
    """Subset of ``/auth/*`` response that we care about.

    The connector's response shape is:
    ``{status, message, user, tokens, needs_email_verification}``.
    """

    status: str
    message: str | None = None
    user: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    needs_email_verification: bool = False


class ImbueCloudConnectorClient(MutableModel):
    """Thin synchronous HTTP wrapper over the connector endpoints."""

    base_url: AnyUrl = Field(description="Base URL of the remote_service_connector")
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, description="Default per-request timeout")

    # ------------------------------------------------------------------
    # URL + header helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return str(self.base_url).rstrip("/") + path

    def _bearer(self, access_token: SecretStr) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token.get_secret_value()}"}

    def _check(self, response: httpx.Response, exc_cls: type[Exception]) -> dict[str, Any]:
        """Raise ``exc_cls`` on non-2xx, otherwise return parsed JSON.

        Special-cases 401/403 -> ImbueCloudAuthError so callers can treat them
        uniformly across all endpoints.
        """
        if response.status_code in (401, 403):
            raise ImbueCloudAuthError(f"Unauthenticated ({response.status_code}): {response.text[:300]}")
        if response.status_code in (200, 201, 204):
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise exc_cls(f"Connector returned non-JSON response: {response.text[:200]}") from exc
        raise exc_cls(f"Connector error {response.status_code}: {response.text[:300]}")

    # ------------------------------------------------------------------
    # Auth (no bearer token required)
    # ------------------------------------------------------------------

    def auth_signup(self, email: str, password: str) -> AuthRawResponse:
        response = httpx.post(
            self._url("/auth/signup"),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        return AuthRawResponse.model_validate(self._check(response, ImbueCloudAuthError))

    def auth_signin(self, email: str, password: str) -> AuthRawResponse:
        response = httpx.post(
            self._url("/auth/signin"),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        return AuthRawResponse.model_validate(self._check(response, ImbueCloudAuthError))

    def auth_oauth_authorize(self, provider_id: str, callback_url: str) -> dict[str, Any]:
        response = httpx.post(
            self._url("/auth/oauth/authorize"),
            json={"provider_id": provider_id, "callback_url": callback_url},
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAuthError)

    def auth_oauth_callback(
        self,
        provider_id: str,
        callback_url: str,
        query_params: dict[str, str],
    ) -> AuthRawResponse:
        response = httpx.post(
            self._url("/auth/oauth/callback"),
            json={
                "provider_id": provider_id,
                "callback_url": callback_url,
                "query_params": query_params,
            },
            timeout=self.timeout_seconds,
        )
        return AuthRawResponse.model_validate(self._check(response, ImbueCloudAuthError))

    def auth_refresh_session(self, refresh_token: SecretStr) -> dict[str, Any]:
        """Returns ``{status, access_token, refresh_token}``."""
        response = httpx.post(
            self._url("/auth/session/refresh"),
            json={"refresh_token": refresh_token.get_secret_value()},
            timeout=self.timeout_seconds,
        )
        return self._check(response, ImbueCloudAuthError)

    def auth_revoke_session(self, access_token: SecretStr) -> None:
        response = httpx.post(
            self._url("/auth/session/revoke"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        # Treat 401 as "already revoked" (idempotent).
        if response.status_code in (200, 204, 401):
            return
        raise ImbueCloudAuthError(f"Revoke failed ({response.status_code}): {response.text[:200]}")

    def auth_send_verification_email(self, user_id: str, email: str) -> None:
        response = httpx.post(
            self._url("/auth/email/send-verification"),
            json={"user_id": user_id, "email": email},
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudAuthError)

    def auth_is_email_verified(self, user_id: str, email: str) -> bool:
        response = httpx.post(
            self._url("/auth/email/is-verified"),
            json={"user_id": user_id, "email": email},
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudAuthError)
        return bool(body.get("verified", False))

    def auth_forgot_password(self, email: str) -> None:
        response = httpx.post(
            self._url("/auth/password/forgot"),
            json={"email": email},
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudAuthError)

    def auth_reset_password(self, token: str, new_password: str) -> None:
        response = httpx.post(
            self._url("/auth/password/reset"),
            json={"token": token, "new_password": new_password},
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudAuthError)

    def auth_get_user(self, user_id: str) -> dict[str, Any]:
        response = httpx.get(
            self._url(f"/auth/users/{user_id}"),
            timeout=self.timeout_seconds,
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
    ) -> LeaseResult:
        body = {
            "attributes": attributes.to_request_dict(),
            "ssh_public_key": ssh_public_key,
        }
        response = httpx.post(
            self._url("/hosts/lease"),
            headers=self._bearer(access_token),
            json=body,
            timeout=self.timeout_seconds,
        )
        if response.status_code == 503:
            try:
                detail = response.json().get("detail", "No matching pool host available.")
            except ValueError:
                detail = "No matching pool host available."
            raise ImbueCloudLeaseUnavailableError(detail)
        body_json = self._check(response, ImbueCloudConnectorError)
        return LeaseResult.model_validate(body_json)

    def release_host(self, access_token: SecretStr, host_db_id: str) -> bool:
        """Release a leased host. Returns True on success, False otherwise (logs warning)."""
        try:
            response = httpx.post(
                self._url(f"/hosts/{host_db_id}/release"),
                headers=self._bearer(access_token),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Release HTTP request failed: {}", exc)
            return False
        if response.status_code in (200, 204):
            return True
        logger.warning("Release returned {}: {}", response.status_code, response.text[:200])
        return False

    def list_hosts(self, access_token: SecretStr) -> list[LeasedHostInfo]:
        response = httpx.get(
            self._url("/hosts"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudConnectorError)
        items = body.get("hosts") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return []
        result: list[LeasedHostInfo] = []
        for entry in items:
            try:
                result.append(LeasedHostInfo.model_validate(entry))
            except ValueError:
                logger.debug("Skipped unparseable leased host entry: {}", entry)
        return result

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
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudKeyError(f"Key creation HTTP request failed: {exc}") from exc
        body_json = self._check(response, ImbueCloudKeyError)
        return LiteLLMKeyMaterial.model_validate(body_json)

    def list_litellm_keys(self, access_token: SecretStr) -> list[LiteLLMKeyInfo]:
        try:
            response = httpx.get(
                self._url("/keys"),
                headers=self._bearer(access_token),
                timeout=KEY_OP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ImbueCloudKeyError(f"Key list HTTP request failed: {exc}") from exc
        body = self._check(response, ImbueCloudKeyError)
        if not isinstance(body, list):
            return []
        result: list[LiteLLMKeyInfo] = []
        for entry in body:
            try:
                result.append(LiteLLMKeyInfo.model_validate(entry))
            except ValueError:
                logger.debug("Skipped unparseable key entry: {}", entry)
        return result

    def get_litellm_key_info(self, access_token: SecretStr, key_id: str) -> LiteLLMKeyInfo:
        response = httpx.get(
            self._url(f"/keys/{key_id}"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        body = self._check(response, ImbueCloudKeyError)
        return LiteLLMKeyInfo.model_validate(body)

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
        )
        self._check(response, ImbueCloudKeyError)

    def delete_litellm_key(self, access_token: SecretStr, key_id: str) -> None:
        response = httpx.delete(
            self._url(f"/keys/{key_id}"),
            headers=self._bearer(access_token),
            timeout=KEY_OP_TIMEOUT_SECONDS,
        )
        self._check(response, ImbueCloudKeyError)

    # ------------------------------------------------------------------
    # Tunnels (Cloudflare)
    # ------------------------------------------------------------------

    def create_tunnel(
        self,
        access_token: SecretStr,
        agent_id: str,
        default_auth_policy: AuthPolicy | None,
    ) -> TunnelInfo:
        body: dict[str, Any] = {"agent_id": agent_id}
        if default_auth_policy is not None:
            body["default_auth_policy"] = _auth_policy_to_connector_body(default_auth_policy)
        response = httpx.post(
            self._url("/tunnels"),
            headers=self._bearer(access_token),
            json=body,
            timeout=self.timeout_seconds,
        )
        body_json = self._check(response, ImbueCloudTunnelError)
        return _parse_tunnel_info(body_json)

    def list_tunnels(self, access_token: SecretStr) -> list[TunnelInfo]:
        response = httpx.get(
            self._url("/tunnels"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudTunnelError)
        if not isinstance(body, list):
            return []
        return [_parse_tunnel_info(entry) for entry in body if isinstance(entry, dict)]

    def delete_tunnel(self, access_token: SecretStr, tunnel_name: str) -> None:
        response = httpx.delete(
            self._url(f"/tunnels/{tunnel_name}"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudTunnelError)

    def add_service(
        self,
        access_token: SecretStr,
        tunnel_name: str,
        service_name: str,
        service_url: str,
    ) -> ServiceInfo:
        response = httpx.post(
            self._url(f"/tunnels/{tunnel_name}/services"),
            headers=self._bearer(access_token),
            json={"service_name": service_name, "service_url": service_url},
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudTunnelError)
        return _parse_service_info(body)

    def list_services(self, access_token: SecretStr, tunnel_name: str) -> list[ServiceInfo]:
        response = httpx.get(
            self._url(f"/tunnels/{tunnel_name}/services"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudTunnelError)
        if not isinstance(body, list):
            return []
        return [_parse_service_info(entry) for entry in body if isinstance(entry, dict)]

    def remove_service(self, access_token: SecretStr, tunnel_name: str, service_name: str) -> None:
        response = httpx.delete(
            self._url(f"/tunnels/{tunnel_name}/services/{service_name}"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudTunnelError)

    def get_tunnel_auth(self, access_token: SecretStr, tunnel_name: str) -> AuthPolicy:
        response = httpx.get(
            self._url(f"/tunnels/{tunnel_name}/auth"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudTunnelError)
        return _parse_auth_policy(body)

    def set_tunnel_auth(self, access_token: SecretStr, tunnel_name: str, policy: AuthPolicy) -> None:
        response = httpx.put(
            self._url(f"/tunnels/{tunnel_name}/auth"),
            headers=self._bearer(access_token),
            json=_auth_policy_to_connector_body(policy),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudTunnelError)

    def get_service_auth(
        self,
        access_token: SecretStr,
        tunnel_name: str,
        service_name: str,
    ) -> AuthPolicy:
        response = httpx.get(
            self._url(f"/tunnels/{tunnel_name}/services/{service_name}/auth"),
            headers=self._bearer(access_token),
            timeout=self.timeout_seconds,
        )
        body = self._check(response, ImbueCloudTunnelError)
        return _parse_auth_policy(body)

    def set_service_auth(
        self,
        access_token: SecretStr,
        tunnel_name: str,
        service_name: str,
        policy: AuthPolicy,
    ) -> None:
        response = httpx.put(
            self._url(f"/tunnels/{tunnel_name}/services/{service_name}/auth"),
            headers=self._bearer(access_token),
            json=_auth_policy_to_connector_body(policy),
            timeout=self.timeout_seconds,
        )
        self._check(response, ImbueCloudTunnelError)


def _parse_tunnel_info(raw: dict[str, Any]) -> TunnelInfo:
    """Best-effort coerce a connector tunnel dict into our TunnelInfo."""
    services = raw.get("services") or ()
    if isinstance(services, list):
        # Connector returns either ['name1', 'name2'] or [{service_name: ...}, ...].
        flat: list[str] = []
        for entry in services:
            if isinstance(entry, str):
                flat.append(entry)
            elif isinstance(entry, dict) and "service_name" in entry:
                flat.append(str(entry["service_name"]))
        services_tuple = tuple(flat)
    else:
        services_tuple = ()
    token_value = raw.get("token") or raw.get("tunnel_token")
    return TunnelInfo(
        tunnel_name=str(raw.get("tunnel_name", raw.get("name", ""))),
        tunnel_id=str(raw.get("tunnel_id", raw.get("id", ""))),
        token=SecretStr(str(token_value)) if token_value else None,
        services=services_tuple,
    )


def _parse_service_info(raw: dict[str, Any]) -> ServiceInfo:
    return ServiceInfo(
        service_name=str(raw.get("service_name", raw.get("name", ""))),
        service_url=str(raw.get("service_url", raw.get("url", ""))),
        hostname=str(raw.get("hostname", "")),
    )


def _auth_policy_to_connector_body(policy: AuthPolicy) -> dict[str, Any]:
    """Translate the plugin's high-level ``AuthPolicy`` into the body shape
    the connector accepts (Cloudflare-native ``{"rules": [...]}``).

    The connector's ``AuthPolicy`` model wraps a list of Cloudflare Access
    rule dicts (``{action, include}``) and is consumed both directly
    (per-service Access policies) and via KV (default-tunnel policy). Our
    high-level model carries flat allow-lists (emails, email domains,
    required IDPs); this helper bundles everything into a single
    ``allow`` rule whose ``include`` is the union of the three.

    A policy with no allow-list members serializes to ``{"rules": []}``,
    which the connector interprets as "no policy" without rejecting the
    request body.
    """
    include: list[dict[str, Any]] = []
    for email in policy.emails:
        include.append({"email": {"email": email}})
    for domain in policy.email_domains:
        include.append({"email_domain": {"domain": domain}})
    for idp_id in policy.require_idp:
        include.append({"login_method": {"id": idp_id}})
    if not include:
        return {"rules": []}
    return {"rules": [{"action": "allow", "include": include}]}


def _parse_auth_policy(raw: dict[str, Any]) -> AuthPolicy:
    """Translate the connector's ``{"rules": [...]}`` response back into
    the plugin's high-level ``AuthPolicy``.

    Walks every rule's ``include`` list and bins entries by Cloudflare
    Access rule type (``email`` / ``email_domain`` / ``login_method``).
    Unknown shapes are ignored rather than raising so a connector that
    later adds a new include type doesn't break older plugin clients.
    """
    emails: list[str] = []
    email_domains: list[str] = []
    require_idp: list[str] = []
    rules = raw.get("rules") or []
    if not isinstance(rules, list):
        return AuthPolicy()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        include = rule.get("include") or []
        if not isinstance(include, list):
            continue
        for entry in include:
            if not isinstance(entry, dict):
                continue
            email_obj = entry.get("email")
            if isinstance(email_obj, dict) and isinstance(email_obj.get("email"), str):
                emails.append(email_obj["email"])
                continue
            domain_obj = entry.get("email_domain")
            if isinstance(domain_obj, dict) and isinstance(domain_obj.get("domain"), str):
                email_domains.append(domain_obj["domain"])
                continue
            login_obj = entry.get("login_method")
            if isinstance(login_obj, dict) and isinstance(login_obj.get("id"), str):
                require_idp.append(login_obj["id"])
    return AuthPolicy(
        emails=tuple(emails),
        email_domains=tuple(email_domains),
        require_idp=tuple(require_idp),
    )
