"""Client for the LiteLLM key management endpoints of the remote service connector.

Encapsulates HTTP calls to create, list, inspect, update, and delete LiteLLM
virtual keys. Authentication uses the caller's SuperTokens JWT.
"""

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError

_DEFAULT_TIMEOUT_SECONDS = 90.0


class LiteLLMKeyError(MindError):
    """Raised when a LiteLLM key operation fails."""

    ...


class CreateKeyResult(FrozenModel):
    """Result of creating a LiteLLM virtual key."""

    key: str = Field(description="The generated LiteLLM virtual key")
    base_url: str = Field(description="The LiteLLM proxy base URL for ANTHROPIC_BASE_URL")


class KeyInfo(FrozenModel):
    """Information about a LiteLLM virtual key."""

    token: str = Field(description="Hashed key token identifier")
    key_alias: str | None = Field(default=None, description="Human-readable alias")
    key_name: str | None = Field(default=None, description="Key name")
    spend: float = Field(default=0.0, description="Total spend in USD")
    max_budget: float | None = Field(default=None, description="Max budget in USD")
    budget_duration: str | None = Field(default=None, description="Budget reset duration")
    user_id: str | None = Field(default=None, description="User ID the key belongs to")


class LiteLLMKeyClient(FrozenModel):
    """Client for the LiteLLM key management endpoints of the remote service connector."""

    connector_url: AnyUrl = Field(description="Base URL of the remote service connector")
    timeout_seconds: float = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        description="HTTP request timeout in seconds",
    )

    def _url(self, path: str) -> str:
        return str(self.connector_url).rstrip("/") + path

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": "Bearer {}".format(access_token)}

    def create_key(
        self,
        access_token: str,
        key_alias: str | None,
        max_budget: float | None,
        budget_duration: str | None,
        metadata: dict[str, str] | None,
    ) -> CreateKeyResult:
        """Create a new LiteLLM virtual key.

        Raises LiteLLMKeyError on failure.
        """
        body: dict[str, object] = {}
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
                headers=self._auth_headers(access_token),
                json=body,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LiteLLMKeyError("Key creation request failed: {}".format(exc)) from exc

        if response.status_code not in (200, 201):
            raise LiteLLMKeyError("Key creation failed ({}): {}".format(response.status_code, response.text[:200]))

        return CreateKeyResult.model_validate(response.json())

    def list_keys(self, access_token: str) -> list[KeyInfo]:
        """List all virtual keys owned by the authenticated user."""
        try:
            response = httpx.get(
                self._url("/keys"),
                headers=self._auth_headers(access_token),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Key list request failed: {}", exc)
            return []

        if response.status_code != 200:
            logger.warning("Key list failed: {} {}", response.status_code, response.text[:200])
            return []

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("Key list returned non-JSON response: {}", exc)
            return []

        result: list[KeyInfo] = []
        for item in data:
            try:
                result.append(KeyInfo.model_validate(item))
            except ValueError:
                logger.debug("Skipped unparseable key entry: {}", item)
        return result

    def get_key_info(self, access_token: str, key_id: str) -> KeyInfo:
        """Get info for a specific key.

        Raises LiteLLMKeyError on failure.
        """
        try:
            response = httpx.get(
                self._url("/keys/{}".format(key_id)),
                headers=self._auth_headers(access_token),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LiteLLMKeyError("Key info request failed: {}".format(exc)) from exc

        if response.status_code != 200:
            raise LiteLLMKeyError("Key info failed ({}): {}".format(response.status_code, response.text[:200]))

        return KeyInfo.model_validate(response.json())

    def update_budget(
        self,
        access_token: str,
        key_id: str,
        max_budget: float | None,
        budget_duration: str | None,
    ) -> None:
        """Update the budget for a key.

        Raises LiteLLMKeyError on failure.
        """
        body: dict[str, object] = {}
        body["max_budget"] = max_budget
        if budget_duration is not None:
            body["budget_duration"] = budget_duration

        try:
            response = httpx.put(
                self._url("/keys/{}/budget".format(key_id)),
                headers=self._auth_headers(access_token),
                json=body,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LiteLLMKeyError("Budget update request failed: {}".format(exc)) from exc

        if response.status_code not in (200, 204):
            raise LiteLLMKeyError("Budget update failed ({}): {}".format(response.status_code, response.text[:200]))

    def delete_key(self, access_token: str, key_id: str) -> None:
        """Delete a key.

        Raises LiteLLMKeyError on failure.
        """
        try:
            response = httpx.delete(
                self._url("/keys/{}".format(key_id)),
                headers=self._auth_headers(access_token),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LiteLLMKeyError("Key deletion request failed: {}".format(exc)) from exc

        if response.status_code not in (200, 204):
            raise LiteLLMKeyError("Key deletion failed ({}): {}".format(response.status_code, response.text[:200]))
